"""`python -m snag` as a real process, through its test hook (snag/__main__.py): the fake yt-dlp in
place of the real one, its own port, and %LOCALAPPDATA% in tmp_path, so nothing is downloaded and
nothing of the user's is touched. It starts, opens the tab once listening, turns a second launch
into "open the tab", downloads through HTTP and the WebSocket as the extension would, and quits
cleanly through POST /api/quit, leaving no process behind."""
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from websockets.asyncio.client import connect

from snag.__main__ import bind_port
from snag.paths import AppPaths
from snag.server.extension import EXTENSION_ID
from tests.server_env import seed_tools

pytestmark = pytest.mark.slow

REPO = Path(__file__).resolve().parents[1]
FAKE_YTDLP = REPO / "tests" / "fake_ytdlp.py"
EXTENSION = {"origin": f"chrome-extension://{EXTENSION_ID}"}


def children_of(pid: int) -> list[str]:
    """Processes still running whose parent was pid (Windows keeps the parent's id after it exits)."""
    done = subprocess.run(
        ["powershell", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
         f"Get-CimInstance Win32_Process -Filter 'ParentProcessId={pid}' | "
         "ForEach-Object { \"$($_.ProcessId) $($_.Name)\" }"],
        capture_output=True, text=True, timeout=60,
    )
    assert done.returncode == 0, done.stderr
    return [line for line in done.stdout.splitlines() if line.strip()]


@pytest.fixture
def snag_env(tmp_path):
    local = tmp_path / "LocalAppData"
    seed_tools(AppPaths(local / "Snag"))  # placeholders: no tool counts as missing, nothing is installed
    state = tmp_path / "fake-state"
    state.mkdir()
    probe = bind_port(0)
    port = probe.getsockname()[1]
    probe.close()
    env = {**os.environ, "LOCALAPPDATA": str(local), "SNAG_TEST_HOOK": "1", "SNAG_FAKE_TOOLS": str(FAKE_YTDLP),
           "SNAG_PORT": str(port), "SNAG_FAKE_STATE": str(state), "PYTHONUTF8": "1"}
    return env, local, port


def launch(env, tmp_path, *args, name="snag", **popen) -> subprocess.Popen:
    out = open(tmp_path / f"{name}.out", "wb")
    try:
        return subprocess.Popen([sys.executable, "-m", "snag", *args], cwd=REPO, env=env, stdout=out,
                                stderr=subprocess.STDOUT, **popen)
    finally:
        out.close()


async def wait_until_up(base: str, proc: subprocess.Popen, timeout: float = 45) -> None:
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient(base_url=base, trust_env=False, timeout=5) as http:
        while True:
            assert proc.poll() is None, f"Snag exited early with {proc.returncode}"
            try:
                if (await http.get("/api/status")).status_code == 200:
                    return
            except httpx.TransportError:
                pass
            assert time.monotonic() < deadline, "Snag didn't start listening"
            await asyncio.sleep(0.1)


async def next_job_event(ws, job_id: str) -> dict:
    while True:
        event = json.loads(await ws.recv())
        if event["type"] == "job" and event["job"]["id"] == job_id:
            return event["job"]


async def test_python_m_snag_runs_downloads_and_quits_cleanly(snag_env, tmp_path):
    env, local, port = snag_env
    base = f"http://127.0.0.1:{port}"
    opened = local / "snag-test-opened.txt"
    proc = launch(env, tmp_path, "--open")
    try:
        await wait_until_up(base, proc)
        deadline = time.monotonic() + 15
        while not opened.exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert opened.read_text(encoding="utf-8").splitlines() == [base + "/"]  # --open, once listening

        second = launch(env, tmp_path, name="second")  # Snag is running: this one just opens the tab
        try:
            assert second.wait(30) == 0
        finally:
            if second.poll() is None:
                second.kill()
                second.wait(10)
        assert opened.read_text(encoding="utf-8").splitlines() == [base + "/", base + "/"]

        dest = tmp_path / "Downloads"
        async with httpx.AsyncClient(base_url=base, trust_env=False, timeout=30, headers=EXTENSION) as http:
            info = (await http.post("/api/lookup", json={"url": "fake://video"})).json()
            assert info["type"] == "video" and info["title"] == "Fake Video"
            async with connect(f"ws://127.0.0.1:{port}/ws", origin=EXTENSION["origin"]) as ws:
                snapshot = json.loads(await ws.recv())
                assert snapshot["type"] == "snapshot" and "settings" in snapshot
                created = await http.post("/api/jobs", json={"url": "fake://downloadable/integration",
                                                             "format_id": "299", "dest_dir": str(dest)})
                assert created.status_code == 201, created.text
                job_id = created.json()["id"]
                async with asyncio.timeout(30):
                    while (job := await next_job_event(ws, job_id))["state"] != "done":
                        assert job["state"] not in ("failed", "cancelled"), job
            assert Path(job["file_path"]).is_file() and Path(job["file_path"]).parent == dest

            # The real process never asks GitHub: Check now says why instead.
            checked = await http.post("/api/update/check")
            assert checked.status_code == 200, checked.text
            assert checked.json()["updater"]["update"]["error"] == "Updates aren't available in test mode."

            quitting = await http.post("/api/quit")
            assert quitting.status_code == 202
        assert proc.wait(30) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(10)

    assert children_of(proc.pid) == []
    bind_port(port).close()  # the port is free again
    log = (local / "Snag" / "snag.log").read_text(encoding="utf-8")
    assert "Snag is listening on" in log and "Snag has quit" in log
    # (The placeholder ffmpeg and Deno don't run, which the tool checks log, with why, at INFO.)
    assert " ERROR " not in log and " WARNING " not in log, log


@pytest.mark.skipif(sys.platform != "win32", reason="Windows console control events")
async def test_ctrl_break_quits_the_real_process_in_order(snag_env, tmp_path):
    """Ctrl+Break reaches Snag's console handler (in its own process group, so only Snag gets it),
    which quits the one way, as the tray's Quit does."""
    env, local, port = snag_env
    proc = launch(env, tmp_path, name="break", creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    try:
        await wait_until_up(f"http://127.0.0.1:{port}", proc)
        os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
        assert proc.wait(30) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(10)
    assert children_of(proc.pid) == []
    bind_port(port).close()
    log = (local / "Snag" / "snag.log").read_text(encoding="utf-8")
    assert "Snag is quitting" in log and "Snag has quit" in log
    assert " ERROR " not in log and " WARNING " not in log, log
