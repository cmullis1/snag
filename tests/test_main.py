"""snag/__main__.py in-process: options and the test hook, the run (core, server, tray, setup), and
the one way out. The process itself (logging, single instance, --open) is tests/test_main_integration.py."""
import asyncio
import logging
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

import snag.__main__ as main_module
from snag.__main__ import (
    CTRL_BREAK_EVENT,
    CTRL_C_EVENT,
    CTRL_CLOSE_EVENT,
    DEFAULT_PORT,
    PORT_IN_USE,
    SESSION_END_WAIT,
    ConsoleHandler,
    Options,
    Snag,
    bind_port,
    configure_logging,
    core_for,
    parse_options,
    platform_for,
    tools_for,
)
from snag.app import SnagCore
from snag.jobs.queue import JobQueue
from snag.paths import AppPaths
from snag.server.extension import EXTENSION_ID
from tests.helpers import wait_for
from tests.server_env import github, seed_tools, snag_defaults  # noqa: F401 (fixtures)
from tests.test_server_routes import FakePlatform
from tests.updater_env import fake_smoke

REPO = Path(__file__).resolve().parents[1]
EXTENSION = {"origin": f"chrome-extension://{EXTENSION_ID}"}


# ----- options and the test hook -----

def test_open_is_the_only_option_and_unknown_arguments_are_ignored():
    assert parse_options(["--open"], {}) == Options(open_tab=True)
    assert parse_options([], {}) == Options()
    assert parse_options(["--frobnicate", "x"], {}) == Options()  # pythonw has nowhere to print a usage error


def test_the_test_hooks_variables_count_only_when_it_is_on(tmp_path):
    hook_vars = {"SNAG_PORT": "50123", "SNAG_FAKE_TOOLS": str(tmp_path / "fake.py")}
    for off in ({}, {"SNAG_TEST_HOOK": "0"}, {"SNAG_TEST_HOOK": "true"}):
        assert parse_options([], {**hook_vars, **off}) == Options(port=DEFAULT_PORT)
    on = parse_options(["--open"], {**hook_vars, "SNAG_TEST_HOOK": "1"})
    assert on == Options(open_tab=True, port=50123, test_hook=True, fake_tools=tmp_path / "fake.py")
    assert on.url == "http://127.0.0.1:50123/"


def test_tools_are_the_installed_ones_unless_the_hook_fakes_yt_dlp(tmp_path):
    paths = AppPaths(tmp_path / "Snag")
    real = tools_for(Options(), paths)
    assert real.ytdlp == (str(paths.bin_dir / "yt-dlp.exe"),)
    fake = tools_for(Options(test_hook=True, fake_tools=tmp_path / "fake.py"), paths)
    assert fake.ytdlp == (sys.executable, str(tmp_path / "fake.py"))
    assert fake.ffmpeg_dir == paths.bin_dir


# ----- small pieces -----

def test_the_port_is_taken_exclusively():
    first = bind_port(0)
    try:
        port = first.getsockname()[1]
        with pytest.raises(OSError):
            bind_port(port).close()
    finally:
        first.close()
    bind_port(port).close()  # free again once Snag lets go


