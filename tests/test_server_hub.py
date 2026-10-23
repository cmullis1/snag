"""The WebSocket hub: snapshot on connect, ordered fan-out, coalescing, dropping slow clients, and the
events it forwards from the queue, the updater and SnagCore."""
import asyncio
import json

import pytest

from snag.jobs.models import JobState
from snag.server.hub import Hub, status_json
from snag.store.settings import settings_to_json
from tests.helpers import make_spec, wait_for
from tests.server_env import core, github, snag_defaults  # noqa: F401 (fixtures)


class FakeSocket:
    """Stands in for Starlette's WebSocket. With blocked, send_text waits until unblock()."""

    def __init__(self, *, blocked: bool = False):
        self.sent: list[dict] = []
        self.incoming: asyncio.Queue[dict] = asyncio.Queue()
        self.accepted = False
        self.close_code: int | None = None
        self.gate = asyncio.Event()
        if not blocked:
            self.gate.set()

    async def accept(self) -> None:
        self.accepted = True

    async def send_text(self, text: str) -> None:
        await self.gate.wait()
        self.sent.append(json.loads(text))

    async def receive(self) -> dict:
        return await self.incoming.get()

    async def close(self, code: int = 1000) -> None:
        self.close_code = code

    def unblock(self) -> None:
        self.gate.set()

    def leave(self) -> None:
        self.incoming.put_nowait({"type": "websocket.disconnect", "code": 1001})

    def types(self) -> list[str]:
        return [event["type"] for event in self.sent]


SNAPSHOT = {"type": "snapshot", "jobs": [], "queue": {}, "status": {}}


@pytest.fixture
async def hub():
    hub = Hub(lambda: dict(SNAPSHOT), max_pending=50, send_timeout=5.0)
    yield hub
    await hub.close()


def serve(hub: Hub, socket: FakeSocket) -> asyncio.Task:
    return asyncio.create_task(hub.serve(socket))


def job_event(job_id: str, percent: float) -> dict:
    return {"type": "job", "job": {"id": job_id, "percent": percent}}


async def test_a_client_gets_the_snapshot_first(hub):
    socket = FakeSocket()
    task = serve(hub, socket)
    await wait_for(lambda: socket.sent)
    assert socket.accepted
    assert socket.sent[0] == SNAPSHOT
    socket.leave()
    await task


async def test_two_clients_get_the_same_events_in_order(hub):
    first, second = FakeSocket(), FakeSocket()
    tasks = [serve(hub, first), serve(hub, second)]
    await wait_for(lambda: hub.client_count == 2)
    events = [
        job_event("a", 1), {"type": "queue", "queue": {"running": 1}}, job_event("b", 5),
        {"type": "job_removed", "id": "c"}, {"type": "setup", "tool": "yt-dlp", "phase": "done"},
        {"type": "status", "updater": {"x": 1}},
    ]
    for event in events:
        hub.broadcast(event)
    await wait_for(lambda: len(first.sent) == len(second.sent) == 1 + len(events))
    assert first.sent == second.sent == [SNAPSHOT, *events]
    first.leave()
    second.leave()
    await asyncio.gather(*tasks)


async def test_progress_bursts_are_coalesced_per_job(hub):
    socket = FakeSocket(blocked=True)
    task = serve(hub, socket)
    await wait_for(lambda: hub.client_count == 1)
    await asyncio.sleep(0.05)  # the snapshot is being sent, and stuck
    for percent in range(1, 101):
        hub.broadcast(job_event("a", percent))
    hub.broadcast({"type": "queue", "queue": {"running": 1}})
    hub.broadcast(job_event("b", 3))
    hub.broadcast(job_event("a", 100.5))
    socket.unblock()
    await wait_for(lambda: len(socket.sent) == 4)
    # The latest of each job, after what came between (so nothing arrives out of date).
    assert socket.sent == [SNAPSHOT, {"type": "queue", "queue": {"running": 1}}, job_event("b", 3), job_event("a", 100.5)]
    socket.leave()
    await task


