import asyncio
import json
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest

import snag.updater.install as install_module
from snag.jobs.models import JobState
from snag.paths import AppPaths, Tools
from snag.updater.releases import UpdaterError
from snag.updater.service import (
    CHECK_INTERVAL,
    LOOP_STEP,
    RETRY_AFTER,
    TEST_MODE_MESSAGE,
    UpdaterService,
    is_newer,
    no_network_client,
)
from tests.helpers import make_spec, wait_for
from tests.updater_env import (
    GOOD,
    FakeGitHub,
    fake_smoke,
    publish_all,
    publish_deno,
    publish_ffmpeg,
    publish_ytdlp,
    sha256,
)

# Stands in for bin\yt-dlp.exe when asked for its version: prints what follows GOOD in the installed file,
# so installing a new "yt-dlp.exe" changes the version it reports, as with the real one.
VERSION_SCRIPT = (
    "import sys\n"
    "from pathlib import Path\n"
    "exe = Path(sys.argv[1])\n"
    "if not exe.is_file() or not exe.read_bytes().startswith(b'GOOD-EXE'):\n"
    "    sys.exit('yt-dlp is missing or broken')\n"
    "print(exe.read_bytes()[len(b'GOOD-EXE'):].decode().split()[0])\n"
)


def ytdlp_exe(version: str) -> bytes:
    return GOOD + f"{version} build".encode()


class Events(list):
    def __call__(self, event: dict) -> None:
        json.dumps(event)  # every event must be JSON-safe for the hub
        self.append(event)

    def of(self, kind: str) -> list[dict]:
        return [e for e in self if e["type"] == kind]


@dataclass
class Env:
    paths: AppPaths
    tools: Tools
    github: FakeGitHub
    events: Events
    queue: object
    queue_env: object

    @property
    def bin(self) -> Path:
        return self.paths.bin_dir

    def service(self, *, queue=None, notify=None, **overrides) -> UpdaterService:
        options = dict(client_factory=self.github.client, smoke=fake_smoke)
        options.update(overrides)
        return UpdaterService(self.paths, self.tools, queue or self.queue, notify or self.events, **options)

    def install_current(self, ytdlp_version: str = "2026.08.19", *, ffmpeg=True, deno=True) -> None:
        self.bin.mkdir(parents=True, exist_ok=True)
        if ytdlp_version:
            (self.bin / "yt-dlp.exe").write_bytes(ytdlp_exe(ytdlp_version))
        if ffmpeg:
            (self.bin / "ffmpeg.exe").write_bytes(GOOD + b"old ffmpeg")
            (self.bin / "ffprobe.exe").write_bytes(GOOD + b"old ffprobe")
        if deno:
            (self.bin / "deno.exe").write_bytes(GOOD + b"old deno")


@pytest.fixture
async def env(tmp_path: Path, queue_env) -> Env:
    paths = AppPaths(tmp_path / "Snag")
    script = tmp_path / "version_script.py"
    script.write_text(VERSION_SCRIPT, encoding="utf-8")
    tools = Tools(ytdlp=(sys.executable, str(script), str(paths.bin_dir / "yt-dlp.exe")),
                  ffmpeg_dir=paths.bin_dir, deno=paths.bin_dir / "deno.exe")
    queue = queue_env.make_queue()
    await queue.start()
    return Env(paths, tools, FakeGitHub(), Events(), queue, queue_env)


def files(folder: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(folder.iterdir())} if folder.is_dir() else {}


# ----- first run -----

async def test_first_run_installs_every_tool_while_holding_the_queue(env):
    publish_all(env.github, ytdlp_exe("2026.09.30"))
    held_during = []

    def notify(event):
        held_during.append(env.queue.held)
        env.events(event)

    service = env.service(notify=notify)

    assert await service.ensure_installed() is True

    assert set(files(env.bin)) == {"yt-dlp.exe", "ffmpeg.exe", "ffprobe.exe", "deno.exe"}
    assert all(held_during[:-1]) and held_during  # held from the first event until the tools are in
    assert not env.queue.held
    setup = env.events.of("setup")
    for tool in ("yt-dlp", "ffmpeg", "deno"):
        phases = [e["phase"] for e in setup if e["tool"] == tool]
        assert phases[0] == "downloading" and phases[-3:] == ["verifying", "installing", "done"]
    first = next(e for e in setup if e["tool"] == "yt-dlp" and e["done"])
    assert set(first) == {"type", "tool", "phase", "done", "total", "message"}
    status = service.status()["setup"]
    assert status["state"] == "done" and status["can_retry"] is False
    assert env.events.of("status")[-1]["updater"]["setup"]["state"] == "done"


