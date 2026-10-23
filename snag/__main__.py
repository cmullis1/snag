"""python -m snag [--open]: runs Snag: the engine (SnagCore), the web server on 127.0.0.1:47823
and the tray icon. The shortcuts run it with pythonw.exe, so there is no console window.

Starting:
  1. The process is made per-monitor DPI aware, so the folder dialog and the tray icon are sharp
     (where Windows can: Windows 10 before 1703 can't, and Snag runs on).
  2. One Snag per Windows session (snag.platform.instance): a second launch opens the app tab in the
     default browser and exits 0.
  3. Logging goes to %LOCALAPPDATA%\\Snag\\snag.log (rotating, 1 MB x 3), and to the console if
     there is one.
  4. The port is taken (127.0.0.1 only); if another program has it, a message box says so and Snag
     exits 1.
  5. SnagCore.open(); a StartupError is shown in a message box, as it is worded, and Snag exits 1.
  6. First-run setup starts as the background task "setup" (the one the UI's Retry setup starts, so
     the two never overlap), and the daily update check, which waits for it to end. Both start
     before the server answers, so a Retry setup can never come first.
  7. The web server starts (uvicorn, websockets' sans-I/O implementation).
  8. The tray icon appears; its Pause/Resume item follows the queue's paused state, however it was
     paused. With --open the app tab opens once the server is listening.

Snag never makes the Startup shortcut on its own: setup.ps1 makes it on a fresh install, and the
Settings toggle adds or removes it. Settings show whether it is there and not disabled in Task
Manager (snag.platform.startup.starts_with_windows), never a stored guess.

Quitting goes one way, once, however it is asked for (the tray's Quit, Ctrl+C, Ctrl+Break or
closing the console, Windows ending the session, POST /api/quit under the test hook), also while
Snag is still starting:
  stop accepting requests (once the server has started, so its own shutdown always runs) -> cancel
  an open folder dialog (its waiting request answers null; an open dialog would otherwise keep the
  server waiting) -> let the server finish its requests and close the pages' WebSockets ->
  core.close() (downloads stop; partial files are kept, to resume at the next start) -> remove the
  tray icon -> exit 0.
When Windows ends the session (shutting down, restarting, signing out), the tray's window hears it
(WM_ENDSESSION, snag.platform.tray) and waits, SESSION_END_WAIT at most, for that quit to finish.

Test hook (snag.hook), only when SNAG_TEST_HOOK=1; nothing here reads these otherwise:
  SNAG_FAKE_TOOLS=<script>  runs that Python script with this Python in place of yt-dlp, and skips
                            first-run setup and the update checks, so nothing is downloaded (the
                            test puts placeholder files in bin, so no tool counts as missing)
  SNAG_PORT=<n>             serves on port n instead of 47823 (the single-instance lock follows it)
  POST /api/quit            exists, answers 202 {"quitting": true} and quits as the tray's Quit does
  Also: no tray icon; message boxes are written to stderr instead; "opening the app tab" appends its
  URL to %LOCALAPPDATA%\\snag-test-opened.txt instead of starting a browser; the Videos and Music
  folders a fresh database starts with are inside %LOCALAPPDATA%\\Snag, so the user's own folders
  are never touched; the updater sends nothing to GitHub (core_for: an update check, setup's retry or
  a reinstall says "Updates aren't available in test mode."); and nothing of Windows is
  (platform_for): no folder dialog, no Explorer, no Startup shortcut made or read (Start with
  Windows reads off)."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import logging.handlers
import os
import socket
import sys
import threading
import webbrowser
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from snag import __version__
from snag.app import DEFAULT_PORT, SnagCore, StartupError
from snag.hook import test_hook_on
from snag.paths import AppPaths, Tools

log = logging.getLogger("snag")

LOG_FILE = "snag.log"
LOG_BYTES = 1_000_000
LOG_BACKUPS = 3
GRACEFUL_SHUTDOWN = 5.0  # seconds the server gives requests still running at Quit
SERVER_STOP_TIMEOUT = 15.0  # then it is cancelled
CONSOLE_CLOSE_WAIT = 4.5  # Windows ends a closing console's process after 5 s
SESSION_END_WAIT = 4.0  # how long WM_ENDSESSION waits for the quit; Windows may end Snag after 5 s
SETUP_TASK = "setup"  # snag.server.routes.system.SETUP: the UI's Retry setup uses the same name
DAILY_TASK = "daily-update-check"
TEST_OPENED_FILE = "snag-test-opened.txt"

# One Snag per PC at a time: every Windows user's Snag would serve on the same 127.0.0.1 port.
PORT_IN_USE = ("Port {port} is in use: another program, or Snag running for another Windows user, is using it. "
               "Quit the other program, or quit Snag in that user's account (or sign them out of Windows), "
               "then start Snag again.")


@dataclass(frozen=True)
class Options:
    open_tab: bool = False
    port: int = DEFAULT_PORT
    test_hook: bool = False
    fake_tools: Path | None = None  # the test hook's stand-in for yt-dlp

    @property
    def url(self) -> str:
        return app_url(self.port)


def app_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/"


def parse_options(argv: list[str] | None, environ: Mapping[str, str]) -> Options:
    """The command line, and the test hook's variables when it is on. Unknown arguments are ignored:
    under pythonw there is no console to print a usage error to."""
    parser = argparse.ArgumentParser(prog="python -m snag", add_help=False)
    parser.add_argument("--open", action="store_true", dest="open_tab")
    known, _ = parser.parse_known_args(argv)
    if not test_hook_on(environ):
        return Options(open_tab=known.open_tab)
    port = int(environ["SNAG_PORT"]) if environ.get("SNAG_PORT") else DEFAULT_PORT
    fake = Path(environ["SNAG_FAKE_TOOLS"]) if environ.get("SNAG_FAKE_TOOLS") else None
    return Options(open_tab=known.open_tab, port=port, test_hook=True, fake_tools=fake)


def tools_for(options: Options, paths: AppPaths) -> Tools:
    if options.fake_tools is not None:
        return Tools(ytdlp=(sys.executable, str(options.fake_tools)), ffmpeg_dir=paths.bin_dir,
                     deno=paths.bin_dir / "deno.exe")
    return Tools.from_bin(paths.bin_dir)


# ----- Windows: DPI, message boxes, the console -----

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    # Private WinDLLs so these prototypes don't leak into ctypes.windll for other code.
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    # Windows 10 1703 and later; on older ones Snag runs DPI-unaware (blurry when scaled).
    _SetProcessDpiAwarenessContext = getattr(_user32, "SetProcessDpiAwarenessContext", None)
    if _SetProcessDpiAwarenessContext is not None:
        _SetProcessDpiAwarenessContext.argtypes = (wintypes.HANDLE,)
        _SetProcessDpiAwarenessContext.restype = wintypes.BOOL

    _MessageBoxW = _user32.MessageBoxW
    _MessageBoxW.argtypes = (wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT)
    _MessageBoxW.restype = ctypes.c_int

    _HandlerRoutine = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
    _SetConsoleCtrlHandler = _kernel32.SetConsoleCtrlHandler
    _SetConsoleCtrlHandler.argtypes = (_HandlerRoutine, wintypes.BOOL)
    _SetConsoleCtrlHandler.restype = wintypes.BOOL

    _PER_MONITOR_AWARE_V2 = -4  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
    _MB_ICONERROR = 0x10
    _MB_SETFOREGROUND = 0x10000
    _MB_TOPMOST = 0x40000

CTRL_C_EVENT, CTRL_BREAK_EVENT, CTRL_CLOSE_EVENT = 0, 1, 2


def make_dpi_aware() -> bool:
    """Per-monitor DPI awareness (v2) for the whole process; call before any window exists. False
    when Windows refuses (already set) or can't (too old); Snag runs on, just blurrier."""
    if sys.platform != "win32" or _SetProcessDpiAwarenessContext is None:
        return False
    return bool(_SetProcessDpiAwarenessContext(wintypes.HANDLE(_PER_MONITOR_AWARE_V2)))