def test_logging_goes_to_a_rotating_file_in_snags_folder(tmp_path):
    top = logging.getLogger()
    before = list(top.handlers), top.level, threading.excepthook
    try:
        handler = configure_logging(tmp_path / "Snag", console=False)
        assert isinstance(handler, logging.handlers.RotatingFileHandler)
        assert Path(handler.baseFilename) == tmp_path / "Snag" / "snag.log"
        assert handler.maxBytes == 1_000_000 and handler.backupCount == 3
        logging.getLogger("snag.test").info("hello from the test")
        handler.flush()
        assert "hello from the test" in (tmp_path / "Snag" / "snag.log").read_text(encoding="utf-8")
        # httpx logs every request at INFO, with the full URL: release downloads redirect to signed,
        # short-lived storage links that have no business in the log.
        logging.getLogger("httpx").info("GET https://storage.example/file?sig=secret")
        handler.flush()
        assert "sig=secret" not in (tmp_path / "Snag" / "snag.log").read_text(encoding="utf-8")
        # uvicorn logs every WebSocket that opens or closes at INFO (each page, each reconnect).
        logging.getLogger("uvicorn.error").info('127.0.0.1:50000 - "WebSocket /ws" [accepted]')
        handler.flush()
        assert "WebSocket /ws" not in (tmp_path / "Snag" / "snag.log").read_text(encoding="utf-8")
    finally:
        for added in [h for h in top.handlers if h not in before[0]]:
            top.removeHandler(added)
            added.close()
        top.setLevel(before[1])
        threading.excepthook = before[2]
        logging.getLogger("httpx").setLevel(logging.NOTSET)
        logging.getLogger("uvicorn.error").setLevel(logging.NOTSET)
        logging.captureWarnings(False)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DPI awareness")
def test_snag_makes_itself_per_monitor_dpi_aware():
    """In a child process: awareness can be set only once per process."""
    script = (
        "import ctypes, snag.__main__ as m\n"
        "u = ctypes.WinDLL('user32')\n"
        "u.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p\n"
        "u.GetAwarenessFromDpiAwarenessContext.argtypes = (ctypes.c_void_p,)\n"
        "before = u.GetAwarenessFromDpiAwarenessContext(u.GetThreadDpiAwarenessContext())\n"
        "ok = m.make_dpi_aware()\n"
        "after = u.GetAwarenessFromDpiAwarenessContext(u.GetThreadDpiAwarenessContext())\n"
        "print(before, ok, after)\n"
    )
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, cwd=REPO, timeout=60)
    assert done.stdout.split() == ["0", "True", "2"], done.stderr  # unaware -> per-monitor aware


def test_without_dpi_awareness_contexts_snag_runs_on(monkeypatch):
    """Windows 10 before 1703 has no SetProcessDpiAwarenessContext: Snag starts all the same."""
    monkeypatch.setattr(main_module, "_SetProcessDpiAwarenessContext", None)
    assert main_module.make_dpi_aware() is False


def test_message_boxes_under_the_hook_go_to_stderr(capsys):
    main_module.to_stderr("Port 1 is in use")
    assert "Port 1 is in use" in capsys.readouterr().err


def test_the_environment_main_is_given_decides_where_snags_folder_is(monkeypatch, tmp_path):
    """main(environ=...) is read for everything, %LOCALAPPDATA% included, not os.environ."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "not this one"))
    monkeypatch.setattr("snag.platform.instance.acquire_single_instance", lambda name: False)
    environ = {"SNAG_TEST_HOOK": "1", "SNAG_PORT": "50999", "LOCALAPPDATA": str(tmp_path / "this one")}
    assert main_module.main([], environ) == 0
    # A second launch "opens the tab"; under the hook that is a line in %LOCALAPPDATA%.
    assert (tmp_path / "this one" / "snag-test-opened.txt").read_text(encoding="utf-8") == "http://127.0.0.1:50999/\n"
    assert not (tmp_path / "not this one").exists()


async def test_under_the_test_hook_the_updater_never_reaches_github(tmp_path):
    """Check now, Retry setup and Reinstall in a test-hook Snag say so instead of asking GitHub."""
    from snag.updater.releases import UpdaterError
    from snag.updater.service import TEST_MODE_MESSAGE, default_client, no_network_client

    hooked = core_for(Options(test_hook=True, fake_tools=tmp_path / "fake.py"), AppPaths(tmp_path / "Snag"))
    assert hooked._updater_options == {"client_factory": no_network_client}
    assert Path(hooked._defaults.video_dir) == tmp_path / "Snag" / "Videos" / "Snag"
    async with hooked._updater_options["client_factory"]() as client:
        with pytest.raises(UpdaterError, match=TEST_MODE_MESSAGE):
            await client.get("https://api.github.com/")
    plain = core_for(Options(), AppPaths(tmp_path / "Plain"))
    assert plain._updater_options == {} and plain._defaults is None  # UpdaterService's default_client
    assert default_client is not no_network_client


def test_under_the_test_hook_nothing_of_windows_is_touched():
    """No folder dialog opens, Explorer doesn't start, and no Startup shortcut is made or read."""
    inert = platform_for(Options(test_hook=True))
    assert inert.pick_folder(None) is None
    inert.open_in_explorer(Path("C:/"))
    inert.set_start_with_windows(True)
    assert inert.starts_with_windows() is False
    if sys.platform == "win32":
        from snag.platform import peer, startup

        assert platform_for(Options()).starts_with_windows is startup.starts_with_windows
        # Who may connect is still checked for real: it reads, and changes, nothing of the user's.
        assert inert.same_user_peer is peer.same_user_peer


