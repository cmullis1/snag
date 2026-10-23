"""The WebSocket hub at /ws: every open page (the full app tab, the extension popup) gets the queue's
and Snag's changes as they happen.

On connect a client gets one snapshot, then events, all JSON text messages:

  {"type": "snapshot", "jobs": [Job], "queue": Queue, "status": Status, "settings": Settings}
      Settings is GET /api/settings' whole object, so a page has them without asking
  {"type": "job", "job": Job}            a job was added or changed (schemas.job_json)
  {"type": "job_removed", "id": str}     a job was cleared
  {"type": "queue", "queue": Queue}      the queue banner's state changed (schemas.queue_state_json)
  {"type": "status", ...}                part of Status changed; merge it into the snapshot's:
      {"type": "status", "updater": {"setup": {...}, "update": {...}}}  (UpdaterService.status())
      {"type": "status", "tools": ToolStatus}          the tool checks ran again (after an install)
      {"type": "status", "youtube_login": str | None}  cookies arrived from the browser
  {"type": "settings", "settings": Settings}  the settings changed (PUT /api/settings, or a picked
      destination folder remembered); Settings is GET /api/settings' whole object
  {"type": "setup",  "tool", "phase", "done", "total", "message"}  first-run install progress
  {"type": "update", "tool", "phase", "done", "total", "message"}  update or reinstall progress
      (tool: "yt-dlp", "ffmpeg" or "deno"; phase: downloading, verifying, installing, waiting,
      done or error; done/total in bytes, total null when unknown; message set on error)

Status (also GET /api/status) is status_json(core):
  {"version": str, "tools": {ytdlp, ffmpeg, ffprobe, deno: {present, version, message}, ready},
   "updater": UpdaterService.status(), "youtube_login": ISO time | null, "queue": Queue}

The updater's events (see snag.updater.service) and the server's settings events (sent through
SnagCore.emit) are forwarded exactly as they are sent.

Delivery: events reach every client in the order they happened. Each client has its own buffer and a
task that sends from it, so a slow page never stalls the queue or the other pages. While a client's
buffer waits, a newer event of the same subject replaces the older one (moving to the end, so it
still comes after everything that happened before it): a job's latest state, the queue's, each
status part's, the settings, each tool's progress. A job_removed drops the job's pending update. A
client whose buffer holds more than max_pending events or max_pending_bytes bytes of them, or whose
socket takes longer than send_timeout to take one, is dropped with close code 1013 (try again
later); the page reconnects and gets a fresh snapshot. Anything the client sends is ignored.

close() ends every connection and stops listening to the queue and core."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from snag import __version__
from snag.server.schemas import job_json, queue_state_json, tool_status_json
from snag.store.settings import Settings, settings_to_json

if TYPE_CHECKING:
    from snag.app import SnagCore

log = logging.getLogger(__name__)

MAX_PENDING = 10_000  # events waiting for one client; with coalescing, about one per job
MAX_PENDING_BYTES = 64 * 1024 * 1024  # their size (UTF-8), the snapshot included
SEND_TIMEOUT = 15.0  # seconds a client may take to accept one message
CLOSE_TIMEOUT = 2.0
GOING_AWAY = 1001
INTERNAL_ERROR = 1011
TRY_AGAIN_LATER = 1013
YOUTUBE = "youtube.com"


class Socket(Protocol):
    """What the hub needs of a WebSocket (Starlette's WebSocket has it)."""

    async def accept(self) -> None: ...
    async def send_text(self, data: str) -> None: ...
    async def receive(self) -> dict: ...
    async def close(self, code: int = 1000) -> None: ...


def iso_time(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def youtube_login(core: SnagCore) -> str | None:
    """When YouTube cookies last arrived from the browser (ISO time), or None."""
    return iso_time(core.cookies.updated_at(YOUTUBE))


def status_json(core: SnagCore) -> dict[str, Any]:
    return {
        "version": __version__,
        "tools": tool_status_json(core.checks),
        "updater": core.updater.status(),
        "youtube_login": youtube_login(core),
        "queue": queue_state_json(core.queue.state),
    }


def snapshot_json(core: SnagCore, settings_json: Callable[[Settings], dict[str, Any]] = settings_to_json,
                  ) -> dict[str, Any]:
    """settings_json: the stored settings -> GET /api/settings' object (the server adds what Windows
    says about starting Snag; see snag.server.routes.settings)."""
    queue = core.queue
    return {
        "type": "snapshot",
        "jobs": [job_json(job) for job in queue.jobs],
        "queue": queue_state_json(queue.state),
        "status": status_json(core),
        # Read on the loop: connect() builds the snapshot synchronously, so no event can come
        # between it and the events after it; one small SQLite read (and, from the server, a file
        # check and a registry read).
        "settings": settings_json(core.settings.get()),
    }


def _encode(event: dict) -> str:
    """An event as strict JSON: NaN or an infinity raises ValueError rather than reaching a page as
    JSON it can't parse (the schemas turn yt-dlp's into null first)."""
    return json.dumps(event, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _key(event: dict) -> object:
    """Events with the same key replace each other while they wait; a fresh object never matches."""
    kind = event.get("type")
    if kind == "job":
        job = event.get("job")
        if isinstance(job, dict) and "id" in job:
            return ("job", job["id"])
    elif kind == "queue":
        return ("queue",)
    elif kind == "settings":
        return ("settings",)
    elif kind == "status":
        return ("status", tuple(sorted(k for k in event if k != "type")))
    elif kind in ("setup", "update"):
        return (kind, event.get("tool"))
    elif kind == "job_removed":
        return ("job_removed", event.get("id"))
    return object()


class _Dropped(Exception):
    pass


class _Client:
    def __init__(self, socket: Socket, hub: Hub):
        self.socket = socket
        self.hub = hub
        self.pending: OrderedDict[object, tuple[str, int]] = OrderedDict()  # key -> (text, its size)
        self.pending_bytes = 0
        self.wake = asyncio.Event()
        self.stop_code: int | None = None
        self.stopped = asyncio.Event()

    def put(self, key: object, text: str, size: int, removed_job: object | None = None) -> None:
        if self.stop_code is not None:
            return
        if removed_job is not None:
            self._drop(("job", removed_job))
        self._drop(key)
        self.pending[key] = (text, size)
        self.pending_bytes += size
        if len(self.pending) > self.hub.max_pending or self.pending_bytes > self.hub.max_pending_bytes:
            log.warning("A page isn't keeping up with Snag's events; dropping its connection")
            self.stop(TRY_AGAIN_LATER)
            return
        self.wake.set()

    def _drop(self, key: object) -> None:
        dropped = self.pending.pop(key, None)
        if dropped is not None:
            self.pending_bytes -= dropped[1]

    def stop(self, code: int) -> None:
        if self.stop_code is None:
            self.stop_code = code
            self.pending.clear()
            self.pending_bytes = 0
            self.stopped.set()

    async def run(self) -> None:
        sender = asyncio.create_task(self._send_loop())
        receiver = asyncio.create_task(self._receive_loop())
        stopper = asyncio.create_task(self.stopped.wait())
        try:
            await asyncio.wait((sender, receiver, stopper), return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (sender, receiver, stopper):
                task.cancel()
            await asyncio.gather(sender, receiver, stopper, return_exceptions=True)
        if sender.done() and not sender.cancelled() and isinstance(sender.exception(), _Dropped):
            self.stop(TRY_AGAIN_LATER)
        if self.stop_code is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.socket.close(code=self.stop_code), CLOSE_TIMEOUT)

    async def _send_loop(self) -> None:
        while True:
            while not self.pending:
                self.wake.clear()
                await self.wake.wait()
            _, (text, size) = self.pending.popitem(last=False)
            self.pending_bytes -= size
            try:
                await asyncio.wait_for(self.socket.send_text(text), self.hub.send_timeout)
            except TimeoutError:
                log.warning("A page took over %.0f s to take an event; dropping its connection", self.hub.send_timeout)
                raise _Dropped from None

    async def _receive_loop(self) -> None:
        while True:
            message = await self.socket.receive()
            if message.get("type") == "websocket.disconnect":
                return


class Hub:
    def __init__(self, snapshot: Callable[[], dict], *, max_pending: int = MAX_PENDING,
                 max_pending_bytes: int = MAX_PENDING_BYTES, send_timeout: float = SEND_TIMEOUT):
        """snapshot: the snapshot event for a new client (built at the moment it joins)."""
        self._snapshot = snapshot
        self.max_pending = max_pending
        self.max_pending_bytes = max_pending_bytes
        self.send_timeout = send_timeout
        self._clients: dict[Socket, _Client] = {}
        self._closed = False
        self._detach: list[Callable[[], None]] = []  # stop listening to what attach() subscribed to

    @classmethod
    def for_core(cls, core: SnagCore, *, settings_json: Callable[[Settings], dict[str, Any]] = settings_to_json,
                 **options: Any) -> Hub:
        """A hub sending core's snapshot (see snapshot_json) and forwarding its queue's and its own events."""
        hub = cls(lambda: snapshot_json(core, settings_json), **options)
        hub.attach(core)
        return hub

    def attach(self, core: SnagCore) -> None:
        """Forward the queue's job, removal and state changes, and core's events (the updater's,
        tool checks). Listeners run on the loop; they only encode and buffer."""
        queue = core.queue

        def on_job(job) -> None:
            if self._clients:
                self.broadcast({"type": "job", "job": job_json(job)})

        def on_removed(job_id: str) -> None:
            if self._clients:
                self.broadcast({"type": "job_removed", "id": job_id})

        def on_state(state) -> None:
            if self._clients:
                self.broadcast({"type": "queue", "queue": queue_state_json(state)})

        queue.subscribe(on_job)
        queue.subscribe_removed(on_removed)
        queue.subscribe_state(on_state)
        core.subscribe_events(self.broadcast)
        self._detach += [
            lambda: [queue.unsubscribe(listener) for listener in (on_job, on_removed, on_state)],
            lambda: core.unsubscribe_events(self.broadcast),
        ]

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def broadcast(self, event: dict) -> None:
        """Queue an event for every client. Never blocks; call it on the loop."""
        if not self._clients:
            return
        text = _encode(event)
        size = len(text.encode("utf-8"))
        key = _key(event)
        removed_job = event.get("id") if event.get("type") == "job_removed" else None
        for client in list(self._clients.values()):
            client.put(key, text, size, removed_job)
            if client.stop_code is not None:
                self._clients.pop(client.socket, None)

    def connect(self, socket: Socket) -> _Client:
        """Register an accepted socket with the snapshot queued first. Synchronous, so no event can
        come between the snapshot and the events after it."""
        client = _Client(socket, self)
        text = _encode(self._snapshot())
        client.put(object(), text, len(text.encode("utf-8")))
        if client.stop_code is None:
            self._clients[socket] = client
        return client

    def disconnect(self, socket: Socket) -> None:
        client = self._clients.pop(socket, None)
        if client is not None:
            client.stop(GOING_AWAY)

    async def serve(self, socket: Socket) -> None:
        """Run one connection until the page goes, it is dropped, or the hub closes."""
        if self._closed:
            with contextlib.suppress(Exception):
                await socket.close(code=GOING_AWAY)
            return
        await socket.accept()
        try:
            client = self.connect(socket)
        except Exception:
            log.exception("Couldn't build the snapshot for a new page")
            with contextlib.suppress(Exception):
                await socket.close(code=INTERNAL_ERROR)
            return
        try:
            await client.run()
        finally:
            if self._clients.get(socket) is client:
                del self._clients[socket]

    async def close(self) -> None:
        """At shutdown: stop listening to the queue and core, end every connection (close code 1001)
        and turn new ones away."""
        self._closed = True
        detach, self._detach = self._detach, []
        for undo in detach:
            try:
                undo()
            except Exception:
                log.exception("Couldn't stop listening to Snag's events")
        clients = list(self._clients.values())
        self._clients.clear()
        for client in clients:
            client.stop(GOING_AWAY)
        await asyncio.sleep(0)
