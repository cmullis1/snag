import sqlite3
from pathlib import Path

import pytest

import snag.app as app_module
from snag.app import SnagCore, StartupError
from snag.checks import ToolStatus
from snag.cookies import CookieJar
from snag.info.lookup import LookupService
from snag.info.models import VideoInfo
from snag.jobs.models import JobState
from snag.jobs.queue import JobQueue
from snag.paths import AppPaths
from snag.store.db import NewerDatabaseError, open_db
from snag.store.history import HistoryStore
from snag.store.jobs import JobStore
from snag.store.settings import SettingsStore, default_settings, remember_dir
from snag.updater.service import UpdaterService
from tests.helpers import make_spec, wait_for
from tests.queue_env import record_spawns
from tests.server_env import TOOL_EXES, seed_tools
from tests.updater_env import GOOD, FakeGitHub, fake_smoke, publish_ytdlp


@pytest.fixture
def paths(tmp_path) -> AppPaths:
    return AppPaths(tmp_path / "Snag")


@pytest.fixture
def defaults(tmp_path):
    return default_settings(tmp_path / "Videos", tmp_path / "Music")


@pytest.fixture
def github() -> FakeGitHub:
    return FakeGitHub()


@pytest.fixture
async def make_core(paths, fake_tools, defaults, monkeypatch, github):
    """Builds SnagCores on the same folders, with the tools "installed" unless tools=False and the
    updater talking to a fake GitHub; closes them all, and checks no yt-dlp is left running."""
    procs = record_spawns(monkeypatch)
    cores: list[SnagCore] = []

    def make(*, tools: bool = True) -> SnagCore:
        if tools:
            seed_tools(paths)
        core = SnagCore(paths, fake_tools, defaults=defaults,
                        updater_options=dict(client_factory=github.client, smoke=fake_smoke))
        cores.append(core)
        return core

    try:
        yield make
    finally:
        for core in cores:
            await core.close()
        assert [p.pid for p in procs if p.returncode is None] == []


async def test_open_builds_everything(make_core, paths):
    core = make_core()
    await core.open()
    for folder in (paths.root, paths.bin_dir, paths.work_dir, paths.cookies_dir):
        assert folder.is_dir()
    assert paths.db_file.is_file()
    assert isinstance(core.settings, SettingsStore)
    assert isinstance(core.history, HistoryStore)
    assert isinstance(core.job_store, JobStore)
    assert isinstance(core.cookies, CookieJar)
    assert isinstance(core.lookups, LookupService)
    assert isinstance(core.queue, JobQueue)
    assert isinstance(core.checks, ToolStatus)
    assert core.port == 47823


async def test_settings_use_the_given_defaults(make_core, defaults):
    core = make_core()
    await core.open()
    assert core.settings.get() == defaults


async def test_tool_checks_run_at_open(make_core):
    core = make_core()
    await core.open()
    assert core.checks.ytdlp.present  # the fake yt-dlp answers --version
    assert core.checks.ffmpeg.present is False and core.checks.ffmpeg.message == "ffmpeg is missing"


async def test_the_queue_uses_the_max_concurrent_setting(make_core, paths, defaults):
    conn = open_db(paths.db_file)
    SettingsStore(conn, defaults).update(max_concurrent=2)
    conn.close()
    core = make_core()
    await core.open()
    assert core.queue.state.current_limit == 2


async def test_lookups_go_through_the_fake_and_are_cached(make_core):
    core = make_core()
    await core.open()
    info = await core.lookups.get("fake://video")
    assert isinstance(info, VideoInfo) and info.title == "Fake Video"
    assert await core.lookups.get("fake://video") is info


async def test_lookups_get_cookies_from_the_jar(make_core):
    core = make_core()
    await core.open()
    core.cookies.put("fake://signin", [{"domain": "signin", "name": "SID", "value": "x", "hostOnly": True}])
    info = await core.lookups.get("fake://signin")  # only a lookup with cookies succeeds
    assert info.title == "Fake Video"
    assert list(core.paths.cookies_dir.iterdir()) == []  # the lookup's cookies file is gone


