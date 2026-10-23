"""A scratch Snag for looking at the popup and the full tab in an ordinary browser tab, without the
extension installed and without touching your real Snag:

    .venv/Scripts/python tests/js/extension/harness/serve.py

serves http://127.0.0.1:47823/ (the address the popup talks to, so it must be free: quit Snag first;
it says so and stops when the port is taken)
with its data in %TEMP%\\snag-harness (emptied when it starts, so what a killed run left goes then,
and deleted when it stops), the harness's yt-dlp stand-in (fake_ytdlp.py: recorded lookups, fake
downloads) and nothing installed or downloaded. Besides Snag's own pages it serves, from
the repository as they are on disk:

    /extension/...        the extension folder (the popup's modules, its synced ui/ copy)
    /harness/harness.html the popup page with a stand-in `chrome` (harness.js), e.g.
                          /harness/harness.html?tab=https://www.youtube.com/watch?v=aqz-KE-bpKQ

and two controls for the screenshots: GET /scratch/hello, which says this is the scratch Snag (so
screenshots.py never queues downloads in your real one), and POST
/scratch/updater?mode=normal|setup|setup-error|update, which makes Snag report first-run setup running
or failed, or a yt-dlp update (the status the popup and the tab read). Being a POST, /scratch/updater
passes Snag's guard only with an Origin header it accepts, e.g.
`curl -X POST -H "Origin: http://127.0.0.1:47823" "http://127.0.0.1:47823/scratch/updater?mode=setup"`
(screenshots.py sends it); without one the answer is 403. Ctrl+C stops it and deletes the scratch
folder."""
from __future__ import annotations

import asyncio
import os
import shutil
import socket
import sys
import tempfile
from pathlib import Path

HARNESS = Path(__file__).resolve().parent
REPO = HARNESS.parents[3]
sys.path.insert(0, str(REPO))

PORT = 47823
PORT_IN_USE = f"Port {PORT} is in use: quit Snag first"


def claim_port() -> socket.socket:
    """The scratch Snag's listening socket on 127.0.0.1:PORT, bound before anything else happens; exits
    with PORT_IN_USE when something already answers there or the port can't be had."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(1)
        if probe.connect_ex(("127.0.0.1", PORT)) == 0:
            sys.exit(PORT_IN_USE)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows: no other socket may share the port meanwhile
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    try:
        sock.bind(("127.0.0.1", PORT))
    except OSError:
        sock.close()
        sys.exit(PORT_IN_USE)
    return sock


SCRATCH_NAME = "snag-harness"  # short, so the paths under it stay well inside Windows' limits


def scratch_folder() -> Path:
    """%TEMP%\\snag-harness, emptied: always the same folder, so what a killed run left (its database,
    fake downloads) goes at the next start instead of piling up. Only one harness runs at a time (it
    holds the port), so nothing else is using it."""
    folder = Path(os.environ.get("TEMP") or tempfile.gettempdir()) / SCRATCH_NAME
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    return folder


def updater_override(mode: str) -> dict:
    """What the updater's status says in each mode (merged over the real one)."""
    if mode == "setup":
        return {"setup": {"state": "running", "message": "", "can_retry": False, "tools": {
            "yt-dlp": {"phase": "done", "done": 0, "total": 0, "message": ""},
            "ffmpeg": {"phase": "downloading", "done": 41_000_000, "total": 96_000_000, "message": ""},
            "deno": {"phase": None, "done": 0, "total": 0, "message": ""},
        }}}
    if mode == "setup-error":
        return {"setup": {"state": "error", "can_retry": True, "tools": {},
                          "message": "Snag couldn't download ffmpeg: the connection dropped. Check your internet connection and retry."}}
    if mode == "update":
        return {"update": {"current": "2026.09.01", "latest": "2026.10.01", "available": "2026.10.01",
                           "last_check": None, "state": "idle", "message": "", "error": None}}
    return {}


async def main(sock: socket.socket) -> None:
    import uvicorn
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    from snag.app import SnagCore
    from snag.checks import ToolCheck, ToolStatus
    from snag.paths import AppPaths, Tools
    from snag.server.app import _Files, create_app
    from snag.store.settings import default_settings
    from tests.server_env import seed_tools
    from tests.test_server_routes import FakePlatform
    from tests.updater_env import FakeGitHub, fake_smoke

    scratch = scratch_folder()
    state = scratch / "fake-state"
    state.mkdir()
    os.environ["SNAG_TEST_HOOK"] = "1"  # snag.hook: accept the fake's fake:// addresses too
    os.environ["SNAG_FAKE_STATE"] = str(state)
    bin_dir = scratch / "bin"
    tools = Tools(ytdlp=(sys.executable, str(HARNESS / "fake_ytdlp.py")), ffmpeg_dir=bin_dir, deno=bin_dir / "deno.exe")
    paths = AppPaths(scratch / "Snag")
    seed_tools(paths)
    defaults = default_settings(scratch / "Videos", scratch / "Music")
    core = SnagCore(paths, tools, port=PORT, defaults=defaults,
                    updater_options=dict(client_factory=FakeGitHub().client, smoke=fake_smoke))
    await core.open()
    # The stand-in tools are placeholders that don't run: report them working, as installed ones would.
    core.checks = ToolStatus(ytdlp=ToolCheck(True, "2026.09.01"), ffmpeg=ToolCheck(True, "7.1"),
                             ffprobe=ToolCheck(True, "7.1"), deno=ToolCheck(True, "2.5.0"))

    real_status = core.updater.status
    override: dict = {}

    def status() -> dict:
        answer = real_status()
        for key, value in override.items():
            answer[key] = {**answer.get(key, {}), **value}
        return answer

    core.updater.status = status

    async def set_updater(request: Request) -> JSONResponse:
        mode = request.query_params.get("mode", "normal")
        override.clear()
        override.update(updater_override(mode))
        core.emit({"type": "status", "updater": status()})
        return JSONResponse({"mode": mode})

    async def hello(request: Request) -> JSONResponse:
        return JSONResponse({"scratch": True})

    app = create_app(core, platform=FakePlatform().hooks())
    app.add_route("/scratch/hello", hello, methods=["GET"])
    app.add_route("/scratch/updater", set_updater, methods=["POST"])
    app.mount("/extension", _Files(directory=REPO / "extension", check_dir=True), name="extension")
    app.mount("/harness", _Files(directory=HARNESS, check_dir=True), name="harness")

    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning", ws="websockets-sansio")
    server = uvicorn.Server(config)
    print(f"Scratch Snag on http://127.0.0.1:{PORT}/ (data in {scratch}); Ctrl+C stops it.", flush=True)
    try:
        await server.serve(sockets=[sock])
    finally:
        await core.close()
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    listening = claim_port()
    try:
        asyncio.run(main(listening))
    except KeyboardInterrupt:
        pass