def test_a_second_launch_opens_the_tab_and_exits_0(monkeypatch, tmp_path):
    opened = []
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr("snag.platform.instance.acquire_single_instance", lambda name: name == "nope")
    monkeypatch.setattr(main_module.webbrowser, "open", opened.append)
    monkeypatch.setattr(main_module, "configure_logging", lambda *a, **k: pytest.fail("logged"))
    assert main_module.main([], {}) == 0
    assert opened == ["http://127.0.0.1:47823/"]
    assert not (tmp_path / "Snag").exists()


def test_the_console_handler_asks_to_quit_and_waits_for_a_closing_console():
    asked, finished = [], threading.Event()
    handler = ConsoleHandler(lambda: asked.append(True), finished)
    started = time.monotonic()
    assert handler.handle(CTRL_C_EVENT) is True and asked == [True]  # Ctrl+C: ask, don't wait
    assert handler.handle(CTRL_BREAK_EVENT) is True and asked == [True, True]
    assert time.monotonic() - started < 1
    asked.clear()
    threading.Timer(0.2, finished.set).start()
    started = time.monotonic()
    assert handler.handle(CTRL_CLOSE_EVENT) is True
    assert 0.1 < time.monotonic() - started < 3  # waited for the shutdown, which ended at 0.2 s
    assert asked == [True]
    assert not hasattr(main_module, "CTRL_LOGOFF_EVENT")  # never sent to Snag: see ConsoleHandler


# ----- a whole run, in-process -----


class FakeTray:
    instances: list["FakeTray"] = []

    def __init__(self, on_open, on_pause_toggle, on_quit, *, paused=False, on_session_end=None, log=None):
        self.on_open, self.on_pause_toggle, self.on_quit = on_open, on_pause_toggle, on_quit
        self.on_session_end = on_session_end
        self.paused = [paused]
        self.started = self.stopped = 0
        self.log = log
        FakeTray.instances.append(self)

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1
        if self.log is not None:
            self.log.append("tray.stop")

    def update_paused(self, paused):
        self.paused.append(paused)


class Run:
    """A Snag on an unopened core, on an ephemeral port, with everything Windows faked."""

    def __init__(self, core: SnagCore, sock: socket.socket, **options):
        self.core, self.port = core, sock.getsockname()[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.platform = FakePlatform()
        self.log: list[str] = []
        self.opened: list[str] = []
        self.picker_closed = threading.Event()
        FakeTray.instances = []
        real_close = core.close

        async def close():
            self.log.append("core.close")
            await real_close()

        core.close = close
        options.setdefault("make_tray", lambda *a, **k: FakeTray(*a, **k, log=self.log))
        self.snag = Snag(core, sock, platform=self.platform.hooks(), open_url=self._open,
                         close_picker=self._close_picker, show_message=self.log.append, **options)
        self.task: asyncio.Task | None = None

    def _open(self, url):
        # Opened only once the server is listening: the page must load.
        self.opened_status = httpx.get(url + "api/status", trust_env=False, timeout=10).status_code
        self.opened.append(url)

    def _close_picker(self):
        refused = False
        try:
            socket.create_connection(("127.0.0.1", self.port), timeout=1).close()
        except OSError:
            refused = True
        self.log.append(f"close_picker (new connections refused: {refused})")
        self.picker_closed.set()

    @property
    def tray(self) -> FakeTray:
        return FakeTray.instances[-1]

    async def start(self) -> None:
        self.task = asyncio.create_task(self.snag.run())
        # Up: the server listening and the tray made (right after it starts listening).
        await wait_for(lambda: self.snag._tray is not None or self.task.done())
        assert not self.task.done(), self.task.result()
        assert self.snag._server.started

    async def finish(self, timeout: float = 30) -> int:
        return await asyncio.wait_for(self.task, timeout)

    def client(self, **kwargs) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.base, trust_env=False, timeout=15, headers=EXTENSION, **kwargs)


