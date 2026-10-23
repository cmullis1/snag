"""The Host / Origin guard (spec 3.4) on a small app of its own; tests/test_server_routes.py checks it
covers every route of the real one."""
import httpx
import pytest
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from starlette.routing import Route, Router, WebSocketRoute

from snag.server.extension import EXTENSION_ID
from snag.server.guard import Guard, allowed_origins, content_security_policy
from tests.server_env import BASE, EVIL, EXTENSION, PORT, TAB, WsSession


async def page(request):
    return HTMLResponse("<!doctype html><title>Snag</title>")


async def data(request):
    return JSONResponse({"ok": True})


async def change(request):
    return JSONResponse({"changed": True})


async def text(request):
    return PlainTextResponse("plain")


async def broken(request):
    raise RuntimeError("a bug")


async def half(request):
    async def body():
        yield b"{"
        raise RuntimeError("mid-stream")

    return StreamingResponse(body(), media_type="application/json")


async def ws_endpoint(websocket):
    await websocket.accept()
    await websocket.send_json({"hello": True})
    await websocket.close()


@pytest.fixture
def app():
    # A bare router: in the real app Starlette's own error middleware sits outside the guard.
    inner = Router(routes=[
        Route("/", page),
        Route("/api/data", data),
        Route("/api/change", change, methods=["POST", "PUT", "DELETE"]),
        Route("/text", text),
        Route("/api/broken", broken),
        Route("/api/half", half),
        WebSocketRoute("/ws", ws_endpoint),
    ])
    return Guard(inner, port=PORT)


@pytest.fixture
async def client(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as client:
        yield client


def is_forbidden(response: httpx.Response) -> bool:
    return response.status_code == 403 and response.json()["error"]["kind"] == "forbidden"


def test_allowed_origins():
    assert allowed_origins(PORT) == frozenset({
        "http://127.0.0.1:47823", "http://localhost:47823", f"chrome-extension://{EXTENSION_ID}",
    })


# ----- Host -----

@pytest.mark.parametrize("host", ["127.0.0.1:47823", "localhost:47823", "LOCALHOST:47823"])
async def test_snags_own_hosts_pass(client, host):
    assert (await client.get("/api/data", headers={"host": host})).status_code == 200


@pytest.mark.parametrize("host", [
    "evil.com:47823",  # DNS rebinding: evil.com resolving to 127.0.0.1
    "evil.com",
    "127.0.0.1",
    "127.0.0.1:80",
    "localhost:47824",
    "127.0.0.1:47823.evil.com",
    "",
])
async def test_other_hosts_are_refused(client, host):
    response = await client.get("/api/data", headers={"host": host})
    assert is_forbidden(response)
    assert "Host" in response.json()["error"]["message"]


async def test_a_missing_host_is_refused(app):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "GET", "path": "/api/data", "raw_path": b"/api/data", "headers": [],
             "query_string": b"", "scheme": "http", "server": ("127.0.0.1", PORT), "root_path": "",
             "http_version": "1.1", "asgi": {"version": "3.0"}}
    await app(scope, receive, send)
    assert sent[0]["status"] == 403


async def test_the_host_is_checked_on_websockets(app):
    session = WsSession(app, headers={"host": "evil.com:47823"})
    assert await session.accepted() is False
    await session.task


# ----- Origin -----

@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE"])
async def test_changes_need_an_origin(client, method):
    assert is_forbidden(await client.request(method, "/api/change"))


@pytest.mark.parametrize("origin", [
    "https://evil.example", "null", "http://127.0.0.1:47824", "http://127.0.0.1", "https://127.0.0.1:47823",
    "chrome-extension://abcdefghijklmnopabcdefghijklmnop", "http://localhost:47823.evil.example",
])
async def test_changes_from_other_origins_are_refused(client, origin):
    response = await client.post("/api/change", headers={"origin": origin})
    assert is_forbidden(response)
    assert "access-control-allow-origin" not in response.headers


@pytest.mark.parametrize("origin", [BASE, "http://localhost:47823"])
async def test_changes_from_the_app_tab_pass(client, origin):
    response = await client.post("/api/change", headers={"origin": origin})
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers  # same origin: no CORS needed


async def test_changes_from_the_extension_pass_with_cors(client):
    response = await client.post("/api/change", headers=EXTENSION)
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == EXTENSION["origin"]
    assert "origin" in response.headers["vary"].lower()


async def test_reads_without_an_origin_pass(client):
    assert (await client.get("/api/data")).status_code == 200


async def test_reads_from_another_origin_are_refused(client):
    # Browsers attach Origin to cross-origin fetches; that page must not read Snag's data.
    assert is_forbidden(await client.get("/api/data", headers=EVIL))
    assert (await client.head("/api/data", headers=EVIL)).status_code == 403


async def test_reads_from_the_extension_get_cors(client):
    response = await client.get("/api/data", headers=EXTENSION)
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == EXTENSION["origin"]