async def test_a_removed_job_drops_its_pending_update(hub):
    socket = FakeSocket(blocked=True)
    task = serve(hub, socket)
    await wait_for(lambda: hub.client_count == 1)
    await asyncio.sleep(0.05)
    hub.broadcast(job_event("a", 50))
    hub.broadcast({"type": "job_removed", "id": "a"})
    socket.unblock()
    await wait_for(lambda: len(socket.sent) == 2)
    await asyncio.sleep(0.05)
    assert socket.sent == [SNAPSHOT, {"type": "job_removed", "id": "a"}]
    socket.leave()
    await task


async def test_status_events_coalesce_by_what_they_carry(hub):
    socket = FakeSocket(blocked=True)
    task = serve(hub, socket)
    await wait_for(lambda: hub.client_count == 1)
    await asyncio.sleep(0.05)
    hub.broadcast({"type": "status", "updater": {"n": 1}})
    hub.broadcast({"type": "status", "tools": {"n": 1}})
    hub.broadcast({"type": "status", "updater": {"n": 2}})
    hub.broadcast({"type": "setup", "tool": "ffmpeg", "done": 1})
    hub.broadcast({"type": "setup", "tool": "deno", "done": 1})
    hub.broadcast({"type": "setup", "tool": "ffmpeg", "done": 2})
    socket.unblock()
    await wait_for(lambda: len(socket.sent) == 5)
    assert socket.sent[1:] == [
        {"type": "status", "tools": {"n": 1}}, {"type": "status", "updater": {"n": 2}},
        {"type": "setup", "tool": "deno", "done": 1}, {"type": "setup", "tool": "ffmpeg", "done": 2},
    ]
    socket.leave()
    await task


async def test_a_client_that_never_reads_is_dropped_without_slowing_the_others():
    hub = Hub(lambda: dict(SNAPSHOT), max_pending=10, send_timeout=60.0)
    stuck, healthy = FakeSocket(blocked=True), FakeSocket()
    tasks = [serve(hub, stuck), serve(hub, healthy)]
    await wait_for(lambda: hub.client_count == 2)
    for n in range(30):  # distinct jobs: nothing to coalesce
        hub.broadcast(job_event(f"job{n}", 1))
        await asyncio.sleep(0)
    await wait_for(lambda: hub.client_count == 1)
    assert stuck.close_code == 1013  # "try again later": the UI reconnects and gets a fresh snapshot
    await wait_for(lambda: len(healthy.sent) == 31)
    assert [e["job"]["id"] for e in healthy.sent[1:]] == [f"job{n}" for n in range(30)]
    await tasks[0]  # the stuck client's serve() has returned
    healthy.leave()
    await tasks[1]
    await hub.close()


async def test_a_client_whose_buffer_grows_too_big_is_dropped():
    hub = Hub(lambda: dict(SNAPSHOT), max_pending=1000, max_pending_bytes=2000, send_timeout=60.0)
    stuck, healthy = FakeSocket(blocked=True), FakeSocket()
    tasks = [serve(hub, stuck), serve(hub, healthy)]
    await wait_for(lambda: hub.client_count == 2)
    for _ in range(50):  # one subject, replaced: its size counts once
        hub.broadcast({"type": "job", "job": {"id": "same", "title": "é" * 400}})
        await asyncio.sleep(0)
    await asyncio.sleep(0.05)
    assert hub.client_count == 2
    for n in range(5):  # five distinct subjects of about 800 bytes each (UTF-8) are too many
        hub.broadcast({"type": "job", "job": {"id": f"job{n}", "title": "é" * 400}})
        for _ in range(3):  # the healthy page takes each in turn
            await asyncio.sleep(0)
    await wait_for(lambda: hub.client_count == 1)
    assert stuck.close_code == 1013
    await tasks[0]
    await wait_for(lambda: len(healthy.sent) >= 6)
    healthy.leave()
    await tasks[1]
    await hub.close()