async def test_progress_events_are_coalesced(env):
    publish_ytdlp(env.github, ytdlp_exe("2026.09.30") + b"x" * 3_000_000)  # ~46 network chunks
    env.install_current(ytdlp_version="", ffmpeg=True, deno=True)
    await env.service().ensure_installed()
    downloading = [e for e in env.events.of("setup") if e["phase"] == "downloading"]
    percents = [e["done"] * 100 // e["total"] for e in downloading if e["total"]]
    assert len(percents) == len(set(percents))  # at most one event per percent
    assert downloading[-1]["done"] == downloading[-1]["total"]


async def test_nothing_missing_means_no_network_and_no_hold(env):
    env.install_current()
    service = env.service()
    assert service.hold_if_missing() is False
    assert await service.ensure_installed() is True
    assert env.github.requests == []
    assert not env.queue.held
    assert service.status()["setup"]["state"] == "done"


async def test_missing_ffprobe_alone_reinstalls_ffmpeg(env):
    env.install_current()
    (env.bin / "ffprobe.exe").unlink()
    publish_ffmpeg(env.github, GOOD + b"new ffmpeg", GOOD + b"new ffprobe")
    service = env.service()
    assert service.missing_tools() == ["ffmpeg"]
    await service.ensure_installed()
    assert files(env.bin)["ffprobe.exe"] == GOOD + b"new ffprobe"


async def test_deno_failure_does_not_hold_the_queue(env):
    publish_ytdlp(env.github, ytdlp_exe("2026.09.30"))
    publish_ffmpeg(env.github)  # no Deno release: GitHub answers 404
    service = env.service()
    assert await service.ensure_installed() is True
    assert not env.queue.held
    setup = service.status()["setup"]
    assert setup["state"] == "done"
    assert setup["tools"]["deno"]["phase"] == "error"
    assert setup["tools"]["deno"]["message"] == "Couldn't find the latest release of denoland/deno."
    assert service.missing_tools() == ["deno"]


async def test_failed_setup_keeps_the_queue_held_until_a_retry_works(env):
    publish_ffmpeg(env.github)
    publish_deno(env.github)
    publish_ytdlp(env.github, ytdlp_exe("2026.09.30"), listed_hash=sha256(b"tampered"))
    service = env.service()

    assert await service.ensure_installed() is False
    assert env.queue.held
    waiting = env.queue.add(make_spec("fake://success", env.queue_env.dest))
    await asyncio.sleep(0.2)
    assert env.queue.get(waiting.id).state is JobState.WAITING
    setup = service.status()["setup"]
    assert setup["state"] == "error" and setup["can_retry"] is True
    assert setup["message"].startswith("Snag couldn't finish setting up.")
    assert "didn't match its published checksum" in setup["tools"]["yt-dlp"]["message"]
    assert set(files(env.bin)) == {"ffmpeg.exe", "ffprobe.exe", "deno.exe"}

    publish_ytdlp(env.github, ytdlp_exe("2026.09.30"))
    assert await service.ensure_installed() is True
    assert not env.queue.held  # one hold, one release
    await wait_for(lambda: env.queue.get(waiting.id).state is JobState.DONE)


async def test_hold_before_start_keeps_restored_jobs_waiting(env):
    publish_all(env.github, ytdlp_exe("2026.09.30"))
    queue = env.queue_env.make_queue()
    service = env.service(queue=queue)
    assert service.hold_if_missing() is True
    assert service.hold_if_missing() is True  # idempotent: still one hold
    await queue.start()
    job = queue.add(make_spec("fake://success", env.queue_env.dest))
    await asyncio.sleep(0.2)
    assert queue.get(job.id).state is JobState.WAITING
    await service.ensure_installed()
    await wait_for(lambda: queue.get(job.id).state is JobState.DONE)
    assert not queue.held


async def test_cancelled_setup_releases_the_queue_and_leaves_no_files(env):
    publish_all(env.github, ytdlp_exe("2026.09.30"))
    entered = asyncio.Event()

    async def stuck_smoke(exe):
        entered.set()
        await asyncio.Event().wait()

    service = env.service(smoke=stuck_smoke)
    task = asyncio.create_task(service.ensure_installed())
    await asyncio.wait_for(entered.wait(), 10)
    assert env.queue.held
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not env.queue.held
    assert files(env.bin) == {}
    assert service.status()["setup"]["state"] == "error"  # not stuck on "running"


async def test_startup_repairs_an_interrupted_swap(env):
    env.install_current()
    os.replace(env.bin / "yt-dlp.exe", env.bin / "yt-dlp.old.exe")
    (env.bin / "yt-dlp.new.exe").write_bytes(b"half staged")
    service = env.service()
    assert await service.ensure_installed() is True
    assert env.github.requests == []
    assert files(env.bin)["yt-dlp.exe"] == ytdlp_exe("2026.08.19")
    assert "yt-dlp.new.exe" not in files(env.bin)


async def test_a_failing_notify_does_not_break_setup(env):
    publish_all(env.github, ytdlp_exe("2026.09.30"))

    def broken(event):
        raise RuntimeError("hub is gone")

    assert await env.service(notify=broken).ensure_installed() is True


# ----- checking for updates -----

@pytest.mark.parametrize(("latest", "current", "newer"), [
    ("2026.09.30", "2026.08.19", True),
    ("2026.08.19", "2026.08.19", False),
    ("2026.08.19", "2026.08.19.233512", False),  # a nightly build is newer than the release it follows
    ("2026.10.01", "2026.09.30.120000", True),
    ("2026.08.19", None, True),
    ("weird-tag", "2026.08.19", True),
    ("weird-tag", "weird-tag", False),
    ("2026.08.19", "2026.08.1²", True),  # a superscript digit is no number: it just differs
    ("2026.08.19", "2026.٨.19", True),  # nor is an Arabic-Indic one
    ("2026..19", "2026.08.19", True),
])
def test_is_newer(latest, current, newer):
    assert is_newer(latest, current) is newer


async def test_check_finds_an_update_and_persists_it(env):
    env.install_current("2026.08.19")
    publish_ytdlp(env.github, ytdlp_exe("2026.09.30"))
    now = [1_800_000_000.0]
    service = env.service(clock=lambda: now[0])

    update = await service.check_for_update()

    assert update["current"] == "2026.08.19"
    assert update["latest"] == "2026.09.30"
    assert update["available"] == "2026.09.30"
    assert update["last_check"] == "2027-01-15T08:00:00Z"
    assert update["error"] is None
    assert env.events.of("status")[-1]["updater"]["update"] == update
    saved = json.loads((env.paths.root / "updater.json").read_text(encoding="utf-8"))
    assert saved["available"] == "2026.09.30" and saved["last_check"] == now[0]

    again = env.service(clock=lambda: now[0])  # the next start of Snag
    assert again.status()["update"] == update


async def test_check_when_up_to_date(env):
    env.install_current("2026.09.30")
    publish_ytdlp(env.github, ytdlp_exe("2026.09.30"))
    update = await env.service().check_for_update()
    assert update["available"] is None
    assert update["current"] == "2026.09.30"


async def test_check_failure_is_reported_not_raised(env):
    env.install_current("2026.08.19")
    env.github.rate_limited_until = 2_000_000_000
    service = env.service()
    update = await service.check_for_update()
    assert update["error"].startswith("GitHub is limiting update checks right now.")
    assert update["last_check"] is None
    assert not (env.paths.root / "updater.json").exists()


async def test_corrupt_state_file_is_ignored(env):
    env.paths.root.mkdir(parents=True, exist_ok=True)
    (env.paths.root / "updater.json").write_text("{not json", encoding="utf-8")
    assert env.service().status()["update"]["last_check"] is None


def write_state(env, last_check_json: str) -> None:
    env.paths.root.mkdir(parents=True, exist_ok=True)
    (env.paths.root / "updater.json").write_text(
        '{"current": "2026.08.19", "latest": "2026.09.30", "available": "2026.09.30", '
        f'"last_check": {last_check_json}}}', encoding="utf-8")


START = 1_800_000_000.0


@pytest.mark.parametrize("last_check", [
    "NaN", "Infinity", "-Infinity", "1e20", "9999999999999", pytest.param("1" + "0" * 400, id="10**400"), "true", "false", "0", "-5",
    '"2026-10-07"', "[]",
    str(START + 2 * 24 * 3600),  # days in the future: a broken clock wrote it
])
async def test_unusable_last_check_ignores_the_state_file(env, last_check):
    env.install_current("2026.08.19")
    publish_ytdlp(env.github, ytdlp_exe("2026.09.30"))
    write_state(env, last_check)
    time_ = FakeTime(START, stop_at=START + 1)
    service = env.service(clock=time_.clock, sleep=time_.sleep)

    update = service.status()["update"]  # never raises
    assert update["last_check"] is None and update["current"] is None
    await run_daily_until_stopped(service, time_, Settings(daily=True))
    assert api_calls(env.github) == 1  # due at once


async def test_last_check_a_little_in_the_future_counts_as_due_now(env):
    env.install_current("2026.08.19")
    publish_ytdlp(env.github, ytdlp_exe("2026.09.30"))
    write_state(env, str(START + 3600))  # the clock was moved back an hour
    time_ = FakeTime(START, stop_at=START + 1)
    service = env.service(clock=time_.clock, sleep=time_.sleep)
    assert service.status()["update"]["last_check"] == "2027-01-15T09:00:00Z"
    await run_daily_until_stopped(service, time_, Settings(daily=True))
    assert api_calls(env.github) == 1


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 1e20, 9999999999999.0, -1.0])
async def test_status_never_raises_on_a_bad_time_in_memory(env, value):
    service = env.service()
    service._update.last_check = value
    assert service.status()["update"]["last_check"] is None
    assert env.events == []