def message_box(text: str) -> None:
    """A plain Windows message box (blocks until dismissed)."""
    if sys.platform == "win32":
        _MessageBoxW(None, text, "Snag", _MB_ICONERROR | _MB_SETFOREGROUND | _MB_TOPMOST)
    elif sys.stderr is not None:
        print(text, file=sys.stderr)


def to_stderr(text: str) -> None:
    """A message box's text on stderr (the test hook). A character stderr's code page lacks (an "…",
    say) becomes "?" rather than failing."""
    if sys.stderr is None:
        return
    line = f"Snag: {text}"
    try:
        print(line, file=sys.stderr, flush=True)
    except UnicodeEncodeError:
        print(line.encode("ascii", "replace").decode("ascii"), file=sys.stderr, flush=True)


class ConsoleHandler:
    """Ctrl+C, Ctrl+Break and closing the console all ask Snag to quit, through the same path as the
    tray's Quit. Windows calls the handler on a thread of its own; for a closing console it ends the
    process once the handler returns, so the handler waits (up to CONSOLE_CLOSE_WAIT) for the
    shutdown to finish first.

    Signing out and shutting down never come here: Windows doesn't send those console events to a
    process that has loaded user32 (Snag has, for the tray), and under pythonw there is no console
    at all. The tray's window hears the session end instead (Snag.end_session)."""

    def __init__(self, request_quit: Callable[[], None], finished: threading.Event):
        self._request_quit = request_quit
        self._finished = finished
        self._routine: Any = None  # kept referenced while installed: Windows holds a raw pointer

    def handle(self, event: int) -> bool:
        self._request_quit()
        if event == CTRL_CLOSE_EVENT:
            self._finished.wait(CONSOLE_CLOSE_WAIT)
        return True  # handled: Python's own Ctrl+C handling (KeyboardInterrupt) isn't reached

    def install(self) -> None:
        if sys.platform == "win32":
            self._routine = _HandlerRoutine(self.handle)
            if not _SetConsoleCtrlHandler(self._routine, True):
                log.warning("Couldn't watch the console for Ctrl+C: %s", ctypes.WinError(ctypes.get_last_error()))
                self._routine = None
        else:
            import signal

            signal.signal(signal.SIGINT, lambda *_: self.handle(CTRL_C_EVENT))
            signal.signal(signal.SIGTERM, lambda *_: self.handle(CTRL_C_EVENT))

    def uninstall(self) -> None:
        if sys.platform == "win32" and self._routine is not None:
            _SetConsoleCtrlHandler(self._routine, False)
            self._routine = None


