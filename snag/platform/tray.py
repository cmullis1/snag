"""Snag's notification-area (tray) icon and its menu: Open Snag, pause or resume downloads, Quit.

pystray runs the icon's Windows message loop, which blocks, so the icon runs on a thread of its
own and never holds up the asyncio loop. Menu clicks arrive on that thread; each callback is
handed to the asyncio loop with call_soon_threadsafe and runs there, so callbacks may touch the
queue and the rest of Snag directly. A callback may be a coroutine function: its coroutine is
run as a task on the loop. A failing callback is logged; the tray carries on.

update_paused() and notify() may be called from any thread (normally the loop's). Pause all
downloads and Resume downloads are two items, one shown at a time, each with a fixed action: a
menu built before the paused state changed (pystray rebuilds it on update_menu() and after each
click, and a menu can be open while the extension flips the state) still does what its label
said, never the opposite.

Windows caps the tooltip at 127 UTF-16 code units, a notification's title at 63 and its text at
255; longer texts are cut to fit, ending in an ellipsis.

On Windows the icon is pystray's with one change: it loads the small-icon size (16 pixels at 100%
scaling), the size the notification area shows, where pystray would load the large size and let
Windows shrink it. So the frame drawn for that size is the one on screen.

The end of the Windows session (shutting down, restarting, signing out) reaches Snag through the
icon's window, the only window Snag has (a console's logoff and shutdown events never come: Windows
doesn't send them to a process that has loaded user32, and pythonw has no console). Its two
messages go in pystray's private table of window-message handlers (_message_handlers; pystray is
capped below 0.20 for this and for _assert_icon_handle):
  WM_QUERYENDSESSION  answered 1, "the session may end" (pystray's answer to a message it doesn't
                      handle is 0, which Windows reads as Snag holding up the shutdown).
  WM_ENDSESSION       when the session really ends (wParam nonzero; zero means a shutdown was
                      cancelled), on_session_end is called on the icon's thread, and Windows waits
                      for it: Snag's own callback asks for the orderly quit and waits, a few
                      seconds at most, for it to finish, since Windows may end the process once the
                      handler returns. stop() called meanwhile doesn't wait for the icon's thread,
                      which is still in that handler."""
from __future__ import annotations

import asyncio
import inspect
import logging
import os
import sys
import tempfile
import threading
from typing import Any, Callable

import pystray

from snag.platform.icon import tray_image

log = logging.getLogger(__name__)

OPEN_LABEL = "Open Snag"
PAUSE_LABEL = "Pause all downloads"
RESUME_LABEL = "Resume downloads"
QUIT_LABEL = "Quit"

_STOP_TIMEOUT = 5.0  # seconds to wait for the icon's thread to end

WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016

# NOTIFYICONDATAW's fixed-size text fields, in UTF-16 code units, less one for the closing NUL.
TOOLTIP_LIMIT = 127
NOTIFY_TITLE_LIMIT = 63
NOTIFY_MESSAGE_LIMIT = 255
_ELLIPSIS = "…"

Callback = Callable[[], Any]  # may return an awaitable, which is run as a task