@pytest.fixture
async def make_run(tmp_path, fake_tools, snag_defaults, github, monkeypatch):
    from tests.queue_env import record_spawns

    procs = record_spawns(monkeypatch)
    runs: list[Run] = []

    def make(**options) -> Run:
        sock = bind_port(0)
        paths = AppPaths(tmp_path / f"Snag{len(runs)}")
        seed_tools(paths)
        core = SnagCore(paths, fake_tools, port=sock.getsockname()[1], defaults=snag_defaults,
                        updater_options=dict(client_factory=github.client, smoke=fake_smoke))
        run = Run(core, sock, **options)
        runs.append(run)
        return run

    try:
        yield make
    finally:
        for run in runs:
            if run.task is not None and not run.task.done():
                run.snag.quit()
                await asyncio.wait_for(run.task, 30)
            await run.core.close()
        assert [p.pid for p in procs if p.returncode is None] == []


async def test_a_run_serves_shows_the_tray_and_quits_in_order(make_run):
    run = make_run(open_tab=True)
    await run.start()
    async with run.client() as http:
        assert (await http.get("/api/status")).status_code == 200
    await wait_for(lambda: run.opened)
    assert run.opened == [run.base + "/"] and run.opened_status == 200
    tray = run.tray
    assert tray.started == 1 and tray.paused == [False]

    tray.on_quit()  # the tray's Quit
    assert await run.finish() == 0
    assert run.log == ["close_picker (new connections refused: True)", "core.close", "tray.stop"]
    assert run.core.conn is None and run.core.queue is None
    assert run.snag.finished.is_set()


async def test_the_end_of_the_windows_session_quits_snag_in_order_and_waits_for_it(make_run):
    """The tray's WM_ENDSESSION, on the tray's thread: Windows may end the process as soon as it
    returns, so it returns once Snag has quit (or after SESSION_END_WAIT)."""
    run = make_run()
    await run.start()
    assert 0 < SESSION_END_WAIT <= 4.5
    ending = asyncio.ensure_future(asyncio.to_thread(run.tray.on_session_end))
    assert await asyncio.wait_for(ending, 10) is True
    assert run.snag.finished.is_set()
    assert await run.finish() == 0
    assert run.log == ["close_picker (new connections refused: True)", "core.close", "tray.stop"]


async def test_quit_is_one_shutdown_however_often_it_is_asked(make_run):
    run = make_run()
    await run.start()
    run.snag.quit()
    run.snag.quit_threadsafe()
    await asyncio.gather(run.snag.shutdown(), run.snag.shutdown())
    assert await run.finish() == 0
    assert run.log == ["close_picker (new connections refused: True)", "core.close", "tray.stop"]
    assert run.tray.stopped == 1


async def test_the_tray_follows_the_queues_paused_state_and_drives_it(make_run):
    run = make_run()
    await run.start()
    tray = run.tray
    queue = run.core.queue
    async with run.client() as http:  # paused from the UI
        assert (await http.post("/api/queue/pause")).status_code == 200
        assert tray.paused[-1] is True
        assert (await http.post("/api/queue/resume")).status_code == 200
        assert tray.paused[-1] is False
    tray.on_pause_toggle(True)  # the tray's Pause all downloads
    assert queue.paused and tray.paused[-1] is True
    tray.on_pause_toggle(False)
    assert not queue.paused and tray.paused[-1] is False
    await tray.on_open()
    assert run.opened == [run.base + "/"]