# ----- logging, the port -----

def configure_logging(root: Path, *, console: bool = True) -> logging.Handler:
    """Log INFO and up to root\\snag.log (rotating), and to stderr when there is one. Returns the
    file handler. Raises OSError if the folder or the file can't be made."""
    root.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(root / LOG_FILE, maxBytes=LOG_BYTES, backupCount=LOG_BACKUPS,
                                                   encoding="utf-8", delay=False)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s [%(threadName)s] %(message)s")
    handler.setFormatter(formatter)
    top = logging.getLogger()
    top.setLevel(logging.INFO)
    top.addHandler(handler)
    if console and sys.stderr is not None:
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        top.addHandler(stream)
    # httpx logs every request at INFO with its full URL, and release downloads redirect to signed,
    # short-lived storage links: only its warnings go in the log.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # uvicorn logs each WebSocket opening and closing at INFO: every page, every reconnect.
    logging.getLogger("uvicorn.error").setLevel(logging.WARNING)
    logging.captureWarnings(True)
    threading.excepthook = _log_thread_exception
    return handler


def _log_thread_exception(args: threading.ExceptHookArgs) -> None:
    if args.exc_type is SystemExit:
        return
    log.error("Unexpected error in thread %s", getattr(args.thread, "name", "?"),
              exc_info=(args.exc_type, args.exc_value, args.exc_traceback))


def bind_port(port: int) -> socket.socket:
    """The listening socket's address, 127.0.0.1:port, bound with SO_EXCLUSIVEADDRUSE: no other
    program can bind that same address while Snag runs (as SO_REUSEADDR on Windows would let one).
    It covers 127.0.0.1:port only; another address with the same port (0.0.0.0, ::1) is another
    socket. Raises OSError when it is taken."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if sys.platform == "win32":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind(("127.0.0.1", port))
    except BaseException:
        sock.close()
        raise
    return sock


def _inert_platform() -> Any:
    from snag.server.app import Platform

    def nothing(*args: Any) -> None:
        return None

    if sys.platform == "win32":
        from snag.platform.peer import same_user_peer
    else:
        def same_user_peer(peer: Any, server: Any) -> bool:
            return True

    return Platform(pick_folder=nothing, open_in_explorer=nothing, set_start_with_windows=nothing,
                    starts_with_windows=lambda: False, same_user_peer=same_user_peer)


def platform_for(options: Options) -> Any:
    """What the API may ask of Windows: under the test hook, nothing that shows or changes anything
    (no dialog, no Explorer, no Startup shortcut made or read), while callers' Windows users are
    still checked for real; otherwise Windows' own (None off Windows)."""
    if options.test_hook:
        return _inert_platform()
    if sys.platform != "win32":
        return None
    from snag.server.app import Platform

    return Platform.windows()


