import asyncio
import logging
import queue
import sys
import threading
import time

import pystray
import pytest
from PIL import Image

from snag.platform import tray as tray_module
from snag.platform.icon import draw_icon
from snag.platform.tray import (
    OPEN_LABEL,
    PAUSE_LABEL,
    QUIT_LABEL,
    RESUME_LABEL,
    WM_ENDSESSION,
    WM_QUERYENDSESSION,
    Tray,
)

WAIT = 10  # seconds


class FakeIcon:
    """Stands in for pystray.Icon: runs "the icon loop" on the calling thread without showing
    anything, and records what the Tray asks of it. Like pystray's Windows icon it has
    _message_handlers, and deliver() hands its loop a window message, which the loop handles on its
    own thread before it looks at stop() again, as pystray's message loop does."""

    instances: list["FakeIcon"] = []

    def __init__(self, name, icon=None, title=None, menu=None):
        self.name, self.image, self.title, self.menu = name, icon, title, menu
        self.visible = False
        self.menu_updates = 0
        self.notifications: list[tuple[str, str | None]] = []
        self.run_thread: threading.Thread | None = None
        self.before_ready = threading.Event()
        self.before_ready.set()  # tests clear it to hold the icon before it is ready
        self._stopped = threading.Event()
        self.stop_calls = 0
        self._message_handlers = {}
        self._messages: queue.Queue = queue.Queue()
        FakeIcon.instances.append(self)

    def run(self, setup=None):
        self.run_thread = threading.current_thread()
        assert self.before_ready.wait(WAIT)
        setup(self)  # pystray calls setup (on a thread of its own) once the icon is ready
        deadline = time.monotonic() + WAIT
        while not self._stopped.is_set():
            assert time.monotonic() < deadline, "the tray never stopped the icon"
            try:
                message, wparam, lparam, answer = self._messages.get(timeout=0.01)
            except queue.Empty:
                continue
            answer.put(self._message_handlers.get(message, lambda w, l: 0)(wparam, lparam))

    def deliver(self, message, wparam=0, lparam=0) -> "queue.Queue":
        """Windows sends the icon's window a message; the answer arrives in the returned queue."""
        answer: queue.Queue = queue.Queue()
        self._messages.put((message, wparam, lparam, answer))
        return answer

    def stop(self):
        self.stop_calls += 1
        self._stopped.set()

    def update_menu(self):
        self.menu_updates += 1

    def notify(self, message, title=None):
        self.notifications.append((message, title))


