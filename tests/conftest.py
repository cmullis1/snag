import sys
from pathlib import Path

import pytest

from snag.paths import Tools

FAKE_YTDLP = Path(__file__).parent / "fake_ytdlp.py"


@pytest.fixture
def fake_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Tools:
    state_dir = tmp_path / "fake-state"
    state_dir.mkdir()
    # Children inherit this through snag.proc.child_env, which copies os.environ.
    monkeypatch.setenv("SNAG_FAKE_STATE", str(state_dir))
    # Snag's test hook (snag.hook): among other things, lookups accept the fake's fake:// addresses.
    monkeypatch.setenv("SNAG_TEST_HOOK", "1")
    bin_dir = tmp_path / "bin"
    return Tools(ytdlp=(sys.executable, str(FAKE_YTDLP)), ffmpeg_dir=bin_dir, deno=bin_dir / "deno.exe")


@pytest.fixture
async def queue_env(tmp_path: Path, fake_tools: Tools, monkeypatch: pytest.MonkeyPatch):
    """Makes JobQueues on a fresh database. At teardown every queue is shut down and every yt-dlp process
    a queue started must have exited."""
    from tests.queue_env import QueueEnv, record_spawns

    procs = record_spawns(monkeypatch)
    env = QueueEnv(tmp_path, fake_tools)
    try:
        yield env
    finally:
        await env.close()
        still_running = [proc.pid for proc in procs if proc.returncode is None]
        assert not still_running, f"yt-dlp processes left running: {still_running}"