def recording_opener(local_app_data: Path) -> Callable[[str], object]:
    """Under the test hook: record the URL instead of starting a browser."""
    def record(url: str) -> bool:
        local_app_data.mkdir(parents=True, exist_ok=True)
        with open(local_app_data / TEST_OPENED_FILE, "a", encoding="utf-8") as file:
            file.write(url + "\n")
        return True

    return record


# ----- running -----

def _uvicorn_server(app: Any, port: int) -> Any:
    import uvicorn

    class Server(uvicorn.Server):
        def capture_signals(self) -> contextlib.AbstractContextManager[None]:
            # Snag handles Ctrl+C itself (ConsoleHandler), so it quits the one way, in order.
            return contextlib.nullcontext()

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, ws="websockets-sansio",
                            lifespan="on", access_log=False, timeout_graceful_shutdown=GRACEFUL_SHUTDOWN)
    return Server(config)


class Snag:
    """One run of Snag on an unopened core: open it, serve, show the tray, quit. Everything Windows
    does is injectable, so tests run it without a window, a browser or a tray icon:

    platform: snag.server.app.Platform (folder dialog, Explorer, Startup shortcut).
    open_url: opens the app tab (webbrowser.open); called off the loop.
    close_picker: cancels an open folder dialog (close_folder_picker); called off the loop.
    make_tray: Tray(on_open, on_pause_toggle, on_quit, paused=..., on_session_end=...), or None for
        no tray icon.
    show_message: tells the user why Snag can't start (a message box)."""

    def __init__(
        self,
        core: SnagCore,
        sock: socket.socket,
        *,
        open_tab: bool = False,
        test_hook: bool = False,
        run_setup: bool = True,
        platform: Any = None,
        open_url: Callable[[str], object] = webbrowser.open,
        close_picker: Callable[[], None] | None = None,
        make_tray: Callable[..., Any] | None = None,
        show_message: Callable[[str], None] = message_box,
    ):
        if close_picker is None:
            from snag.platform.folders import close_folder_picker as close_picker
        self.core = core
        self.url = app_url(core.port)
        self._sock = sock
        self._open_tab_at_start = open_tab
        self._test_hook = test_hook
        self._run_setup = run_setup
        self._platform = platform
        self._open_url = open_url
        self._close_picker = close_picker
        self._make_tray = make_tray
        self._show_message = show_message
        self._quit = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: Any = None
        self._serving: asyncio.Task[None] | None = None
        self._tray: Any = None
        self._opening_tab: asyncio.Future[None] | None = None
        self._setup_finished = asyncio.Event()
        self._shutdown: asyncio.Future[None] | None = None
        self.finished = threading.Event()  # set once the shutdown is complete

    # ----- asking to quit -----

    def attach(self, loop: asyncio.AbstractEventLoop) -> None:
        """The loop Snag runs on, so quit_threadsafe() works before run() starts (the console
        handler is installed first). run() attaches the running loop itself."""
        self._loop = loop

    def quit(self) -> None:
        """Ask Snag to quit (on the loop): the tray's Quit, POST /api/quit."""
        self._quit.set()

    def quit_threadsafe(self) -> None:
        """Ask Snag to quit from any thread (the console handler)."""
        loop = self._loop
        if loop is None:
            return
        with contextlib.suppress(RuntimeError):  # the loop is closed: Snag has already gone
            loop.call_soon_threadsafe(self.quit)

    def end_session(self) -> bool:
        """Windows is ending the session (the tray's WM_ENDSESSION, on the tray's thread): quit, and
        wait for it, SESSION_END_WAIT at most, since Windows may end the process once this returns.
        True when Snag quit in time."""
        log.info("Windows is ending the session")
        self.quit_threadsafe()
        return self.finished.wait(SESSION_END_WAIT)

    # ----- running -----

    async def run(self) -> int:
        """0 after a normal quit (also one asked for while starting); 1 when Snag couldn't start or
        its web server stopped on its own."""
        self.attach(asyncio.get_running_loop())
        try:
            return await self._run()
        except _CantStart:
            return 1
        finally:
            await self.shutdown()

    async def _run(self) -> int:
        if not await self._until_quit_or(self.core.open(), "open"):
            return 0
        if self._run_setup:  # before the server answers: see the module docstring
            self.core.start_background(SETUP_TASK, self._first_setup)
            self.core.start_background(DAILY_TASK, self._daily_checks)
        if not await self._until_quit_or(self._start_server(), "server"):
            return 0
        if self._server is None or not self._server.started:
            self._show_message("Snag's web server couldn't start. See snag.log in Snag's folder for why.")
            return 1
        log.info("Snag is listening on %s", self.url)
        self._start_tray()
        if self._open_tab_at_start:
            self._opening_tab = asyncio.ensure_future(self.open_tab())  # kept: the loop holds tasks weakly
        quitting = asyncio.ensure_future(self._quit.wait())
        try:
            await asyncio.wait((quitting, self._serving), return_when=asyncio.FIRST_COMPLETED)
        finally:
            quitting.cancel()
        if not self._quit.is_set():
            log.error("The web server stopped on its own; Snag is quitting")
            return 1
        return 0

    async def _until_quit_or(self, work: Any, what: str) -> bool:
        """Run work unless a quit comes first (then work is cancelled). True when work finished."""
        task = asyncio.ensure_future(work)
        quitting = asyncio.ensure_future(self._quit.wait())
        try:
            await asyncio.wait((task, quitting), return_when=asyncio.FIRST_COMPLETED)
        finally:
            quitting.cancel()
        if not task.done():
            log.info("Quit while starting (%s)", what)
            if what == "open":
                await self.core.close()  # cancels the open under way, and waits for it
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            return False
        try:
            task.result()
        except StartupError as error:
            log.error("Snag can't start: %s", error.message)
            self._show_message(error.message)
            raise _CantStart from None
        return True

    async def _start_server(self) -> None:
        from snag.server.app import create_app

        app = create_app(self.core, platform=self._platform)
        if self._test_hook:
            from starlette.responses import JSONResponse

            async def quit_route() -> JSONResponse:
                self.quit()
                return JSONResponse({"quitting": True}, status_code=202)

            app.add_api_route("/api/quit", quit_route, methods=["POST"])
        self._server = _uvicorn_server(app, self.core.port)
        self._serving = asyncio.ensure_future(self._server.serve(sockets=[self._sock]))
        while not self._server.started and not self._serving.done():
            await asyncio.sleep(0.02)

    def _start_tray(self) -> None:
        if self._make_tray is None:
            return
        queue = self.core.queue
        try:
            self._tray = self._make_tray(self.open_tab, self._set_paused, self.quit, paused=queue.paused,
                                         on_session_end=self.end_session)
            queue.subscribe_state(self._on_queue_state)
            self._tray.start()
        except Exception:
            log.exception("The tray icon couldn't be shown; quit Snag with Ctrl+C or from Task Manager")

    def _on_queue_state(self, state: Any) -> None:
        if self._tray is not None:
            self._tray.update_paused(state.paused)

    def _set_paused(self, paused: bool) -> None:
        queue = self.core.queue
        if queue is not None:
            queue.pause() if paused else queue.resume()

    async def open_tab(self) -> None:
        """Open the app tab in the default browser (the tray's Open Snag, --open)."""
        try:
            await asyncio.to_thread(self._open_url, self.url)
        except Exception:
            log.exception("Couldn't open %s in the browser", self.url)

    async def _first_setup(self) -> None:
        try:
            await self.core.setup_tools()
        finally:
            self._setup_finished.set()

    async def _daily_checks(self) -> None:
        await self._setup_finished.wait()  # a check during setup would only be put off for an hour
        await self.core.updater.run_daily(self.core.settings)

    # ----- quitting -----

    async def shutdown(self) -> None:
        """Quit (see the module docstring). Runs once; every call waits for that one."""
        self.quit()
        if self._shutdown is None:
            self._shutdown = asyncio.ensure_future(self._shut_down())
        await asyncio.shield(self._shutdown)

    async def _shut_down(self) -> None:
        log.info("Snag is quitting")
        try:
            server = self._server
            if server is not None:
                await self._until_server_started(server)
                server.should_exit = True
                for listener in getattr(server, "servers", ()):  # stop accepting now, not at uvicorn's next tick
                    listener.close()
            try:
                await asyncio.to_thread(self._close_picker)
            except Exception:
                log.exception("Couldn't close the folder dialog")
            if self._serving is not None:
                done, _ = await asyncio.wait((self._serving,), timeout=SERVER_STOP_TIMEOUT)
                if not done:
                    log.warning("The web server didn't stop within %s seconds", SERVER_STOP_TIMEOUT)
                    self._serving.cancel()
                    await asyncio.gather(self._serving, return_exceptions=True)
                elif not self._serving.cancelled() and self._serving.exception() is not None:
                    log.error("The web server failed", exc_info=self._serving.exception())
            self._sock.close()
            if self.core.queue is not None:
                self.core.queue.unsubscribe(self._on_queue_state)
            try:
                await self.core.close()
            except Exception:
                log.exception("Snag's engine didn't close cleanly")
            if self._tray is not None:
                await asyncio.to_thread(self._tray.stop)
            log.info("Snag has quit")
        finally:
            self.finished.set()


    async def _until_server_started(self, server: Any) -> None:
        """Quit while uvicorn starts: let it finish starting (or fail), so the exit it is told of
        next goes through its own shutdown (the pages' WebSockets closed, the app's lifespan end)."""
        serving = self._serving
        deadline = asyncio.get_running_loop().time() + SERVER_STOP_TIMEOUT
        while not server.started and serving is not None and not serving.done():
            if asyncio.get_running_loop().time() >= deadline:
                log.warning("The web server didn't finish starting within %s seconds", SERVER_STOP_TIMEOUT)
                return
            await asyncio.sleep(0.02)


