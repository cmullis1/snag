"""The Host and Origin guard (spec 3.4), as ASGI middleware around the whole app, so it covers every
HTTP route, the static files and the WebSocket alike.

- Same user: with fast user switching, other signed-in users reach the same 127.0.0.1 port. A
  caller whose process runs as another Windows user is refused with 403 (a WebSocket before it is
  accepted), whatever it asks; so is one whose user can't be told (logged once). same_user (the
  platform's same_user_peer, snag.platform.peer) answers from the TCP table and the caller's
  process token; each client address's answer is kept PEER_CACHE_SECONDS, so a connection's
  requests cost one lookup, and a port Windows later gives to another program is looked up afresh.
  Without same_user (this module's own tests) nobody is asked.
- Host: every request and WebSocket must be addressed to 127.0.0.1:<port> or localhost:<port>.
  Anything else (a DNS-rebinding page whose own name resolves to 127.0.0.1) is refused with 403.
- Origin: requests that change something (anything but GET and HEAD) and every WebSocket handshake
  must come from the full app tab (http://127.0.0.1:<port>, http://localhost:<port>) or the Snag
  extension (chrome-extension://<EXTENSION_ID>). Browsers set Origin themselves and pages can't
  forge it. A GET or HEAD carrying any other Origin (a cross-origin fetch) is refused too, as is one
  without an Origin that the browser marks as another site's subresource load (Sec-Fetch-Site
  cross-site or same-site, outside a navigation), so other pages can't read Snag's data.
- CORS: only the extension's origin gets Access-Control-Allow-Origin (never "*"), and its preflight
  OPTIONS requests are answered here. The tab is same-origin and needs none.
- Every response is X-Content-Type-Options: nosniff; HTML responses (the tab's pages) also carry a
  Content-Security-Policy that forbids framing and limits what the page may load, connect and submit
  to. Every answer under /api/ is Cache-Control: no-store and Vary: Origin (it may differ by caller).
- Unexpected errors: an exception the app raises before it starts answering is logged once (with its
  traceback) and answered here as the API's JSON 500 (kind unknown, details "<type>: <message>"), so
  that answer carries the headers above too, CORS included. One raised after the answer began can't
  be answered any more; it goes on to the server, which logs it and drops the connection. A caller
  that disconnects while its request is read (ClientDisconnect) is logged at debug level only and
  not answered: nothing went wrong in Snag, and no one is left to answer.

The server binds 127.0.0.1 only (snag/__main__.py); this guard is the second line."""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from starlette.requests import ClientDisconnect

from snag.server.extension import EXTENSION_ID
from snag.server.routes import error_body

log = logging.getLogger(__name__)

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

SAFE_METHODS = frozenset({"GET", "HEAD"})
CORS_METHODS = "GET, HEAD, POST, PUT, DELETE"
CORS_HEADERS = "Content-Type, Accept"
CORS_MAX_AGE = "600"
POLICY_VIOLATION = 1008  # WebSocket close code
API_PREFIX = "/api/"
UNEXPECTED = "Something went wrong in Snag."
OTHER_USER = "Snag only answers programs of the Windows user it runs for."
PEER_CACHE_SECONDS = 2.0  # a client address's answer; ports aren't handed out again that fast
PEER_CACHE_SIZE = 256

Address = tuple[str, int]
SameUser = Callable[[Address, Address], bool]  # (client, server) -> same user; OSError: can't tell


def allowed_hosts(port: int) -> frozenset[str]:
    return frozenset({f"127.0.0.1:{port}", f"localhost:{port}"})


def extension_origin(extension_id: str = EXTENSION_ID) -> str:
    return f"chrome-extension://{extension_id}"


def allowed_origins(port: int, extension_id: str = EXTENSION_ID) -> frozenset[str]:
    return frozenset({f"http://127.0.0.1:{port}", f"http://localhost:{port}", extension_origin(extension_id)})


def content_security_policy(port: int) -> str:
    return (
        "frame-ancestors 'none'; default-src 'self'; img-src 'self' https: data:; "
        f"connect-src 'self' ws://127.0.0.1:{port} ws://localhost:{port}; "
        "base-uri 'none'; form-action 'self'; object-src 'none'"
    )


def unexpected_details(error: BaseException) -> str:
    """What a 500's details say: the error's type and message (the traceback goes to the log only)."""
    return f"{type(error).__name__}: {error}"


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers") or ():
        if key.lower() == name:
            return value.decode("latin-1")
    return None