class Recorder:
    """The app's side: records each callback with the thread it ran on."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.event = asyncio.Event()

    def make(self, name):
        def callback(*args):
            self.calls.append((name, args, threading.get_ident()))
            self.event.set()

        return callback

    async def next_call(self):
        await asyncio.wait_for(self.event.wait(), WAIT)
        self.event.clear()
        return self.calls[-1]


@pytest.fixture
def app():
    return Recorder()


@pytest.fixture
def make_tray(app):
    FakeIcon.instances = []
    made: list[Tray] = []

    def make(**kwargs):
        options = dict(
            on_open=app.make("open"), on_pause_toggle=app.make("pause"), on_quit=app.make("quit"),
            icon_factory=FakeIcon,
        )
        options.update(kwargs)
        tray = Tray(**options)
        made.append(tray)
        return tray

    yield make
    for tray in made:
        tray.stop()


def _items(tray: Tray) -> list[pystray.MenuItem]:
    return [item for item in tray.menu.items if item is not pystray.Menu.SEPARATOR]


def _shown(tray: Tray) -> list[str]:
    """The labels the menu shows right now (pystray leaves out items that aren't visible)."""
    return [item.text for item in _items(tray) if item.visible]


def _item(tray: Tray, text: str) -> pystray.MenuItem:
    (found,) = [item for item in _items(tray) if item.text == text]
    return found


def _click(tray: Tray, text: str, icon=None) -> None:
    """Activate a menu item the way pystray does: on the tray's own thread."""
    clicker = threading.Thread(target=_item(tray, text), args=(icon,))
    clicker.start()
    clicker.join(WAIT)


async def test_the_menu_offers_open_pause_and_quit(make_tray):
    tray = make_tray()
    assert _shown(tray) == [OPEN_LABEL, PAUSE_LABEL, QUIT_LABEL]
    assert tray.menu.items[-2] is pystray.Menu.SEPARATOR  # Quit stands apart
    assert OPEN_LABEL == "Open Snag"
    assert PAUSE_LABEL == "Pause all downloads"
    assert RESUME_LABEL == "Resume downloads"
    assert QUIT_LABEL == "Quit"


async def test_open_is_the_default_item(make_tray):
    tray = make_tray()
    assert [item.text for item in _items(tray) if item.default] == [OPEN_LABEL]


async def test_callbacks_run_on_the_event_loop_thread(make_tray, app):
    tray = make_tray()
    for label, name, args in ((OPEN_LABEL, "open", ()), (PAUSE_LABEL, "pause", (True,)), (QUIT_LABEL, "quit", ())):
        _click(tray, label)
        assert await app.next_call() == (name, args, threading.get_ident())


async def test_a_left_click_on_the_icon_opens_snag(make_tray, app):
    tray = make_tray()
    clicker = threading.Thread(target=tray.menu, args=(None,))  # pystray runs the default item
    clicker.start()
    clicker.join(WAIT)
    assert (await app.next_call())[0] == "open"


async def test_the_pause_item_reflects_the_state_and_asks_for_the_opposite(make_tray, app):
    tray = make_tray()
    _click(tray, PAUSE_LABEL)
    assert (await app.next_call())[:2] == ("pause", (True,))
    assert _shown(tray)[1] == PAUSE_LABEL  # unchanged until the app says so
    tray.update_paused(True)
    assert _shown(tray) == [OPEN_LABEL, RESUME_LABEL, QUIT_LABEL]
    _click(tray, RESUME_LABEL)
    assert (await app.next_call())[:2] == ("pause", (False,))


async def test_starting_paused_shows_resume(make_tray):
    tray = make_tray(paused=True)
    assert _shown(tray) == [OPEN_LABEL, RESUME_LABEL, QUIT_LABEL]
    assert tray.paused is True


@pytest.mark.parametrize(("built_paused", "label", "asked"), [(True, RESUME_LABEL, False), (False, PAUSE_LABEL, True)])
async def test_a_click_on_a_stale_menu_does_what_its_label_said(make_tray, app, built_paused, label, asked):
    """The menu on screen was built before the state flipped (by the extension, say): its item
    must still do what it said, never the opposite."""
    tray = make_tray(paused=built_paused)
    tray.update_paused(not built_paused)  # the state changes while the old menu is still open
    _click(tray, label)
    assert (await app.next_call())[:2] == ("pause", (asked,))


async def test_coroutine_callbacks_are_run_as_tasks(make_tray):
    done = asyncio.Event()

    async def on_quit():
        await asyncio.sleep(0)
        done.set()

    tray = make_tray(on_quit=on_quit)
    _click(tray, QUIT_LABEL)
    await asyncio.wait_for(done.wait(), WAIT)


async def test_a_failing_callback_is_logged_and_the_tray_carries_on(make_tray, app, caplog):
    def on_open():
        raise RuntimeError("browser missing")

    async def on_quit():
        raise RuntimeError("quit broke")

    tray = make_tray(on_open=on_open, on_quit=on_quit)
    with caplog.at_level(logging.ERROR, logger=tray_module.__name__):
        _click(tray, OPEN_LABEL)
        _click(tray, QUIT_LABEL)
        for _ in range(100):
            if len(caplog.records) == 2:
                break
            await asyncio.sleep(0.01)
    messages = sorted(str(record.exc_info[1]) for record in caplog.records)
    assert messages == ["browser missing", "quit broke"]
    _click(tray, PAUSE_LABEL)
    assert (await app.next_call())[0] == "pause"


def test_a_click_after_the_loop_closed_is_ignored(app):
    loop = asyncio.new_event_loop()
    loop.close()
    tray = Tray(app.make("open"), app.make("pause"), app.make("quit"), loop=loop, icon_factory=FakeIcon)
    _click(tray, OPEN_LABEL)  # must not raise on the tray thread
    assert app.calls == []


def test_without_a_running_loop_a_loop_must_be_given(app):
    with pytest.raises(RuntimeError):
        Tray(app.make("open"), app.make("pause"), app.make("quit"), icon_factory=FakeIcon)


# The icon's own thread.


async def test_start_runs_the_icon_on_its_own_daemon_thread_and_shows_it(make_tray):
    tray = make_tray(title="Snag (test)")
    tray.start()
    (icon,) = FakeIcon.instances
    for _ in range(100):
        if icon.visible:
            break
        await asyncio.sleep(0.01)
    assert icon.visible
    assert icon.run_thread is not threading.current_thread()
    assert icon.run_thread.daemon  # a stuck icon never keeps Snag from exiting
    assert icon.title == "Snag (test)"
    assert icon.menu is tray.menu
    assert isinstance(icon.image, Image.Image)
    assert icon.image.tobytes() == draw_icon(icon.image.width).tobytes()
    tray.stop()
    assert not icon.run_thread.is_alive()
    assert icon.stop_calls == 1


async def test_stop_right_after_start_still_stops_the_icon(make_tray):
    class SlowIcon(FakeIcon):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.before_ready.clear()  # the icon isn't ready yet when stop() comes

    tray = make_tray(icon_factory=SlowIcon)
    tray.start()
    (icon,) = FakeIcon.instances
    stopper = threading.Thread(target=tray.stop)
    stopper.start()
    await asyncio.sleep(0.05)
    icon.before_ready.set()
    stopper.join(WAIT)
    assert not stopper.is_alive()
    icon.run_thread.join(WAIT)
    assert not icon.run_thread.is_alive()
    assert icon.stop_calls == 1


async def test_stop_is_safe_before_start_and_twice(make_tray):
    tray = make_tray()
    tray.stop()
    tray.stop()
    assert FakeIcon.instances == []


async def test_start_twice_is_an_error(make_tray):
    tray = make_tray()
    tray.start()
    with pytest.raises(RuntimeError):
        tray.start()


async def test_update_paused_refreshes_the_running_icons_menu(make_tray):
    tray = make_tray()
    tray.update_paused(True)  # not started: just remembered
    tray.start()
    (icon,) = FakeIcon.instances
    for _ in range(100):
        if icon.visible:
            break
        await asyncio.sleep(0.01)
    before = icon.menu_updates
    tray.update_paused(False)
    assert icon.menu_updates == before + 1
    assert tray.paused is False


async def test_notify_shows_a_message_once_the_icon_is_up(make_tray):
    tray = make_tray()
    tray.notify("Snag", "not shown: no icon yet")
    tray.start()
    (icon,) = FakeIcon.instances
    for _ in range(100):
        if icon.visible:
            break
        await asyncio.sleep(0.01)
    tray.notify("Download finished", "Song.mp3")
    assert icon.notifications == [("Song.mp3", "Download finished")]
    tray.stop()
    tray.notify("Snag", "not shown: stopped")
    assert icon.notifications == [("Song.mp3", "Download finished")]


async def test_a_failing_icon_loop_is_logged_and_stop_returns(make_tray, caplog):
    class BrokenIcon(FakeIcon):
        def run(self, setup=None):
            raise OSError("no notification area")

    tray = make_tray(icon_factory=BrokenIcon)
    with caplog.at_level(logging.ERROR, logger=tray_module.__name__):
        tray.start()
        tray.stop()
    assert any("no notification area" in str(record.exc_info[1]) for record in caplog.records)


async def test_notify_waits_for_the_icon_to_be_showing(make_tray):
    """Between the icon's loop being ready and the icon being on screen, a notification (or a
    stop) waits rather than reaching an icon that isn't showing yet."""
    entered, go = threading.Event(), threading.Event()

    class SlowToShowIcon(FakeIcon):
        _visible = False

        @property
        def visible(self):
            return self._visible

        @visible.setter
        def visible(self, value):
            if value:
                entered.set()
                assert go.wait(WAIT)
            self._visible = value

        def notify(self, message, title=None):
            self.notifications.append((message, title, self.visible))

    tray = make_tray(icon_factory=SlowToShowIcon)
    tray.start()
    assert entered.wait(WAIT)
    notifier = threading.Thread(target=tray.notify, args=("Download finished", "Song.mp3"))
    notifier.start()
    notifier.join(0.2)
    assert notifier.is_alive()  # waiting for the icon to show, not sent to a hidden icon
    go.set()
    notifier.join(WAIT)
    (icon,) = FakeIcon.instances
    assert icon.notifications == [("Song.mp3", "Download finished", True)]


async def test_an_icon_that_cannot_show_is_logged_and_stops_cleanly(make_tray, caplog):
    class UnshowableIcon(FakeIcon):
        @property
        def visible(self):
            return False

        @visible.setter
        def visible(self, value):
            if value:
                raise OSError("no notification area")

    tray = make_tray(icon_factory=UnshowableIcon)
    with caplog.at_level(logging.ERROR, logger=tray_module.__name__):
        tray.start()
        (icon,) = FakeIcon.instances
        for _ in range(100):
            if caplog.records:
                break
            await asyncio.sleep(0.01)
    assert any("no notification area" in str(record.exc_info[1]) for record in caplog.records)
    tray.notify("Snag", "dropped: the icon isn't showing")
    assert icon.notifications == []
    tray.stop()
    assert not icon.run_thread.is_alive()


# Windows' length limits for the tooltip and notifications, counted in UTF-16 code units.


def _utf16_units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


async def _shown_icon(tray: Tray) -> FakeIcon:
    tray.start()
    (icon,) = FakeIcon.instances
    for _ in range(100):
        if icon.visible:
            break
        await asyncio.sleep(0.01)
    assert icon.visible
    return icon


async def test_long_notifications_are_cut_to_fit_with_an_ellipsis(make_tray):
    tray = make_tray()
    icon = await _shown_icon(tray)
    tray.notify("T" * 100, "M" * 300)
    ((message, title),) = icon.notifications
    assert title == "T" * 62 + "…"
    assert message == "M" * 254 + "…"


async def test_a_cut_never_splits_an_emoji(make_tray):
    """An emoji outside the Basic Multilingual Plane is two UTF-16 code units (a surrogate pair):
    the cut goes before it, never between its halves."""
    tray = make_tray(title="a" + "\U0001F600" * 100)
    icon = await _shown_icon(tray)
    tray.notify("b" + "\U0001F3B5" * 40, "\U0001F4E5" * 200)
    ((message, title),) = icon.notifications
    assert title == "b" + "\U0001F3B5" * 30 + "…"  # 1 + 60 + 1 units; one more emoji would make 63
    assert message == "\U0001F4E5" * 127 + "…"  # 254 + 1 units
    assert icon.title == "a" + "\U0001F600" * 62 + "…"  # 1 + 124 + 1 units
    for text, limit in ((title, 63), (message, 255), (icon.title, 127)):
        assert _utf16_units(text) <= limit
        text.encode("utf-16-le")  # no lone surrogate: it would not encode


async def test_a_long_tooltip_is_cut_to_fit(make_tray):
    tray = make_tray(title="Snag - " + "x" * 200)
    icon = await _shown_icon(tray)
    assert _utf16_units(icon.title) == 127
    assert icon.title.startswith("Snag - x") and icon.title.endswith("x…")


@pytest.mark.skipif(sys.platform != "win32", reason="pystray's Windows backend")
async def test_the_cut_texts_fit_windows_notification_structure(make_tray):
    """The real NOTIFYICONDATAW pystray fills in: the uncut texts don't fit, the cut ones do,
    with room left for the terminating NUL."""
    from pystray._util import win32

    tray = make_tray(title="\U0001F600" * 100)
    icon = await _shown_icon(tray)
    long_title, long_message = "\U0001F3B5" * 40, "M" * 300
    tray.notify(long_title, long_message)
    ((message, title),) = icon.notifications
    with pytest.raises(ValueError):
        win32.NOTIFYICONDATAW(szInfoTitle=long_title)
    with pytest.raises(ValueError):
        win32.NOTIFYICONDATAW(szInfo=long_message)
    data = win32.NOTIFYICONDATAW(szTip=icon.title, szInfoTitle=title, szInfo=message)
    assert (data.szTip, data.szInfoTitle, data.szInfo) == (icon.title, title, message)


async def test_short_texts_are_left_alone(make_tray):
    tray = make_tray(title="Snag")
    icon = await _shown_icon(tray)
    tray.notify("t" * 63, "m" * 255)  # exactly at the limits
    assert icon.notifications == [("m" * 255, "t" * 63)]
    assert icon.title == "Snag"


# The end of the Windows session (shutting down, restarting, signing out).


async def test_windows_may_end_the_session_and_snag_quits_when_it_does(make_tray):
    """WM_QUERYENDSESSION is answered "yes" at once (pystray's own answer, 0, would be read as Snag
    holding up the shutdown); WM_ENDSESSION, when the session really ends, asks Snag to quit on the
    icon's thread and returns only once on_session_end does, since Windows may end the process then."""
    ended = []
    tray = make_tray(on_session_end=lambda: ended.append(threading.current_thread()))
    tray.start()
    icon = FakeIcon.instances[-1]
    assert icon.deliver(WM_QUERYENDSESSION, 0, 0).get(timeout=WAIT) == 1
    assert ended == []
    assert icon.deliver(WM_ENDSESSION, 0, 0).get(timeout=WAIT) == 0  # the shutdown was cancelled
    assert ended == []
    assert icon.deliver(WM_ENDSESSION, 1, 0).get(timeout=WAIT) == 0
    assert ended == [icon.run_thread]


async def test_stopping_the_tray_while_the_session_ends_does_not_wait_for_its_thread(make_tray):
    """Snag's quit removes the icon (stop()) while the icon's thread is still in WM_ENDSESSION,
    waiting for that quit to finish: stop() must not wait for the thread, or each would wait for
    the other."""
    finished = threading.Event()
    stopped_in = []

    def quit_and_wait():  # what Snag does: its shutdown, on the loop, stops the tray, then finishes
        def shut_down():
            started = time.monotonic()
            tray.stop()
            stopped_in.append(time.monotonic() - started)
            finished.set()

        threading.Thread(target=shut_down).start()
        assert finished.wait(WAIT)

    tray = make_tray(on_session_end=quit_and_wait)
    tray.start()
    icon = FakeIcon.instances[-1]
    assert icon.deliver(WM_ENDSESSION, 1, 0).get(timeout=WAIT) == 0
    assert stopped_in and stopped_in[0] < 2
    assert icon.stop_calls == 1


async def test_a_failing_session_end_callback_is_logged(make_tray, caplog):
    def broken():
        raise RuntimeError("no quitting today")

    tray = make_tray(on_session_end=broken)
    tray.start()
    with caplog.at_level(logging.ERROR, logger="snag.platform.tray"):
        assert FakeIcon.instances[-1].deliver(WM_ENDSESSION, 1, 0).get(timeout=WAIT) == 0
    assert "no quitting today" in caplog.text


async def test_without_a_session_end_callback_the_session_may_still_end(make_tray):
    tray = make_tray()
    tray.start()
    icon = FakeIcon.instances[-1]
    assert icon.deliver(WM_QUERYENDSESSION).get(timeout=WAIT) == 1
    assert icon.deliver(WM_ENDSESSION, 1).get(timeout=WAIT) == 0


@pytest.mark.skipif(sys.platform != "win32", reason="pystray's Windows backend")
async def test_the_real_pystray_icon_takes_the_session_end_handlers(app):
    """They go in pystray's private _message_handlers (pystray is capped below 0.20 for this and
    _assert_icon_handle): this catches a pystray that renames it."""
    ended = []
    tray = Tray(app.make("open"), app.make("pause"), app.make("quit"), on_session_end=lambda: ended.append(1))
    icon = tray._make_icon()  # constructed only: nothing appears until run()
    assert icon._message_handlers[WM_QUERYENDSESSION](0, 0) == 1
    icon._message_handlers[WM_ENDSESSION](1, 0)
    assert ended == [1]


@pytest.mark.skipif(sys.platform != "win32", reason="pystray's Windows backend")
async def test_the_real_pystray_icon_accepts_the_tray_without_showing_anything(app):
    tray = Tray(app.make("open"), app.make("pause"), app.make("quit"))
    icon = tray._make_icon()  # constructed only: nothing appears until run()
    assert isinstance(icon, pystray.Icon)
    assert icon.menu is tray.menu
    assert icon.title == "Snag"


# The icon Windows shows: the frame drawn for the notification area's size.

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _ICONINFO(ctypes.Structure):
        _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD), ("yHotspot", wintypes.DWORD),
                    ("hbmMask", wintypes.HBITMAP), ("hbmColor", wintypes.HBITMAP)]

    class _BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                    ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]

    _test_user32 = ctypes.WinDLL("user32", use_last_error=True)
    _test_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    _test_user32.GetIconInfo.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ICONINFO))
    _test_user32.GetSystemMetrics.argtypes = (ctypes.c_int,)
    _test_user32.GetDC.argtypes = (wintypes.HWND,)
    _test_user32.GetDC.restype = wintypes.HDC
    _test_user32.ReleaseDC.argtypes = (wintypes.HWND, wintypes.HDC)
    _test_gdi32.GetDIBits.argtypes = (wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT, wintypes.LPVOID,
                                      ctypes.POINTER(_BITMAPINFOHEADER), wintypes.UINT)
    _test_gdi32.DeleteObject.argtypes = (wintypes.HGDIOBJ,)