class _CantStart(Exception):
    """Snag couldn't start; the user has been told why."""


def core_for(options: Options, paths: AppPaths) -> SnagCore:
    """Snag's engine. Under the test hook: the Videos and Music folders a fresh database starts with
    are inside paths.root, and the updater's client sends nothing (no_network_client: checking for an
    update, retrying setup or reinstalling says it isn't available in test mode)."""
    if not options.test_hook:
        return SnagCore(paths, tools_for(options, paths), port=options.port)
    from snag.store.settings import default_settings
    from snag.updater.service import no_network_client

    return SnagCore(paths, tools_for(options, paths), port=options.port,
                    defaults=default_settings(paths.root / "Videos", paths.root / "Music"),
                    updater_options={"client_factory": no_network_client})


async def run(options: Options, paths: AppPaths, *, show_message: Callable[[str], None],
              open_url: Callable[[str], object]) -> int:
    try:
        sock = bind_port(options.port)
    except OSError as error:
        log.error("Couldn't take port %d: %s", options.port, error)
        show_message(PORT_IN_USE.format(port=options.port))
        return 1
    core = core_for(options, paths)
    make_tray = None
    if not options.test_hook:
        from snag.platform.tray import Tray

        make_tray = Tray
    snag = Snag(core, sock, open_tab=options.open_tab, test_hook=options.test_hook,
                run_setup=options.fake_tools is None, platform=platform_for(options), open_url=open_url,
                make_tray=make_tray, show_message=show_message)
    console = ConsoleHandler(snag.quit_threadsafe, snag.finished)
    snag.attach(asyncio.get_running_loop())
    console.install()
    try:
        return await snag.run()
    finally:
        console.uninstall()
        sock.close()