async def test_open_clears_leftover_cookies_files(make_core, paths):
    paths.cookies_dir.mkdir(parents=True)
    leftover = paths.cookies_dir / "0123456789abcdef0123456789abcdef.txt"
    leftover.write_text("secret")
    core = make_core()
    await core.open()
    assert not leftover.exists()


async def test_extra_roots_are_the_folders_and_remembered_destinations(make_core, tmp_path):
    core = make_core()
    await core.open()
    remember_dir(core.settings, tmp_path / "Picked")
    remember_dir(core.settings, tmp_path / "Videos" / "Snag")  # the video folder again: listed once
    assert core.extra_roots() == [
        tmp_path / "Videos" / "Snag", tmp_path / "Music" / "Snag", tmp_path / "Picked",
    ]


async def test_start_sweeps_orphans_in_remembered_destinations(make_core, paths, defaults, tmp_path):
    conn = open_db(paths.db_file)
    remember_dir(SettingsStore(conn, defaults), tmp_path / "Picked")
    conn.close()
    orphan = tmp_path / "Picked" / ".snag-tmp" / ("f" * 32)
    orphan.mkdir(parents=True)
    video_orphan = Path(defaults.video_dir) / ".snag-tmp" / ("e" * 32)
    video_orphan.mkdir(parents=True)
    core = make_core()
    await core.open()
    assert not video_orphan.exists()  # a local folder: swept before open() returns
    await core.queue.wait_swept()  # remembered folders: in the background
    assert not orphan.exists()


async def test_open_does_not_wait_for_remembered_folders(make_core, paths, defaults, tmp_path, monkeypatch):
    import asyncio
    import threading

    import snag.jobs.queue as queue_module

    conn = open_db(paths.db_file)
    remember_dir(SettingsStore(conn, defaults), tmp_path / "Unreachable")
    conn.close()
    release = threading.Event()
    real_sweep = queue_module.sweep_orphans

    def sweep(roots, keep_ids):
        if any("Unreachable" in str(root) for root in roots):
            release.wait(15)  # a network share that doesn't answer
        return real_sweep(roots, keep_ids)

    monkeypatch.setattr(queue_module, "sweep_orphans", sweep)
    core = make_core()
    try:
        await asyncio.wait_for(core.open(), 5)
        await asyncio.wait_for(core.close(), 5)
    finally:
        release.set()


async def test_restart_resumes_an_interrupted_job(make_core, tmp_path):
    first = make_core()
    await first.open()
    job = first.queue.add(make_spec("fake://slow-once/restart", tmp_path / "out"))
    await wait_for(lambda: job.state is JobState.DOWNLOADING and (job.percent or 0) > 0)
    await first.close()
    assert job.state is JobState.INTERRUPTED

    second = make_core()
    await second.open()
    resumed = second.queue.get(job.id)
    assert resumed is not None
    await wait_for(lambda: resumed.state is JobState.DONE)
    assert resumed.file_path.is_file()
    assert [entry.status for entry in second.history.list()] == ["done"]


async def test_close_closes_the_database(make_core):
    core = make_core()
    await core.open()
    conn = core.conn
    await core.close()
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


async def test_close_twice_and_close_without_open_are_harmless(make_core):
    never_opened = make_core()
    await never_opened.close()
    core = make_core()
    await core.open()
    await core.close()
    await core.close()


def locked(message: str = "database is locked") -> sqlite3.OperationalError:
    error = sqlite3.OperationalError(message)
    error.sqlite_errorcode = sqlite3.SQLITE_BUSY
    return error


def table_locked() -> sqlite3.OperationalError:
    error = sqlite3.OperationalError("database table is locked")
    error.sqlite_errorcode = sqlite3.SQLITE_LOCKED  # inside Snag's own connection: not another program
    return error