async def test_a_snapshot_too_big_to_buffer_closes_that_connection():
    hub = Hub(lambda: {**SNAPSHOT, "jobs": ["x" * 5000]}, max_pending_bytes=1000)
    socket = FakeSocket()
    await asyncio.wait_for(hub.serve(socket), 5)
    assert socket.close_code == 1013 and hub.client_count == 0 and socket.sent == []


async def test_settings_events_coalesce(hub):
    socket = FakeSocket(blocked=True)
    task = serve(hub, socket)
    await wait_for(lambda: hub.client_count == 1)
    await asyncio.sleep(0.05)
    hub.broadcast({"type": "settings", "settings": {"max_concurrent": 2}})
    hub.broadcast({"type": "queue", "queue": {"running": 0}})
    hub.broadcast({"type": "settings", "settings": {"max_concurrent": 3}})
    socket.unblock()
    await wait_for(lambda: len(socket.sent) == 3)
    assert socket.sent[1:] == [{"type": "queue", "queue": {"running": 0}},
                               {"type": "settings", "settings": {"max_concurrent": 3}}]
    socket.leave()
    await task


async def test_a_send_that_hangs_drops_the_client():
    hub = Hub(lambda: dict(SNAPSHOT), max_pending=100, send_timeout=0.2)
    stuck = FakeSocket(blocked=True)
    task = serve(hub, stuck)
    await wait_for(lambda: hub.client_count == 1)
    await asyncio.wait_for(task, 5)
    assert hub.client_count == 0 and stuck.close_code == 1013
    hub.broadcast(job_event("a", 1))  # nobody left: harmless
    await hub.close()


async def test_disconnect_cleans_up(hub):
    socket = FakeSocket()
    task = serve(hub, socket)
    await wait_for(lambda: hub.client_count == 1)
    socket.leave()
    await asyncio.wait_for(task, 5)
    assert hub.client_count == 0
    hub.broadcast(job_event("a", 1))


async def test_messages_from_the_client_are_ignored(hub):
    socket = FakeSocket()
    task = serve(hub, socket)
    await wait_for(lambda: socket.sent)
    socket.incoming.put_nowait({"type": "websocket.receive", "text": "hello"})
    hub.broadcast(job_event("a", 1))
    await wait_for(lambda: len(socket.sent) == 2)
    socket.leave()
    await task


async def test_close_ends_every_connection():
    hub = Hub(lambda: dict(SNAPSHOT))
    sockets = [FakeSocket(), FakeSocket()]
    tasks = [serve(hub, s) for s in sockets]
    await wait_for(lambda: hub.client_count == 2)
    await hub.close()
    await asyncio.wait_for(asyncio.gather(*tasks), 5)
    assert [s.close_code for s in sockets] == [1001, 1001]
    late = FakeSocket()
    await asyncio.wait_for(hub.serve(late), 5)  # a client after close is turned away
    assert late.close_code == 1001 and late.sent == []


async def test_a_snapshot_that_fails_closes_only_that_connection():
    def broken():
        raise RuntimeError("bug")

    hub = Hub(broken)
    socket = FakeSocket()
    await asyncio.wait_for(hub.serve(socket), 5)
    assert socket.close_code == 1011 and hub.client_count == 0


# ----- with a real SnagCore -----