def _icon_pixels(handle) -> tuple[int, bytes]:
    """The icon's width and its colour pixels as top-down RGBA."""
    info = _ICONINFO()
    assert _test_user32.GetIconInfo(handle, ctypes.byref(info)), ctypes.WinError(ctypes.get_last_error())
    dc = _test_user32.GetDC(None)
    try:
        header = _BITMAPINFOHEADER(biSize=ctypes.sizeof(_BITMAPINFOHEADER))
        assert _test_gdi32.GetDIBits(dc, info.hbmColor, 0, 0, None, ctypes.byref(header), 0)
        width = header.biWidth
        header = _BITMAPINFOHEADER(biSize=ctypes.sizeof(_BITMAPINFOHEADER), biWidth=width, biHeight=-width,
                                   biPlanes=1, biBitCount=32)
        pixels = ctypes.create_string_buffer(width * width * 4)
        assert _test_gdi32.GetDIBits(dc, info.hbmColor, 0, width, pixels, ctypes.byref(header), 0) == width
    finally:
        _test_user32.ReleaseDC(None, dc)
        _test_gdi32.DeleteObject(info.hbmColor)
        _test_gdi32.DeleteObject(info.hbmMask)
    bgra = pixels.raw
    rgba = bytearray(len(bgra))
    rgba[0::4], rgba[1::4], rgba[2::4], rgba[3::4] = bgra[2::4], bgra[1::4], bgra[0::4], bgra[3::4]
    return width, bytes(rgba)