def fit_text(text: str, limit: int) -> str:
    """text, or as much of it as fits in limit UTF-16 code units followed by an ellipsis. A
    character outside the Basic Multilingual Plane (most emoji) takes two units, a surrogate pair,
    and is kept or dropped whole, never split."""
    if len(text.encode("utf-16-le", "surrogatepass")) // 2 <= limit:
        return text
    budget = limit - len(_ELLIPSIS)
    used = end = 0
    for end, char in enumerate(text):  # the text is too long, so this always stops early
        used += 2 if ord(char) > 0xFFFF else 1
        if used > budget:
            break
    return text[:end] + _ELLIPSIS


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    # A private WinDLL so these prototypes don't leak into ctypes.windll.user32, which pystray uses.
    _user32 = ctypes.WinDLL("user32", use_last_error=True)

    _LoadImageW = _user32.LoadImageW
    _LoadImageW.argtypes = (
        wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT,
    )
    _LoadImageW.restype = wintypes.HANDLE

    _GetSystemMetrics = _user32.GetSystemMetrics
    _GetSystemMetrics.argtypes = (ctypes.c_int,)
    _GetSystemMetrics.restype = ctypes.c_int

    _IMAGE_ICON = 1
    _LR_LOADFROMFILE = 0x0010
    _SM_CXSMICON = 49
    _SM_CYSMICON = 50

    def load_small_icon(image: Any) -> int:
        """An icon handle for image at the small-icon size. The image is saved as an .ico, with
        every frame it carries (see snag.platform.icon.tray_image), and Windows picks the frame
        drawn for that size. The caller owns the handle; pystray destroys it."""
        fd, name = tempfile.mkstemp(prefix="snag-tray-", suffix=".ico")
        try:
            with os.fdopen(fd, "wb") as file:
                image.save(file, format="ICO")
            width, height = _GetSystemMetrics(_SM_CXSMICON), _GetSystemMetrics(_SM_CYSMICON)
            handle = _LoadImageW(None, name, _IMAGE_ICON, width, height, _LR_LOADFROMFILE)
            if not handle:
                raise ctypes.WinError(ctypes.get_last_error())
            return handle
        finally:
            os.unlink(name)

    class SmallTrayIcon(pystray.Icon):
        """pystray's Windows icon, loading the small-icon size (see the module docstring). Only
        _assert_icon_handle, where pystray turns the image into a handle, is replaced; pystray
        calls it when showing the icon and again after the display changes."""

        def _assert_icon_handle(self) -> None:
            if not self._icon_handle:
                self._icon_handle = load_small_icon(self.icon)

    _default_icon_factory: Callable[..., Any] = SmallTrayIcon
else:
    _default_icon_factory = pystray.Icon