@pytest.mark.parametrize("error, message", [
    (NewerDatabaseError("This database was written by a newer version of Snag (schema 9)"),
     "Snag's database is from a newer version"),
    (locked(), "Snag's database is in use by another program"),
    (sqlite3.OperationalError("database is locked"), "Snag's database is in use by another program"),
    (table_locked(), "Snag couldn't open its database: "),
    (PermissionError(13, "The process cannot access the file", "snag.db", 32),
     "Snag's database is in use by another program"),
    (PermissionError(13, "The process cannot access the file", "snag.db", 33),
     "Snag's database is in use by another program"),
    (OSError(13, "Access is denied"), "Snag couldn't open its database: "),
    (sqlite3.DatabaseError("table jobs has no column named data"), "Snag couldn't open its database: "),
])
async def test_database_failures_become_plain_startup_errors(make_core, monkeypatch, error, message):
    def failing_open_db(path: Path):
        raise error

    monkeypatch.setattr(app_module, "open_db", failing_open_db)
    core = make_core()
    with pytest.raises(StartupError) as raised:
        await core.open()
    assert raised.value.message.startswith(message)
    assert str(raised.value) == raised.value.message
    if message.endswith(": "):
        assert str(error) in raised.value.message


async def test_a_real_newer_database_is_refused(make_core, paths):
    paths.root.mkdir(parents=True)
    conn = sqlite3.connect(paths.db_file)
    conn.execute("PRAGMA user_version = 999")
    conn.close()
    core = make_core()
    with pytest.raises(StartupError) as raised:
        await core.open()
    assert raised.value.message.startswith("Snag's database is from a newer version")


async def test_a_folder_that_cannot_be_made_is_a_startup_error(make_core, paths):
    paths.root.parent.mkdir(parents=True, exist_ok=True)
    paths.root.write_text("a file where Snag's folder should be")
    core = make_core(tools=False)
    with pytest.raises(StartupError) as raised:
        await core.open()
    assert raised.value.message.startswith("Snag couldn't create its folder")


async def test_a_failure_after_the_database_opens_closes_it(make_core, monkeypatch):
    opened = []
    real_open_db = app_module.open_db

    def recording_open_db(path):
        conn = real_open_db(path)
        opened.append(conn)
        return conn

    async def failing_start(self, extra_roots=(), deferred_roots=()):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(app_module, "open_db", recording_open_db)
    monkeypatch.setattr(JobQueue, "start", failing_start)
    core = make_core()
    with pytest.raises(StartupError):
        await core.open()
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute("SELECT 1")


# ----- the updater and the event fan-out -----

class Events(list):
    def __call__(self, event: dict) -> None:
        self.append(event)

    def of(self, kind: str) -> list[dict]:
        return [event for event in self if event["type"] == kind]


async def test_open_builds_the_updater(make_core):
    core = make_core()
    await core.open()
    assert isinstance(core.updater, UpdaterService)
    assert core.updater.status()["setup"]["state"] == "idle"
    assert core.queue.held is False  # every tool is there


async def test_missing_tools_hold_restored_jobs_until_installed(make_core, paths, tmp_path):
    first = make_core()
    await first.open()
    job = first.queue.add(make_spec("fake://slow-once/held", tmp_path / "out"))
    await wait_for(lambda: job.state is JobState.DOWNLOADING and (job.percent or 0) > 0)
    await first.close()

    (paths.bin_dir / "yt-dlp.exe").unlink()  # say the user deleted it
    second = make_core(tools=False)
    await second.open()
    restored = second.queue.get(job.id)
    # Held before the queue started, so the restored job never ran without yt-dlp.
    assert second.queue.held and restored.state is JobState.WAITING and second.queue.state.running == 0

    seed_tools(paths)  # as if setup put it back
    assert await second.setup_tools() is True
    assert second.queue.held is False
    await wait_for(lambda: restored.state is JobState.DONE)


async def test_updater_events_reach_every_subscriber(make_core, github):
    publish_ytdlp(github, GOOD + b"2026.09.30 build")
    core = make_core()
    await core.open()
    first, second = Events(), Events()
    core.subscribe_events(first)
    core.subscribe_events(second)
    await core.updater.check_for_update()
    statuses = first.of("status")
    assert statuses and statuses[-1]["updater"]["update"]["latest"] == "2026.09.30"
    assert second == first