async def test_a_failing_status_does_not_break_setup_or_a_check(env, monkeypatch):
    publish_all(env.github, ytdlp_exe("2026.09.30"))
    service = env.service()
    real_status = service.status

    def status():  # broken whenever an event is being built, fine for the methods' return values
        frame = sys._getframe(1)
        while frame is not None:
            if frame.f_code.co_name == "_send_status":
                raise RuntimeError("status bug")
            frame = frame.f_back
        return real_status()

    monkeypatch.setattr(service, "status", status)
    assert await service.ensure_installed() is True
    assert not env.queue.held
    update = await service.check_for_update()
    assert update["state"] == "idle" and update["available"] is None and update["current"] == "2026.09.30"
    assert env.events.of("status") == []


class FakeTime:
    """A wall clock that only moves when the service sleeps. Sleeping past `stop_at` blocks forever."""

    def __init__(self, start: float, stop_at: float):
        self.now, self.stop_at = start, stop_at
        self.sleeps: list[float] = []
        self.stopped = asyncio.Event()

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if self.now + seconds > self.stop_at:
            self.stopped.set()
            await asyncio.Event().wait()
        self.now += seconds


class Settings:
    def __init__(self, daily: bool):
        self.check_updates_daily = daily

    def get(self):
        return self


async def run_daily_until_stopped(service, time_, settings):
    task = asyncio.create_task(service.run_daily(settings))
    await asyncio.wait_for(time_.stopped.wait(), 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def api_calls(github: FakeGitHub) -> int:
    return sum("api.github.com" in url for url in github.requests)


async def test_daily_check_runs_once_a_day_in_bounded_sleeps(env):
    env.install_current("2026.08.19")
    publish_ytdlp(env.github, ytdlp_exe("2026.09.30"))
    start = 1_800_000_000.0
    time_ = FakeTime(start, stop_at=start + 3 * CHECK_INTERVAL - 1)
    service = env.service(clock=time_.clock, sleep=time_.sleep)

    await run_daily_until_stopped(service, time_, Settings(daily=True))

    assert api_calls(env.github) == 3  # at the start, after 24 h and after 48 h
    assert all(0 < s <= LOOP_STEP for s in time_.sleeps)
    assert service.status()["update"]["available"] == "2026.09.30"


async def test_daily_check_waits_for_a_day_since_the_last_one(env):
    env.install_current("2026.08.19")
    publish_ytdlp(env.github, ytdlp_exe("2026.09.30"))
    start = 1_800_000_000.0
    await env.service(clock=lambda: start - CHECK_INTERVAL + 7200).check_for_update()  # 22 h ago
    env.github.requests.clear()
    time_ = FakeTime(start, stop_at=start + 7200 - 1)
    await run_daily_until_stopped(env.service(clock=time_.clock, sleep=time_.sleep), time_, Settings(daily=True))
    assert api_calls(env.github) == 0
    time_ = FakeTime(start, stop_at=start + 7200 + LOOP_STEP)
    await run_daily_until_stopped(env.service(clock=time_.clock, sleep=time_.sleep), time_, Settings(daily=True))
    assert api_calls(env.github) == 1


async def test_daily_check_respects_the_setting(env):
    env.install_current("2026.08.19")
    publish_ytdlp(env.github, ytdlp_exe("2026.09.30"))
    start = 1_800_000_000.0
    time_ = FakeTime(start, stop_at=start + 3 * CHECK_INTERVAL)
    await run_daily_until_stopped(env.service(clock=time_.clock, sleep=time_.sleep), time_, Settings(daily=False))
    assert env.github.requests == []
    assert all(0 < s <= LOOP_STEP for s in time_.sleeps)


async def test_daily_check_retries_a_failure_after_a_while_not_at_once(env):
    env.install_current("2026.08.19")
    env.github.api_status = 500
    start = 1_800_000_000.0
    time_ = FakeTime(start, stop_at=start + 3 * RETRY_AFTER - 1)
    await run_daily_until_stopped(env.service(clock=time_.clock, sleep=time_.sleep), time_, Settings(daily=True))
    assert api_calls(env.github) == 3  # at the start, then once per RETRY_AFTER


# ----- installing an update -----

async def test_update_waits_for_running_jobs_and_holds_new_ones(env):
    env.install_current("2026.08.19")
    publish_ytdlp(env.github, ytdlp_exe("2026.09.30"))
    service = env.service()
    running = env.queue.add(make_spec("fake://slow", env.queue_env.dest))
    await wait_for(lambda: env.queue.get(running.id).state is JobState.DOWNLOADING)

    task = asyncio.create_task(service.install_update())
    await wait_for(lambda: any(e.get("phase") == "waiting" for e in env.events.of("update")))
    assert env.queue.held
    assert files(env.bin)["yt-dlp.exe"] == ytdlp_exe("2026.08.19")  # not swapped while a job runs
    later = env.queue.add(make_spec("fake://success", env.queue_env.dest))
    await asyncio.sleep(0.2)
    assert env.queue.get(later.id).state is JobState.WAITING
    assert not task.done()

    await env.queue.cancel(running.id)
    update = await asyncio.wait_for(task, 15)

    assert update["message"] == "Updated to 2026.09.30"
    assert update["current"] == "2026.09.30" and update["available"] is None
    assert files(env.bin)["yt-dlp.exe"] == ytdlp_exe("2026.09.30")
    assert set(files(env.bin)) == {"yt-dlp.exe", "ffmpeg.exe", "ffprobe.exe", "deno.exe"}
    assert not env.queue.held
    await wait_for(lambda: env.queue.get(later.id).state is JobState.DONE)
    phases = [e["phase"] for e in env.events.of("update")]
    assert phases[0] == "downloading" and phases[-3:] == ["waiting", "installing", "done"]
    saved = json.loads((env.paths.root / "updater.json").read_text(encoding="utf-8"))
    assert saved["current"] == "2026.09.30" and saved["available"] is None


async def test_a_check_during_an_update_does_not_undo_it(env):
    """A check that read the old version before the swap must not save "update available" after it."""
    env.install_current("2026.08.19")
    publish_ytdlp(env.github, ytdlp_exe("2026.09.30"))
    service = env.service()
    running = env.queue.add(make_spec("fake://slow", env.queue_env.dest))
    await wait_for(lambda: env.queue.get(running.id).state is JobState.DOWNLOADING)
    task = asyncio.create_task(service.install_update())
    await wait_for(lambda: any(e.get("phase") == "waiting" for e in env.events.of("update")))
    calls = api_calls(env.github)

    during = await asyncio.wait_for(service.check_for_update(), 5)  # answers at once, without checking

    assert during["state"] == "installing"
    assert api_calls(env.github) == calls
    await env.queue.cancel(running.id)
    update = await asyncio.wait_for(task, 15)
    assert update["current"] == "2026.09.30" and update["available"] is None and update["state"] == "idle"
    assert service.status()["update"] == update
    saved = json.loads((env.paths.root / "updater.json").read_text(encoding="utf-8"))
    assert saved["current"] == "2026.09.30" and saved["available"] is None


async def test_an_update_waits_for_a_check_under_way(env):
    env.install_current("2026.08.19")
    publish_ytdlp(env.github, ytdlp_exe("2026.09.30"))
    gate, entered = asyncio.Event(), asyncio.Event()
    real_installed_version = UpdaterService.installed_version

    async def slow_version(self):  # only the check's call (the first) is slow
        version = await real_installed_version(self)
        if not entered.is_set():
            entered.set()
            await gate.wait()
        return version

    service = env.service()
    service.installed_version = slow_version.__get__(service)
    check = asyncio.create_task(service.check_for_update())
    await asyncio.wait_for(entered.wait(), 10)
    update_task = asyncio.create_task(service.install_update())
    await asyncio.sleep(0.2)
    assert not any(e.get("phase") == "downloading" for e in env.events.of("update"))  # waits for the check
    gate.set()
    checked = await asyncio.wait_for(check, 10)
    assert checked["available"] == "2026.09.30"
    update = await asyncio.wait_for(update_task, 15)
    assert update["current"] == "2026.09.30" and update["available"] is None


async def test_a_check_during_setup_does_not_check(env):
    publish_all(env.github, ytdlp_exe("2026.09.30"))
    entered = asyncio.Event()
    gate = asyncio.Event()

    async def gated_smoke(exe):
        entered.set()
        await gate.wait()
        return await fake_smoke(exe)

    service = env.service(smoke=gated_smoke)
    setup = asyncio.create_task(service.ensure_installed())
    await asyncio.wait_for(entered.wait(), 10)
    calls = api_calls(env.github)
    update = await asyncio.wait_for(service.check_for_update(), 5)
    assert api_calls(env.github) == calls and update["last_check"] is None
    gate.set()
    assert await asyncio.wait_for(setup, 15) is True


async def test_daily_check_skipped_during_an_install_retries_later_not_at_once(env):
    env.install_current("2026.08.19")
    publish_ytdlp(env.github, ytdlp_exe("2026.09.30"))
    start = 1_800_000_000.0
    time_ = FakeTime(start, stop_at=start + RETRY_AFTER - 1)
    service = env.service(clock=time_.clock, sleep=time_.sleep)
    await service._lock.acquire()  # stands in for an update waiting for running downloads
    try:
        await run_daily_until_stopped(service, time_, Settings(daily=True))
    finally:
        service._lock.release()
    assert api_calls(env.github) == 0
    assert len(time_.sleeps) <= RETRY_AFTER / LOOP_STEP + 1  # it slept, it didn't spin


async def test_failed_download_keeps_the_version_and_never_holds(env):
    env.install_current("2026.08.19")
    publish_ytdlp(env.github, ytdlp_exe("2026.09.30"), listed_hash=sha256(b"tampered"))
    service = env.service()
    holds = []
    real_hold = env.queue.hold
    env.queue.hold = lambda: (holds.append(1), real_hold())

    update = await service.install_update()

    assert update["message"] == "Update didn't work: still on version 2026.08.19"
    assert "didn't match its published checksum" in update["error"]
    assert holds == [] and not env.queue.held
    assert files(env.bin)["yt-dlp.exe"] == ytdlp_exe("2026.08.19")
    assert env.events.of("update")[-1]["phase"] == "error"


async def test_failed_swap_releases_the_queue(env, monkeypatch):
    env.install_current("2026.08.19")
    publish_ytdlp(env.github, ytdlp_exe("2026.09.30"))
    real_replace = os.replace

    def failing(source, target):
        if Path(source).name == "yt-dlp.new.exe":
            raise PermissionError(13, "Access is denied")
        return real_replace(source, target)

    monkeypatch.setattr(install_module.os, "replace", failing)
    update = await env.service().install_update()
    assert update["message"] == "Update didn't work: still on version 2026.08.19"
    assert not env.queue.held
    assert files(env.bin) == {
        "deno.exe": GOOD + b"old deno", "ffmpeg.exe": GOOD + b"old ffmpeg", "ffprobe.exe": GOOD + b"old ffprobe",
        "yt-dlp.exe": ytdlp_exe("2026.08.19"),
    }


async def test_failed_rollback_that_loses_yt_dlp_holds_the_queue_until_repaired(env, monkeypatch):
    env.install_current("2026.08.19")
    publish_ytdlp(env.github, ytdlp_exe("2026.09.30"))
    real_replace = os.replace

    def failing(source, target):  # the swap fails, and so does putting the old one back
        if Path(source).name in ("yt-dlp.new.exe", "yt-dlp.old.exe"):
            raise PermissionError(13, "Access is denied")
        return real_replace(source, target)

    monkeypatch.setattr(install_module.os, "replace", failing)
    service = env.service()
    update = await service.install_update()
    assert update["message"] == "Update didn't work: yt-dlp needs reinstalling"
    assert "yt-dlp.exe" not in files(env.bin)
    assert env.queue.held  # jobs wait instead of failing without yt-dlp
    setup = service.status()["setup"]
    assert setup["state"] == "error" and setup["can_retry"] is True

    monkeypatch.setattr(install_module.os, "replace", real_replace)
    assert await service.ensure_installed() is True  # the retry: the old yt-dlp goes back in place
    assert files(env.bin)["yt-dlp.exe"] == ytdlp_exe("2026.08.19")
    assert not env.queue.held


async def test_failed_reinstall_that_loses_ffprobe_holds_the_queue(env, monkeypatch):
    env.install_current()
    publish_ffmpeg(env.github, GOOD + b"new ffmpeg", GOOD + b"new ffprobe")
    real_replace = os.replace

    def failing(source, target):
        if Path(source).name in ("ffprobe.new.exe", "ffprobe.old.exe"):
            raise PermissionError(13, "Access is denied")
        return real_replace(source, target)

    monkeypatch.setattr(install_module.os, "replace", failing)
    service = env.service()
    result = await service.reinstall("ffmpeg")
    assert result["message"].startswith("Couldn't reinstall ffmpeg")
    assert "ffprobe.exe" not in files(env.bin)
    assert env.queue.held
    monkeypatch.setattr(install_module.os, "replace", real_replace)
    assert await service.ensure_installed() is True
    assert not env.queue.held


async def test_cancelled_update_releases_the_queue_and_discards_the_download(env):
    env.install_current("2026.08.19")
    publish_ytdlp(env.github, ytdlp_exe("2026.09.30"))
    running = env.queue.add(make_spec("fake://slow", env.queue_env.dest))
    await wait_for(lambda: env.queue.get(running.id).state is JobState.DOWNLOADING)
    service = env.service()
    task = asyncio.create_task(service.install_update())
    await wait_for(lambda: any(e.get("phase") == "waiting" for e in env.events.of("update")))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not env.queue.held
    assert "yt-dlp.new.exe" not in files(env.bin)
    assert files(env.bin)["yt-dlp.exe"] == ytdlp_exe("2026.08.19")
    assert service.status()["update"]["state"] == "idle"
    await env.queue.cancel(running.id)


async def test_a_cancel_during_a_successful_swap_still_records_the_new_version(env, monkeypatch):
    env.install_current("2026.08.19")
    publish_ytdlp(env.github, ytdlp_exe("2026.09.30"))
    swapping, may_finish = threading.Event(), threading.Event()
    real_swap = install_module.swap

    def slow_swap(staged):  # runs in the worker thread
        swapping.set()
        assert may_finish.wait(10)
        real_swap(staged)

    monkeypatch.setattr(install_module, "swap", slow_swap)
    service = env.service()
    task = asyncio.create_task(service.install_update())
    await wait_for(swapping.is_set)
    task.cancel()  # Snag closing, say, while the files are being moved
    await asyncio.sleep(0.05)
    may_finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert files(env.bin)["yt-dlp.exe"] == ytdlp_exe("2026.09.30")
    assert not any(name.endswith((".new.exe", ".old.exe")) for name in files(env.bin))
    assert not env.queue.held
    update = service.status()["update"]
    assert update["current"] == "2026.09.30" and update["available"] is None
    assert update["message"] == "Updated to 2026.09.30" and update["state"] == "idle"
    assert env.service().status()["update"]["current"] == "2026.09.30"  # saved for the next start


async def test_update_installs_yt_dlp_when_it_is_missing(env):
    env.install_current(ytdlp_version="")
    publish_ytdlp(env.github, ytdlp_exe("2026.09.30"))
    update = await env.service().install_update()
    assert update["message"] == "Updated to 2026.09.30"


async def test_failed_update_without_a_known_version(env):
    env.install_current(ytdlp_version="")
    (env.bin / "yt-dlp.exe").write_bytes(b"there, but it doesn't say its version")
    env.github.api_status = 500
    update = await env.service().install_update()
    assert update["message"] == "Update didn't work: the current version is still in place"


async def test_failed_update_while_yt_dlp_is_missing_says_it_needs_reinstalling(env):
    env.install_current(ytdlp_version="")
    env.github.api_status = 500
    update = await env.service().install_update()
    assert update["message"] == "Update didn't work: yt-dlp needs reinstalling"


# ----- reinstalling ffmpeg and Deno -----

async def test_reinstall_ffmpeg(env):
    env.install_current()
    publish_ffmpeg(env.github, GOOD + b"new ffmpeg", GOOD + b"new ffprobe")
    result = await env.service().reinstall("ffmpeg")
    assert result["message"] == "Reinstalled ffmpeg"
    assert files(env.bin)["ffmpeg.exe"] == GOOD + b"new ffmpeg"
    assert files(env.bin)["ffprobe.exe"] == GOOD + b"new ffprobe"
    assert not env.queue.held
    assert {e["tool"] for e in env.events.of("update")} == {"ffmpeg"}


async def test_reinstall_deno_failure(env):
    env.install_current()
    publish_deno(env.github, b"BROKEN")
    result = await env.service().reinstall("deno")
    assert result["message"] == "Couldn't reinstall Deno: The new Deno didn't run, so it wasn't installed."
    assert files(env.bin)["deno.exe"] == GOOD + b"old deno"
    assert not env.queue.held


async def test_reinstall_of_a_missing_required_tool_ends_the_setup_hold(env):
    env.install_current(ffmpeg=False)
    service = env.service()
    assert await service.ensure_installed() is False  # no ffmpeg release yet
    assert env.queue.held
    publish_ffmpeg(env.github)
    await service.reinstall("ffmpeg")
    assert not env.queue.held
    assert service.status()["setup"]["state"] == "done"


async def test_reinstall_rejects_other_tools(env):
    with pytest.raises(ValueError):
        await env.service().reinstall("yt-dlp")


# ----- the test hook's client: nothing reaches GitHub -----

async def test_the_no_network_client_sends_nothing_and_says_why():
    async with no_network_client() as client:
        with pytest.raises(UpdaterError, match="test mode"):
            await client.get("https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest")


async def test_under_the_no_network_client_every_updater_step_says_test_mode(env):
    env.install_current(ffmpeg=False)
    service = env.service(client_factory=no_network_client)
    assert TEST_MODE_MESSAGE == "Updates aren't available in test mode."
    assert (await service.check_for_update())["error"] == TEST_MODE_MESSAGE
    assert await service.ensure_installed() is False  # setup's retry
    assert service.status()["setup"]["tools"]["ffmpeg"]["message"] == TEST_MODE_MESSAGE
    env.install_current()
    result = await service.reinstall("deno")
    assert result["message"] == f"Couldn't reinstall Deno: {TEST_MODE_MESSAGE}"
    assert files(env.bin)["deno.exe"] == GOOD + b"old deno"