async def test_cross_site_subresource_loads_without_an_origin_are_refused(client):
    # <script src> or <img src> from another site: no Origin header, but Sec-Fetch-Site says so.
    headers = {"sec-fetch-site": "cross-site", "sec-fetch-mode": "no-cors", "sec-fetch-dest": "script"}
    assert is_forbidden(await client.get("/api/data", headers=headers))


async def test_following_a_link_to_the_app_tab_is_allowed(client):
    headers = {"sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate", "sec-fetch-dest": "document"}
    assert (await client.get("/", headers=headers)).status_code == 200


# ----- preflight -----

async def test_the_extensions_preflight_is_answered(client):
    response = await client.options("/api/change", headers={
        **EXTENSION, "access-control-request-method": "PUT", "access-control-request-headers": "content-type",
    })
    assert response.status_code == 204
    headers = response.headers
    assert headers["access-control-allow-origin"] == EXTENSION["origin"]
    assert {m.strip() for m in headers["access-control-allow-methods"].split(",")} >= {"GET", "POST", "PUT", "DELETE"}
    assert "content-type" in headers["access-control-allow-headers"].lower()
    assert headers.get("access-control-allow-origin") != "*"


async def test_a_private_network_preflight_is_allowed_for_the_extension(client):
    response = await client.options("/api/change", headers={
        **EXTENSION, "access-control-request-method": "POST", "access-control-request-private-network": "true",
    })
    assert response.headers["access-control-allow-private-network"] == "true"


async def test_a_foreign_preflight_is_refused(client):
    response = await client.options("/api/change", headers={**EVIL, "access-control-request-method": "POST"})
    assert is_forbidden(response)
    assert "access-control-allow-origin" not in response.headers


# ----- WebSocket -----

@pytest.mark.parametrize("headers", [{"origin": "https://evil.example"}, {"origin": "null"}])
async def test_websockets_from_other_origins_are_refused(app, headers):
    session = WsSession(app, headers=headers)
    assert await session.accepted() is False
    assert session.close_code == 1008
    await session.task


async def test_websockets_need_an_origin(app):
    session = WsSession(app, headers={"origin": None})
    assert await session.accepted() is False
    await session.task


@pytest.mark.parametrize("headers", [TAB, EXTENSION])
async def test_websockets_from_the_tab_and_extension_pass(app, headers):
    session = WsSession(app, headers=headers)
    assert await session.accepted() is True
    assert await session.receive_json() == {"hello": True}
    await session.task


# ----- response headers -----

async def test_html_carries_the_content_security_policy(client):
    response = await client.get("/")
    assert response.headers["content-security-policy"] == content_security_policy(PORT)
    policy = response.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in policy
    assert "default-src 'self'" in policy
    assert "img-src 'self' https: data:" in policy
    assert "connect-src 'self' ws://127.0.0.1:47823 ws://localhost:47823" in policy
    assert response.headers["x-content-type-options"] == "nosniff"


async def test_every_response_is_nosniff(client):
    for path in ("/api/data", "/text"):
        response = await client.get(path)
        assert response.headers["x-content-type-options"] == "nosniff"
        assert "content-security-policy" not in response.headers


async def test_the_policy_also_locks_base_forms_and_plugins(client):
    policy = (await client.get("/")).headers["content-security-policy"]
    assert "base-uri 'none'" in policy and "form-action 'self'" in policy and "object-src 'none'" in policy


@pytest.mark.parametrize("headers", [TAB, EXTENSION, {}])
async def test_api_answers_are_never_cached_and_vary_by_origin(client, headers):
    for response in (await client.get("/api/data", headers=headers), await client.post("/api/change", headers=EVIL)):
        assert response.headers["cache-control"] == "no-store"
        assert response.headers.get_list("vary") == ["Origin"]


async def test_files_outside_the_api_keep_their_caching(client):
    response = await client.get("/text")
    assert "cache-control" not in response.headers and "vary" not in response.headers


async def test_an_unexpected_failure_is_a_json_500_with_the_guards_headers(client, caplog):
    import logging

    with caplog.at_level(logging.ERROR):
        response = await client.get("/api/broken", headers=EXTENSION)
    assert response.status_code == 500
    error = response.json()["error"]
    assert error["kind"] == "unknown" and error["message"] == "Something went wrong in Snag."
    assert error["details"] == "RuntimeError: a bug"
    assert response.headers["access-control-allow-origin"] == EXTENSION["origin"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"
    logged = [record for record in caplog.records if record.exc_info]
    assert len(logged) == 1 and "/api/broken" in logged[0].getMessage()
    assert "Traceback" in caplog.text


async def test_a_failure_after_the_answer_began_is_left_to_the_server(client):
    with pytest.raises(RuntimeError, match="mid-stream"):
        await client.get("/api/half")


async def test_refusals_are_nosniff_json(client):
    response = await client.post("/api/change", headers=EVIL)
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["x-content-type-options"] == "nosniff"
    error = response.json()["error"]
    assert set(error) >= {"kind", "message", "action", "can_retry"}


async def test_a_client_gone_before_its_body_arrived_is_logged_quietly_and_not_answered(caplog):
    """The page closed (or the extension popup went away) while Snag read its request: there is no one
    to answer, and nothing went wrong in Snag, so no traceback and no 500."""
    import logging

    async def reads_body(request):
        await request.body()
        return JSONResponse({"read": True})

    guard = Guard(Router(routes=[Route("/api/upload", reads_body, methods=["POST"])]), port=PORT)
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "http",
        "path": "/api/upload", "raw_path": b"/api/upload", "query_string": b"", "root_path": "",
        "headers": [(b"host", f"127.0.0.1:{PORT}".encode()), (b"origin", BASE.encode())],
        "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", PORT),
    }
    sent = []

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    with caplog.at_level(logging.DEBUG, logger="snag.server.guard"):
        await guard(scope, receive, send)
    assert sent == []
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING or record.exc_info]
    assert any("disconnect" in record.getMessage().lower() for record in caplog.records)