class Tray:
    """The tray icon. Make it on the event loop's thread (or pass loop), then start() it.

    on_open: Open Snag, also a left click on the icon.
    on_pause_toggle(paused): called with the state the user asked for: True from Pause all
        downloads, False from Resume downloads. Which of the two the menu shows follows
        update_paused(), which the app calls whenever the queue's paused state changes (also
        when the change came from the tray). Each item always asks for its own state, so a click
        on a menu built before a change can't ask for the opposite.
    on_quit: Quit. The app shuts down and calls stop().
    on_session_end: Windows is ending the session (see the module docstring). Called on the icon's
        thread, not the loop's, and Windows waits for it to return, so it must be thread-safe and
        bounded.
    title: the tooltip, cut to Windows' 127 code units.
    loop: the asyncio loop the callbacks run on; by default the running one."""

    def __init__(
        self,
        on_open: Callback,
        on_pause_toggle: Callable[[bool], Any],
        on_quit: Callback,
        title: str = "Snag",
        *,
        paused: bool = False,
        on_session_end: Callable[[], Any] | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
        icon_factory: Callable[..., Any] = _default_icon_factory,
    ):
        self._loop = loop or asyncio.get_running_loop()
        self._on_open = on_open
        self._on_pause_toggle = on_pause_toggle
        self._on_quit = on_quit
        self._on_session_end = on_session_end
        self._session_ending = False  # the icon's thread is in WM_ENDSESSION
        self._title = title
        self._paused = paused
        self._icon_factory = icon_factory
        self._tasks: set[asyncio.Future[Any]] = set()  # running coroutine callbacks, kept from the GC

        self._lock = threading.Lock()
        self._icon: Any = None
        self._thread: threading.Thread | None = None
        self._ready = False  # the icon's loop is running, so icon.stop() will be heard
        self._showing = False  # and the icon is on screen
        self._stopping = False

        self.menu = pystray.Menu(
            pystray.MenuItem(OPEN_LABEL, self._open_clicked, default=True),
            pystray.MenuItem(PAUSE_LABEL, self._pause_clicked, visible=lambda item: not self._paused),
            pystray.MenuItem(RESUME_LABEL, self._resume_clicked, visible=lambda item: self._paused),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(QUIT_LABEL, self._quit_clicked),
        )

    @property
    def paused(self) -> bool:
        return self._paused

    def start(self) -> None:
        """Show the icon. Its message loop runs on a daemon thread, so a stuck icon never keeps
        Snag from exiting."""
        with self._lock:
            if self._thread is not None or self._stopping:
                raise RuntimeError("The tray icon can only be started once")
            self._icon = self._make_icon()
            self._thread = threading.Thread(target=self._run, args=(self._icon,), name="snag-tray", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        """Remove the icon and end its thread; returns once it has ended (or after a few seconds
        if pystray doesn't respond). Safe to call at any time, more than once."""
        with self._lock:
            self._stopping = True
            icon, thread, ready = self._icon, self._thread, self._ready
        if icon is None or thread is None:
            return
        if ready:
            icon.stop()
        # Not ready yet: the setup callback sees _stopping and stops the icon itself.
        if self._session_ending:
            return  # its thread is in WM_ENDSESSION, waiting for the quit that called this
        if thread is not threading.current_thread():
            thread.join(_STOP_TIMEOUT)
            if thread.is_alive():
                log.warning("The tray icon's thread didn't end within %s seconds", _STOP_TIMEOUT)

    def update_paused(self, paused: bool) -> None:
        """Show whether downloads are paused: the menu offers Resume downloads when they are,
        Pause all downloads when they aren't."""
        self._paused = paused
        icon = self._live_icon()
        if icon is not None:
            icon.update_menu()

    def notify(self, title: str, message: str) -> None:
        """A Windows notification from the icon; dropped if the icon isn't showing. The title and
        message are cut to Windows' limits, 63 and 255 code units."""
        icon = self._live_icon()
        if icon is not None:
            icon.notify(fit_text(message, NOTIFY_MESSAGE_LIMIT), fit_text(title, NOTIFY_TITLE_LIMIT))

    def _make_icon(self) -> Any:
        title = fit_text(self._title, TOOLTIP_LIMIT)
        icon = self._icon_factory("Snag", icon=tray_image(), title=title, menu=self.menu)
        handlers = getattr(icon, "_message_handlers", None)
        if isinstance(handlers, dict):
            handlers[WM_QUERYENDSESSION] = self._on_query_end_session
            handlers[WM_ENDSESSION] = self._on_end_session
        else:
            log.warning("This pystray has no window-message handlers: Snag won't quit cleanly when Windows shuts down")
        return icon

    # Window messages: pystray calls these on the icon's thread with (wParam, lParam).

    def _on_query_end_session(self, wparam: int, lparam: int) -> int:
        return 1  # the session may end; Snag quits when it does (WM_ENDSESSION)

    def _on_end_session(self, wparam: int, lparam: int) -> int:
        if not wparam:  # a shutdown that was cancelled: Snag carries on
            return 0
        log.info("Windows is ending the session; Snag is quitting")
        self._session_ending = True
        if self._on_session_end is not None:
            try:
                self._on_session_end()
            except Exception:
                log.exception("Quitting as the session ends failed")
        return 0

    def _live_icon(self) -> Any:
        with self._lock:
            return self._icon if self._showing and not self._stopping else None

    def _run(self, icon: Any) -> None:
        try:
            icon.run(setup=self._on_icon_ready)
        except Exception:
            log.exception("The tray icon stopped working")

    def _on_icon_ready(self, icon: Any) -> None:
        """pystray's setup callback: runs once the icon's loop is going. The icon is shown while
        the lock is held, so notify() and stop() never meet an icon that is ready but not yet on
        screen."""
        with self._lock:
            self._ready = True
            stopping = self._stopping
            if not stopping:
                try:
                    icon.visible = True
                except Exception:
                    log.exception("The tray icon couldn't be shown")
                else:
                    self._showing = True
        if stopping:  # stop() came before the icon was ready, so it couldn't stop it
            icon.stop()

    # Menu actions: pystray calls these on the icon's thread with (icon, item).

    def _open_clicked(self, icon: Any, item: Any) -> None:
        self._dispatch(self._on_open)

    def _pause_clicked(self, icon: Any, item: Any) -> None:
        self._dispatch(self._on_pause_toggle, True)

    def _resume_clicked(self, icon: Any, item: Any) -> None:
        self._dispatch(self._on_pause_toggle, False)

    def _quit_clicked(self, icon: Any, item: Any) -> None:
        self._dispatch(self._on_quit)

    def _dispatch(self, callback: Callable[..., Any], *args: Any) -> None:
        try:
            self._loop.call_soon_threadsafe(self._invoke, callback, args)
        except RuntimeError:  # the loop is closed: Snag is on its way out
            log.info("Tray click ignored: Snag is shutting down")

    def _invoke(self, callback: Callable[..., Any], args: tuple[Any, ...]) -> None:
        """On the loop's thread."""
        try:
            result = callback(*args)
            if inspect.isawaitable(result):
                task = asyncio.ensure_future(result)
                self._tasks.add(task)
                task.add_done_callback(self._task_done)
        except Exception:
            log.exception("A tray action failed")

    def _task_done(self, task: asyncio.Future[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            log.error("A tray action failed", exc_info=error)