async def test_a_failing_subscriber_does_not_stop_the_others(make_core):
    core = make_core()
    await core.open()

    def broken(event):
        raise RuntimeError("listener bug")

    events = Events()
    core.subscribe_events(broken)
    core.subscribe_events(events)
    core.emit({"type": "status", "youtube_login": None})
    assert events == [{"type": "status", "youtube_login": None}]


async def test_refresh_checks_reports_the_tools(make_core):
    core = make_core()
    await core.open()
    events = Events()
    core.subscribe_events(events)
    checks = await core.refresh_checks()
    assert core.checks is checks
    [event] = events.of("status")
    assert event["tools"]["ytdlp"]["present"] is True
    assert event["tools"]["ffmpeg"] == {"present": False, "version": None, "message": "ffmpeg is missing"}
    assert event["tools"]["ready"] is False


async def test_setup_tools_installs_then_rechecks(make_core, paths, github):
    from tests.updater_env import publish_all
    publish_all(github)
    core = make_core(tools=False)
    await core.open()
    assert core.queue.held
    events = Events()
    core.subscribe_events(events)
    assert await core.setup_tools() is True
    assert all((paths.bin_dir / name).is_file() for name in TOOL_EXES)
    assert core.queue.held is False
    assert {e["tool"] for e in events.of("setup")} == {"yt-dlp", "ffmpeg", "deno"}
    assert "tools" in events.of("status")[-1]  # the checks ran again after setup


async def test_background_tasks_run_once_at_a_time_and_stop_at_close(make_core):
    import asyncio
    core = make_core()
    await core.open()
    started, release = asyncio.Event(), asyncio.Event()
    runs = []

    async def work():
        runs.append(1)
        started.set()
        await release.wait()

    assert core.start_background("job", work) is True
    await started.wait()
    assert core.start_background("job", work) is False  # still running
    assert core.background_running("job")
    release.set()
    await wait_for(lambda: not core.background_running("job"))
    assert core.start_background("job", work) is True
    await wait_for(lambda: len(runs) == 2)
    release.clear()
    await core.close()  # cancels the second run, which waits for release forever
    assert not core.background_running("job")


async def test_a_failing_background_task_is_logged(make_core, caplog):
    core = make_core()
    await core.open()

    async def broken():
        raise RuntimeError("boom")

    core.start_background("broken", broken)
    await wait_for(lambda: not core.background_running("broken"))
    assert "boom" in caplog.text


@pytest.mark.parametrize("run, updater_method, args", [
    ("setup_tools", "ensure_installed", ()),
    ("install_update", "install_update", ()),
    ("reinstall_tool", "reinstall", ("ffmpeg",)),
])
async def test_cancelled_updater_work_skips_the_tool_checks(make_core, monkeypatch, run, updater_method, args):
    import asyncio
    core = make_core()
    await core.open()
    started, rechecks = asyncio.Event(), []

    async def blocked(*_):
        started.set()
        await asyncio.Event().wait()

    async def recheck():
        rechecks.append(1)
        await asyncio.Event().wait()  # would hold close() up forever

    monkeypatch.setattr(core.updater, updater_method, blocked)
    monkeypatch.setattr(core, "refresh_checks", recheck)
    core.start_background("work", lambda: getattr(core, run)(*args))
    await asyncio.wait_for(started.wait(), 2)
    await asyncio.wait_for(core.close(), 2)
    assert rechecks == []


async def test_failed_updater_work_still_rechecks_the_tools(make_core, monkeypatch):
    core = make_core()
    await core.open()
    rechecks = []

    async def broken():
        raise RuntimeError("no network")

    async def recheck():
        rechecks.append(1)

    monkeypatch.setattr(core.updater, "install_update", broken)
    monkeypatch.setattr(core, "refresh_checks", recheck)
    with pytest.raises(RuntimeError):
        await core.install_update()
    assert rechecks == [1]


async def test_unsubscribed_event_listeners_hear_nothing_more(make_core):
    core = make_core()
    await core.open()
    events = []
    core.subscribe_events(events.append)
    core.unsubscribe_events(events.append)
    core.emit({"type": "status", "youtube_login": None})
    assert events == []


async def test_open_twice_is_refused(make_core):
    core = make_core()
    await core.open()
    with pytest.raises(RuntimeError):
        await core.open()