# ----- the caller's Windows user -----

OTHER_USER = "Snag only answers programs of the Windows user it runs for."


class Peers:
    """Stands in for the platform's same_user_peer: who owns each client port, as the test says."""

    def __init__(self, other_ports=(), failing=False):
        self.other_ports, self.failing = set(other_ports), failing
        self.asked: list[tuple] = []

    def __call__(self, peer, server):
        self.asked.append((peer, server))
        if self.failing:
            raise OSError("the TCP table couldn't be read")
        return peer[1] not in self.other_ports


def guarded(peers, **options):
    inner = Router(routes=[Route("/api/data", data), Route("/api/change", change, methods=["POST"]),
                           WebSocketRoute("/ws", ws_endpoint)])
    return Guard(inner, port=PORT, same_user=peers, **options)


def call(app, port, path="/api/data", method="GET", headers=None):
    """One request from client port `port` (httpx's ASGI transport always says port 123)."""
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", port))
    return httpx.AsyncClient(transport=transport, base_url=BASE, headers=headers or {}).request(method, path)


async def test_programs_of_another_windows_user_are_refused(caplog):
    """Fast user switching: another user's browser reaches the same 127.0.0.1 port, and its extension
    would hand this user's Snag that user's cookies."""
    peers = Peers(other_ports={6000})
    app = guarded(peers)
    assert (await call(app, 5000)).status_code == 200
    refused = await call(app, 6000)
    assert is_forbidden(refused) and refused.json()["error"]["message"] == OTHER_USER
    assert refused.headers["x-content-type-options"] == "nosniff"
    assert "access-control-allow-origin" not in refused.headers
    assert is_forbidden(await call(app, 6000, "/api/change", "POST", EXTENSION))
    assert is_forbidden(await call(app, 6000, "/api/change", "OPTIONS",
                                   {**EXTENSION, "access-control-request-method": "POST"}))
    assert peers.asked[0] == (("127.0.0.1", 5000), ("127.0.0.1", PORT))


async def test_websockets_of_another_windows_user_are_refused():
    peers = Peers(other_ports={50000})  # WsSession's client port
    session = WsSession(guarded(peers))
    assert not await session.accepted()
    assert session.close_code == 1008
    assert await WsSession(guarded(Peers())).accepted()


async def test_when_the_user_cannot_be_told_the_caller_is_refused_and_logged_once(caplog):
    app = guarded(Peers(failing=True))
    with caplog.at_level("WARNING", logger="snag.server.guard"):
        assert is_forbidden(await call(app, 5000))
        assert is_forbidden(await call(app, 5001))
    warnings = [r for r in caplog.records if "couldn't be read" in r.getMessage()]
    assert len(warnings) == 1


async def test_a_request_without_a_client_address_is_refused():
    app = guarded(Peers())
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "GET", "path": "/api/data", "headers": [(b"host", f"127.0.0.1:{PORT}".encode())],
             "client": None, "server": ("127.0.0.1", PORT), "query_string": b"", "root_path": ""}
    await app(scope, receive, send)
    assert sent[0]["status"] == 403


async def test_each_client_port_is_looked_up_once_while_its_answer_is_fresh():
    """The lookup reads the TCP table and a process token: once per connection, not per request. A
    port's answer is kept briefly, well short of the time Windows takes to give the port to another
    connection."""
    now = [1000.0]
    peers = Peers()
    app = guarded(peers, clock=lambda: now[0])
    for _ in range(3):
        assert (await call(app, 5000)).status_code == 200
    assert len(peers.asked) == 1
    assert (await call(app, 5001)).status_code == 200
    assert len(peers.asked) == 2
    now[0] += 60  # stale: asked again
    assert (await call(app, 5000)).status_code == 200
    assert len(peers.asked) == 3


async def test_without_a_user_check_the_guard_asks_nothing(client):
    """Guard(..., same_user=None), as on this module's own app: the server passes the platform's."""
    assert (await client.get("/api/data")).status_code == 200