async def test_an_open_folder_dialog_does_not_hold_up_quit(make_run):
    run = make_run()
    dialog_open = threading.Event()

    def pick_folder(initial):  # a dialog the user leaves open, until Snag closes it
        dialog_open.set()
        assert run.picker_closed.wait(20)
        return None

    run.platform.pick_folder = pick_folder
    run.snag._platform = run.platform.hooks()
    await run.start()
    async with run.client() as http:
        picking = asyncio.create_task(http.post("/api/pick-folder", json={}))
        await wait_for(dialog_open.is_set)
        started = time.monotonic()
        run.tray.on_quit()
        answer = await asyncio.wait_for(picking, 20)
        assert await run.finish() == 0
    assert time.monotonic() - started < 10
    assert answer.status_code == 200 and answer.json() == {"path": None}
    assert run.log == ["close_picker (new connections refused: True)", "core.close", "tray.stop"]


async def test_quit_while_starting_cancels_the_start(make_run, monkeypatch):
    real_start = JobQueue.start
    starting = asyncio.Event()

    async def slow_start(self, *args, **kwargs):
        starting.set()
        await asyncio.sleep(5)
        return await real_start(self, *args, **kwargs)

    monkeypatch.setattr(JobQueue, "start", slow_start)
    run = make_run(open_tab=True)
    run.task = asyncio.create_task(run.snag.run())
    await asyncio.wait_for(starting.wait(), 10)
    run.snag.quit_threadsafe()  # Ctrl+C
    assert await run.finish(10) == 0
    assert run.snag._server is None and run.opened == [] and FakeTray.instances == []
    assert run.core.conn is None
    assert "core.close" in run.log and not any(entry.startswith("Snag") for entry in run.log)


async def test_quit_while_the_server_starts_waits_for_it_to_start_then_stops_it(make_run, monkeypatch):
    """uvicorn is told to exit only once it has started, so its own shutdown (the pages' WebSockets
    closed, the hub's lifespan end) always runs."""
    real_server = main_module._uvicorn_server
    starting = asyncio.Event()
    exit_asked_when_started: list[bool] = []

    def slow_server(app, port):
        server = real_server(app, port)
        real_startup = server.startup

        async def startup(sockets=None):
            starting.set()
            await asyncio.sleep(0.5)
            await real_startup(sockets=sockets)

        server.startup = startup

        class Watched(type(server)):
            @property
            def should_exit(self):
                return self.__dict__.get("_watched_exit", False)

            @should_exit.setter
            def should_exit(self, value):
                if value:
                    exit_asked_when_started.append(self.started)
                self.__dict__["_watched_exit"] = value

        server.__class__ = Watched
        return server

    monkeypatch.setattr(main_module, "_uvicorn_server", slow_server)
    run = make_run()
    run.task = asyncio.create_task(run.snag.run())
    await asyncio.wait_for(starting.wait(), 10)
    run.snag.quit()
    assert await run.finish() == 0
    assert exit_asked_when_started and all(exit_asked_when_started)
    assert run.snag._serving.done() and FakeTray.instances == []


async def test_setup_and_the_daily_check_are_under_way_before_the_server_answers(make_run, monkeypatch):
    """So the UI's Retry setup can't start "setup" first, leaving the daily check waiting forever for
    a setup of Snag's own that never ran."""
    run = make_run()
    seen = []
    real_start_server = Snag._start_server

    async def start_server(self):
        seen.append((self.core.background_running("setup"), self.core.background_running("daily-update-check")))
        await real_start_server(self)

    async def setup_tools():
        await asyncio.Event().wait()

    monkeypatch.setattr(Snag, "_start_server", start_server)
    monkeypatch.setattr(run.core, "setup_tools", setup_tools)
    await run.start()
    assert seen == [(True, True)]