async def _running_job(core, tmp_path):
    job = core.queue.add(make_spec("fake://slow/close", tmp_path / "out"))
    await wait_for(lambda: job.state is JobState.DOWNLOADING)
    return job


def _saved_state(paths, job_id):
    conn = open_db(paths.db_file)
    try:
        return JobStore(conn).load(job_id).state
    finally:
        conn.close()


async def test_concurrent_closes_shut_down_once(make_core, paths, tmp_path):
    import asyncio
    core = make_core()
    await core.open()
    job = await _running_job(core, tmp_path)
    await asyncio.gather(core.close(), core.close())
    assert core.conn is None
    assert _saved_state(paths, job.id) is JobState.INTERRUPTED


async def test_a_cancelled_close_still_shuts_down(make_core, paths, tmp_path):
    import asyncio
    core = make_core()
    await core.open()
    job = await _running_job(core, tmp_path)
    closing = asyncio.create_task(core.close())
    await asyncio.sleep(0)  # the shutdown has begun
    assert not closing.done()
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    await asyncio.wait_for(core.close(), 15)  # the same shutdown, carried on
    assert core.conn is None
    assert _saved_state(paths, job.id) is JobState.INTERRUPTED


async def test_tool_checks_run_alongside_the_queue_start(make_core, monkeypatch):
    import asyncio
    real_check = app_module.check_tools
    real_start = JobQueue.start
    checking, overlapped = [], []

    async def slow_check(tools):
        checking.append(True)
        await asyncio.sleep(0.3)
        checking.append(False)
        return await real_check(tools)

    async def start(self, *args, **kwargs):
        await asyncio.sleep(0.05)
        overlapped.append(checking == [True])
        return await real_start(self, *args, **kwargs)

    monkeypatch.setattr(app_module, "check_tools", slow_check)
    monkeypatch.setattr(JobQueue, "start", start)
    core = make_core()
    await core.open()
    assert overlapped == [True]
    assert core.checks.ytdlp.present


async def test_a_failed_queue_start_stops_the_tool_checks(make_core, monkeypatch):
    import asyncio
    cancelled = []

    async def endless_check(tools):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    async def failing_start(self, *args, **kwargs):
        await asyncio.sleep(0.05)
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(app_module, "check_tools", endless_check)
    monkeypatch.setattr(JobQueue, "start", failing_start)
    core = make_core()
    with pytest.raises(StartupError):
        await asyncio.wait_for(core.open(), 5)
    assert cancelled == [True]


async def test_open_after_close_is_refused(make_core, paths):
    core = make_core()
    await core.close()
    with pytest.raises(RuntimeError):
        await core.open()
    assert not paths.db_file.exists()  # nothing was opened


def _live_snag_tasks():
    import asyncio
    current = asyncio.current_task()
    return [task for task in asyncio.all_tasks() if task is not current and not task.done()]


@pytest.mark.parametrize("delay", [0, 0.01, 0.05, 0.15, 0.4, 1.5])
async def test_a_close_racing_open_cancels_it_and_leaves_nothing_live(make_core, monkeypatch, delay):
    """Quit or Ctrl+C while Snag is still starting: close() cancels the open under way and waits for
    it, so open() ends with CancelledError (never a StartupError, which would show the user a message
    box about a problem that isn't there), and nothing is left running or open."""
    import asyncio
    real_start = JobQueue.start

    async def slow_start(self, *args, **kwargs):
        await asyncio.sleep(0.1)
        return await real_start(self, *args, **kwargs)

    monkeypatch.setattr(JobQueue, "start", slow_start)
    core = make_core()
    opening = asyncio.create_task(core.open())
    await asyncio.sleep(delay)
    await asyncio.wait_for(core.close(), 15)
    outcome = (await asyncio.gather(opening, return_exceptions=True))[0]
    assert outcome is None or isinstance(outcome, asyncio.CancelledError), repr(outcome)
    if delay == 0:
        assert isinstance(outcome, asyncio.CancelledError)
    assert core.conn is None and core.queue is None
    assert _live_snag_tasks() == []
    with pytest.raises(RuntimeError):
        await core.open()