class Guard:
    def __init__(self, app: ASGIApp, *, port: int, extension_id: str = EXTENSION_ID,
                 same_user: SameUser | None = None, clock: Callable[[], float] = time.monotonic):
        self.app = app
        self.port = port
        self.hosts = allowed_hosts(port)
        self.origins = allowed_origins(port, extension_id)
        self.extension = extension_origin(extension_id)
        self.policy = content_security_policy(port).encode("latin-1")
        self._same_user = same_user
        self._clock = clock
        self._peers: dict[Address, tuple[bool, float]] = {}  # client -> (same user, when looked up)
        self._lookup_failed_logged = False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        kind = scope["type"]
        if kind == "http":
            await self._http(scope, receive, send)
        elif kind == "websocket":
            await self._websocket(scope, receive, send)
        else:  # lifespan
            await self.app(scope, receive, send)

    # ----- checks -----

    def _same_user_ok(self, scope: Scope) -> bool:
        """Whether the caller's process runs as Snag's Windows user (see the module docstring)."""
        if self._same_user is None:
            return True
        client, server = scope.get("client"), scope.get("server")
        if not client or not server:
            return False
        peer, local = (str(client[0]), int(client[1])), (str(server[0]), int(server[1]))
        now = self._clock()
        cached = self._peers.get(peer)
        if cached is not None and now - cached[1] < PEER_CACHE_SECONDS:
            return cached[0]
        try:
            same = bool(self._same_user(peer, local))
        except OSError as error:
            if not self._lookup_failed_logged:
                self._lookup_failed_logged = True
                log.warning("Couldn't tell which Windows user a caller runs as, so it was refused "
                            "(logged once): %s", error)
            same = False
        if len(self._peers) >= PEER_CACHE_SIZE:
            self._peers = {key: value for key, value in self._peers.items() if now - value[1] < PEER_CACHE_SECONDS}
        self._peers[peer] = (same, now)
        return same

    def _host_ok(self, scope: Scope) -> bool:
        host = _header(scope, b"host")
        return host is not None and host.lower() in self.hosts

    def _refusal(self, scope: Scope, origin: str | None) -> str | None:
        """Why an HTTP request is refused, or None to let it through."""
        if not self._host_ok(scope):
            return "Snag only answers requests addressed to 127.0.0.1 (wrong Host)."
        if not self._same_user_ok(scope):
            return OTHER_USER
        method = scope["method"].upper()
        if origin is not None:
            if origin not in self.origins:
                return "Snag only answers its own pages and its extension (wrong Origin)."
            return None
        if method not in SAFE_METHODS:
            return "Snag only accepts changes from its own pages and its extension (missing Origin)."
        site = (_header(scope, b"sec-fetch-site") or "").lower()
        mode = (_header(scope, b"sec-fetch-mode") or "").lower()
        if site in ("cross-site", "same-site") and mode != "navigate":
            return "Snag only answers its own pages and its extension (another site's request)."
        return None

    # ----- HTTP -----

    async def _http(self, scope: Scope, receive: Receive, send: Send) -> None:
        origin = _header(scope, b"origin")
        is_api = str(scope.get("path", "")).startswith(API_PREFIX)
        refusal = self._refusal(scope, origin)
        if refusal is not None:  # never CORS: the caller isn't one Snag answers
            await self._send_json(self._with_headers(send, is_api=is_api), 403, error_body("forbidden", refusal))
            return
        is_extension = origin == self.extension
        if scope["method"].upper() == "OPTIONS" and _header(scope, b"access-control-request-method"):
            if is_extension:
                await self._preflight(scope, send)
            else:  # the tab is same-origin and never preflights
                await self._send_json(self._with_headers(send, is_api=is_api), 403, error_body(
                    "forbidden", "Snag doesn't take cross-origin requests from here."))
            return

        started = False
        answer = self._with_headers(send, is_api=is_api, is_extension=is_extension)

        async def tracked(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await answer(message)

        try:
            await self.app(scope, receive, tracked)
        except ClientDisconnect:  # the caller left while its request was read: no one to answer
            log.debug("%s %s: the client disconnected", scope["method"], scope.get("path", ""))
        except Exception as error:
            if started:
                raise
            log.error("%s %s failed", scope["method"], scope.get("path", ""), exc_info=True)
            await self._send_json(answer, 500, error_body("unknown", UNEXPECTED, details=unexpected_details(error)))

    def _with_headers(self, send: Send, *, is_api: bool, is_extension: bool = False) -> Send:
        """send, adding the guard's headers (see the module docstring) to the answer's start."""
        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers") or () if k.lower() != b"vary"]
                varies = [v for k, v in message.get("headers") or () if k.lower() == b"vary"]
                headers.append((b"x-content-type-options", b"nosniff"))
                content_type = next((v for k, v in headers if k.lower() == b"content-type"), b"")
                if content_type.lower().startswith(b"text/html"):
                    headers.append((b"content-security-policy", self.policy))
                if is_api:
                    headers = [(k, v) for k, v in headers if k.lower() != b"cache-control"]
                    headers.append((b"cache-control", b"no-store"))
                if is_extension:
                    headers.append((b"access-control-allow-origin", self.extension.encode("latin-1")))
                if is_api or is_extension:
                    varies.append(b"Origin")
                if varies:
                    headers.append((b"vary", b", ".join(dict.fromkeys(varies))))
                message["headers"] = headers
            await send(message)

        return send_with_headers

    async def _preflight(self, scope: Scope, send: Send) -> None:
        headers = [
            (b"access-control-allow-origin", self.extension.encode("latin-1")),
            (b"vary", b"Origin"),
            (b"access-control-allow-methods", CORS_METHODS.encode()),
            (b"access-control-allow-headers", CORS_HEADERS.encode()),
            (b"access-control-max-age", CORS_MAX_AGE.encode()),
            (b"x-content-type-options", b"nosniff"),
        ]
        if (_header(scope, b"access-control-request-private-network") or "").lower() == "true":
            headers.append((b"access-control-allow-private-network", b"true"))
        await send({"type": "http.response.start", "status": 204, "headers": headers})
        await send({"type": "http.response.body", "body": b""})

    @staticmethod
    async def _send_json(send: Send, status: int, body: dict) -> None:
        """A JSON answer through send (one of _with_headers, which adds nosniff and the rest)."""
        data = json.dumps(body).encode("utf-8")
        await send({"type": "http.response.start", "status": status, "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(data)).encode()),
        ]})
        await send({"type": "http.response.body", "body": data})

    # ----- WebSocket -----

    async def _websocket(self, scope: Scope, receive: Receive, send: Send) -> None:
        origin = _header(scope, b"origin")
        if not self._host_ok(scope) or origin not in self.origins or not self._same_user_ok(scope):
            # Closing before accepting makes the server answer the handshake with 403.
            message = await receive()
            if message["type"] == "websocket.connect":
                await send({"type": "websocket.close", "code": POLICY_VIOLATION})
            return
        await self.app(scope, receive, send)