@pytest.mark.skipif(sys.platform != "win32", reason="pystray's Windows backend")
async def test_windows_gets_the_frame_drawn_for_the_small_icon_size(app):
    """pystray on its own loads the icon at the large size (32 pixels at 100% scaling) and Windows
    shrinks it for the notification area; Snag's icon loads the small-icon size, so the frame
    drawn for it is what appears. The icon is loaded, never shown."""
    tray = Tray(app.make("open"), app.make("pause"), app.make("quit"))
    icon = tray._make_icon()
    small = _test_user32.GetSystemMetrics(49)  # SM_CXSMICON
    try:
        icon._assert_icon_handle()
        width, pixels = _icon_pixels(icon._icon_handle)
    finally:
        icon._release_icon()
    assert width == small
    assert pixels == draw_icon(small).tobytes()


@pytest.mark.skipif(sys.platform != "win32", reason="pystray's Windows backend")
async def test_pystrays_own_icon_update_loads_the_small_size(app):
    """Snag replaces a private pystray method (_assert_icon_handle). Driving pystray's own
    _update_icon(), the path it takes when the image changes, catches a pystray release that stops
    calling it: the handle sent to the notification area must still be the small-icon size. Nothing
    is shown: _message, which would talk to the notification area, is replaced."""
    tray = Tray(app.make("open"), app.make("pause"), app.make("quit"))
    icon = tray._make_icon()
    sent = []
    icon._message = lambda code, flags, **data: sent.append(data)
    small = _test_user32.GetSystemMetrics(49)  # SM_CXSMICON
    try:
        icon._update_icon()
        assert [data.get("hIcon") for data in sent] == [icon._icon_handle]
        width, pixels = _icon_pixels(icon._icon_handle)
    finally:
        icon._release_icon()
    assert width == small
    assert pixels == draw_icon(small).tobytes()
