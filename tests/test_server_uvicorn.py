"""The app under a real uvicorn on an ephemeral port: HTTP through the guard, the WebSocket hub through
a real handshake (so the websockets dependency is really there), and a clean shutdown with a page
still connected."""
import asyncio
import json
import socket

import httpx
import pytest
import uvicorn
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

import snag
from snag.server.app import create_app
from tests.helpers import wait_for
from tests.server_env import core, github, snag_defaults  # noqa: F401 (fixtures)
from tests.test_server_routes import FakePlatform


@pytest.fixture
async def server(core):
    """(port, uvicorn.Server) serving create_app(core) on 127.0.0.1; stopped at teardown."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    core.port = port  # the guard allows this port's Host and Origin
    servers = []

    async def start(ws: str) -> uvicorn.Server:
        config = uvicorn.Config(create_app(core, platform=FakePlatform().hooks()), host="127.0.0.1", port=port,
                                log_config=None, ws=ws, lifespan="on")
        server = uvicorn.Server(config)
        task = asyncio.create_task(server.serve(sockets=[listener]))
        servers.append((server, task))
        await wait_for(lambda: server.started or task.done())
        assert server.started
        return server

    try:
        yield port, start
    finally:
        for server, task in servers:
            server.should_exit = True
            await asyncio.wait_for(task, 15)
        listener.close()


async def next_event(ws, kind: str, timeout: float = 15.0) -> dict:
    async with asyncio.timeout(timeout):
        while True:
            event = json.loads(await ws.recv())
            if event["type"] == kind:
                return event


# The implementation snag/__main__ should ask for: websockets' sans-I/O one, which is also what "auto"
# picks. ws="websockets" is the legacy implementation, deprecated by both uvicorn and websockets.
WS = "websockets-sansio"


async def test_a_real_server(server, core):
    port, start = server
    await start(WS)
    base = f"http://127.0.0.1:{port}"
    async with httpx.AsyncClient(base_url=base, trust_env=False, timeout=15) as http:
        status = await http.get("/api/status")
        assert status.status_code == 200 and status.json()["version"] == snag.__version__
        assert (await http.get("/api/status", headers={"host": f"evil.com:{port}"})).status_code == 403
        assert (await http.post("/api/queue/pause", headers={"origin": "https://evil.example"})).status_code == 403
        page = await http.get("/")
        assert "frame-ancestors 'none'" in page.headers["content-security-policy"]

        async with connect(f"ws://127.0.0.1:{port}/ws", origin=base) as hub:
            snapshot = json.loads(await hub.recv())
            assert snapshot["type"] == "snapshot" and snapshot["status"]["tools"]["ytdlp"]["present"]
            response = await http.post("/api/jobs", headers={"origin": base},
                                       json={"url": "fake://downloadable/real", "format_id": "299"})
            assert response.status_code == 201
            job_id = response.json()["id"]
            async with asyncio.timeout(20):
                while True:
                    event = await next_event(hub, "job")
                    if event["job"]["id"] == job_id and event["job"]["state"] == "done":
                        break

    with pytest.raises(InvalidStatus) as refused:
        async with connect(f"ws://127.0.0.1:{port}/ws", origin="https://evil.example"):
            pass
    assert refused.value.response.status_code == 403
    with pytest.raises(InvalidStatus):
        async with connect(f"ws://127.0.0.1:{port}/ws"):  # no Origin at all
            pass


async def test_shutdown_closes_connected_pages(server, core):
    port, start = server
    running = await start(WS)
    async with connect(f"ws://127.0.0.1:{port}/ws", origin=f"http://127.0.0.1:{port}") as hub:
        assert json.loads(await hub.recv())["type"] == "snapshot"
        running.should_exit = True
        with pytest.raises(ConnectionClosed):
            async with asyncio.timeout(10):
                while True:
                    await hub.recv()