async def test_a_startup_error_is_shown_and_exits_1(make_run):
    run = make_run()
    run.core.paths.root.rename(run.core.paths.root.with_name("moved"))
    run.core.paths.root.write_text("a file where Snag's folder should be")
    run.task = asyncio.create_task(run.snag.run())
    assert await run.finish() == 1
    assert run.log[0].startswith("Snag couldn't create its folder")
    assert run.snag._server is None and FakeTray.instances == []


async def test_setup_runs_as_the_shared_background_task_then_the_daily_check(make_run, monkeypatch):
    run = make_run()
    setup_calls, daily_calls = [], []
    release = asyncio.Event()

    async def setup_tools():
        setup_calls.append(True)
        await release.wait()
        return True

    async def run_daily(settings):
        daily_calls.append(settings)
        await asyncio.Event().wait()

    monkeypatch.setattr(run.core, "setup_tools", setup_tools)
    await run.start()
    monkeypatch.setattr(run.core.updater, "run_daily", run_daily)
    await wait_for(lambda: setup_calls)
    assert run.core.background_running("setup")
    async with run.client() as http:  # the UI's Retry setup while it runs: not a second one
        retry = await http.post("/api/setup/retry")
    assert retry.status_code == 202 and retry.json()["started"] is False
    await asyncio.sleep(0.1)
    assert setup_calls == [True] and daily_calls == []  # the check waits for setup
    release.set()
    await wait_for(lambda: daily_calls)
    assert daily_calls == [run.core.settings]


async def test_snag_never_makes_the_startup_shortcut_on_its_own(make_run):
    """Only setup.ps1 and the Settings toggle make it; with none there, Settings say so."""
    run = make_run(open_tab=True)
    run.platform.started = False
    await run.start()
    async with run.client() as http:
        assert (await http.get("/api/settings")).json()["start_with_windows"] is False
    await asyncio.sleep(0.2)
    run.snag.quit()
    assert await run.finish() == 0
    assert run.platform.startup == []


async def test_the_quit_route_exists_only_under_the_test_hook(make_run):
    plain = make_run()
    await plain.start()
    async with plain.client() as http:
        assert (await http.post("/api/quit")).status_code in (404, 405)
    assert not plain.task.done()
    plain.snag.quit()
    await plain.finish()

    hooked = make_run(test_hook=True)
    await hooked.start()
    async with hooked.client() as http:
        answer = await http.post("/api/quit")
    assert answer.status_code == 202 and answer.json() == {"quitting": True}
    assert await hooked.finish() == 0
    assert hooked.log == ["close_picker (new connections refused: True)", "core.close", "tray.stop"]


def test_a_message_on_a_narrow_stderr_keeps_what_it_can(monkeypatch):
    import io

    from snag.__main__ import to_stderr

    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stderr", io.TextIOWrapper(raw, encoding="cp437"))
    to_stderr("Setting up Snag…")
    assert raw.getvalue().decode("cp437").strip() == "Snag: Setting up Snag?"


async def test_the_port_in_use_is_a_plain_message(tmp_path, monkeypatch):
    taken = bind_port(0)
    shown = []
    try:
        port = taken.getsockname()[1]
        options = Options(port=port, test_hook=True, fake_tools=tmp_path / "fake.py")
        code = await main_module.run(options, AppPaths(tmp_path / "Snag"), show_message=shown.append,
                                     open_url=lambda url: None)
    finally:
        taken.close()
    assert code == 1
    assert shown == [f"Port {port} is in use: another program, or Snag running for another Windows user, "
                     "is using it. Quit the other program, or quit Snag in that user's account (or sign them out of "
                     "Windows), then start Snag again."]
    assert not (tmp_path / "Snag").exists()  # nothing else was done


def test_the_readme_quotes_the_port_in_use_message() -> None:
    """The README's troubleshooting entry quotes the message box word for word (lines rewrapped)."""
    readme = Path(__file__).resolve().parent.parent / "README.md"
    message = PORT_IN_USE.format(port=DEFAULT_PORT)
    assert message in " ".join(readme.read_text(encoding="utf-8").split())
