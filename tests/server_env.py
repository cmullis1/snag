"""Helpers for the server tests: an in-process WebSocket client for any ASGI app (httpx's ASGI
transport speaks only HTTP), and the headers each kind of caller sends."""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from snag.server.extension import EXTENSION_ID

PORT = 47823
BASE = f"http://127.0.0.1:{PORT}"
TAB = {"origin": BASE}  # the full app tab
EXTENSION = {"origin": f"chrome-extension://{EXTENSION_ID}"}
EVIL = {"origin": "https://evil.example"}


class WsSession:
    """One WebSocket connection to an ASGI app, run in this event loop."""

    def __init__(self, app, path: str = "/ws", headers: dict[str, str | None] | None = None):
        """headers: added to (or, with None, removed from) the app tab's Host and Origin."""
        headers = {"host": f"127.0.0.1:{PORT}", **TAB, **(headers or {})}
        headers = {key: value for key, value in headers.items() if value is not None}
        self.scope = {
            "type": "websocket", "asgi": {"version": "3.0", "spec_version": "2.3"}, "scheme": "ws",
            "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
            "headers": [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()],
            "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", PORT), "subprotocols": [],
            "extensions": {},
        }
        self._incoming: asyncio.Queue[dict] = asyncio.Queue()
        self._outgoing: asyncio.Queue[dict] = asyncio.Queue()
        self._incoming.put_nowait({"type": "websocket.connect"})
        self.task = asyncio.create_task(app(self.scope, self._incoming.get, self._outgoing.put))
        self.close_code: int | None = None

    async def accepted(self, timeout: float = 5.0) -> bool:
        """True once the app accepts; False if it closes the connection instead."""
        message = await asyncio.wait_for(self._outgoing.get(), timeout)
        if message["type"] == "websocket.close":
            self.close_code = message.get("code", 1000)
            return False
        assert message["type"] == "websocket.accept", message
        return True

    async def receive_json(self, timeout: float = 5.0) -> Any:
        message = await asyncio.wait_for(self._outgoing.get(), timeout)
        if message["type"] == "websocket.close":
            self.close_code = message.get("code", 1000)
            raise ConnectionError(f"closed with {self.close_code}")
        return json.loads(message["text"])

    def pending(self) -> list[dict]:
        """Messages sent and not yet received, without waiting."""
        found = []
        while not self._outgoing.empty():
            found.append(self._outgoing.get_nowait())
        return found

    def send_text(self, text: str) -> None:
        self._incoming.put_nowait({"type": "websocket.receive", "text": text})

    async def disconnect(self, timeout: float = 5.0) -> None:
        self._incoming.put_nowait({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(self.task, timeout)


# ----- a SnagCore for server tests -----

TOOL_EXES = ("yt-dlp.exe", "ffmpeg.exe", "ffprobe.exe", "deno.exe")


def seed_tools(paths, names=TOOL_EXES) -> None:
    """Stand-ins for installed tools in bin_dir, so the updater finds nothing missing (downloads run the
    fake yt-dlp from fake_tools; to the updater only the files' presence matters)."""
    from tests.updater_env import GOOD

    paths.bin_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        (paths.bin_dir / name).write_bytes(GOOD + b" placeholder")


@pytest.fixture
def github():
    from tests.updater_env import FakeGitHub

    return FakeGitHub()


@pytest.fixture
def snag_defaults(tmp_path):
    from snag.store.settings import default_settings

    return default_settings(tmp_path / "Videos", tmp_path / "Music")


@pytest.fixture
async def core(tmp_path, fake_tools, snag_defaults, github, monkeypatch):
    """An open SnagCore on tmp_path: fake yt-dlp, tools "installed", a fake GitHub, the tests' own
    Videos and Music folders. Closed at teardown, with no yt-dlp left running."""
    from snag.app import SnagCore
    from snag.paths import AppPaths
    from tests.queue_env import record_spawns
    from tests.updater_env import fake_smoke

    procs = record_spawns(monkeypatch)
    paths = AppPaths(tmp_path / "Snag")
    seed_tools(paths)
    core = SnagCore(paths, fake_tools, port=PORT, defaults=snag_defaults,
                    updater_options=dict(client_factory=github.client, smoke=fake_smoke))
    await core.open()
    try:
        yield core
    finally:
        await core.close()
        assert [p.pid for p in procs if p.returncode is None] == []