async def test_snapshot_and_status_from_the_core(core, tmp_path):
    job = core.queue.add(make_spec("fake://success/snap", tmp_path / "out"))
    await wait_for(lambda: job.state is JobState.DONE)
    core.cookies.put("https://www.youtube.com/", [{"domain": ".youtube.com", "name": "SID", "value": "x"}])
    hub = Hub.for_core(core)
    socket = FakeSocket()
    task = serve(hub, socket)
    await wait_for(lambda: socket.sent)
    snapshot = socket.sent[0]
    assert snapshot["type"] == "snapshot"
    assert [j["id"] for j in snapshot["jobs"]] == [job.id]
    assert snapshot["jobs"][0]["state"] == "done" and snapshot["jobs"][0]["kind"] == "video"
    assert snapshot["queue"]["running"] == 0 and snapshot["queue"]["paused"] is False
    assert set(snapshot) == {"type", "jobs", "queue", "status", "settings"}
    assert snapshot["settings"] == settings_to_json(core.settings.get())
    assert snapshot["settings"]["video_dir"] == core.settings.get().video_dir
    status = snapshot["status"]
    assert status == status_json(core)
    assert set(status) == {"version", "tools", "updater", "youtube_login", "queue"}
    assert status["tools"]["ytdlp"]["present"] is True
    assert set(status["updater"]) == {"setup", "update"}
    assert status["youtube_login"].endswith("Z")
    socket.leave()
    await task
    await hub.close()


async def test_queue_and_core_events_are_forwarded(core, tmp_path):
    hub = Hub.for_core(core)
    socket = FakeSocket()
    task = serve(hub, socket)
    await wait_for(lambda: socket.sent)
    job = core.queue.add(make_spec("fake://success/fwd", tmp_path / "out"))
    # Sent, not just done: a job_removed drops a job update still waiting in the buffer.
    await wait_for(lambda: any(e["type"] == "job" and e["job"]["state"] == "done" for e in socket.sent))
    core.queue.clear_finished()
    core.emit({"type": "setup", "tool": "deno", "phase": "done", "done": 1, "total": 1, "message": None})
    await wait_for(lambda: socket.types()[-1] == "setup")
    job_events = [e["job"] for e in socket.sent if e["type"] == "job"]
    assert job_events[-1]["id"] == job.id and job_events[-1]["state"] == "done"
    assert {"type": "job_removed", "id": job.id} in socket.sent
    queues = [e["queue"] for e in socket.sent if e["type"] == "queue"]
    assert queues and queues[-1]["running"] == 0
    assert socket.sent[-1] == {"type": "setup", "tool": "deno", "phase": "done", "done": 1, "total": 1, "message": None}
    socket.leave()
    await task
    await hub.close()


async def test_updater_status_events_are_forwarded_unchanged(core, github):
    from tests.updater_env import GOOD, publish_ytdlp

    publish_ytdlp(github, GOOD + b"2026.09.30 build")
    hub = Hub.for_core(core)
    socket = FakeSocket()
    task = serve(hub, socket)
    await wait_for(lambda: socket.sent)
    await core.updater.check_for_update()
    final = {"type": "status", "updater": core.updater.status()}
    assert final["updater"]["update"]["available"] == "2026.09.30"
    await wait_for(lambda: socket.sent[-1] == final)
    socket.leave()
    await task
    await hub.close()


async def test_close_stops_listening_to_the_queue_and_core(core, tmp_path):
    queue_listeners = (list(core.queue._listeners), list(core.queue._removed_listeners),
                       list(core.queue._state_listeners))
    core_listeners = list(core._listeners)
    hub = Hub.for_core(core)
    assert len(core._listeners) == len(core_listeners) + 1
    await hub.close()
    assert (core.queue._listeners, core.queue._removed_listeners, core.queue._state_listeners) == queue_listeners
    assert core._listeners == core_listeners
    core.queue.add(make_spec("fake://success/after", tmp_path / "out"))  # nothing reaches the closed hub
    core.emit({"type": "settings", "settings": {}})


async def test_events_are_strict_json(hub):
    socket = FakeSocket()
    task = serve(hub, socket)
    await wait_for(lambda: hub.client_count == 1)
    with pytest.raises(ValueError):
        hub.broadcast({"type": "job", "job": {"id": "a", "percent": float("nan")}})
    hub.broadcast(job_event("a", 1))
    await wait_for(lambda: len(socket.sent) == 2)
    assert socket.sent[1] == job_event("a", 1)
    socket.leave()
    await task