def main(argv: list[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    environ = os.environ if environ is None else environ
    options = parse_options(argv, environ)
    show_message = to_stderr if options.test_hook else message_box
    make_dpi_aware()
    paths = AppPaths.default(environ)
    open_url: Callable[[str], object] = recording_opener(paths.root.parent) if options.test_hook else webbrowser.open
    from snag.platform.instance import acquire_single_instance

    try:
        first = acquire_single_instance(f"Local\\Snag-{options.port}")
    except OSError as error:
        show_message(f"Snag couldn't start: {error}")
        return 1
    if not first:  # Snag is already running: show it
        open_url(options.url)
        return 0
    try:
        configure_logging(paths.root)
    except OSError as error:
        show_message(f"Snag couldn't create its folder {paths.root}: {error}")
        return 1
    log.info("Snag %s is starting (Python %s, port %d%s)", __version__, sys.version.split()[0], options.port,
             ", test hook on" if options.test_hook else "")
    try:
        code = asyncio.run(run(options, paths, show_message=show_message, open_url=open_url))
    except Exception as error:
        log.exception("Snag stopped because of an unexpected error")
        show_message(f"Snag stopped because of an unexpected error: {error}")
        return 1
    log.info("Snag exited with code %d", code)
    return code


if __name__ == "__main__":
    sys.exit(main())
