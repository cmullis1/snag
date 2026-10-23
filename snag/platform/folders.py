"""The native Windows folder dialog, and showing files and folders in Explorer.

The folder dialog is tkinter's, which wraps the standard Windows one. Tk may only be used from the
thread that created it, and Snag's server and tray each have threads of their own, so every dialog
runs on one dedicated picker thread: callers (the server, through asyncio.to_thread) hand it a
request and wait for the answer. Each dialog gets a fresh hidden Tk root, made topmost and set as
the dialog's parent, so the dialog opens in front of the browser that asked for it; the root is
destroyed when the dialog closes, so no Tk state outlives a dialog.

Only one dialog is ever on screen. A request that arrives while one is open joins it: it opens
nothing, waits for the open dialog, and gets the same answer (its own start folder is ignored).
Answering the newest request matters because the page that opened the dialog may be gone (an
extension popup closes when the dialog takes focus), and the user's choice should still reach
whoever asks again.

Manual check (opt-in, shows a real dialog): from the repo root run
    .venv/Scripts/python -c "from snag.platform.folders import pick_folder; print(pick_folder(None))"
with a browser window in front. The dialog must open in front of the browser; choosing a folder
prints its path with backslashes, cancelling prints None."""
from __future__ import annotations

import contextlib
import errno
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from concurrent.futures import Future, InvalidStateError
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

Dialog = Callable[[str | None], object]  # start folder -> what askdirectory returned
Dismiss = Callable[[int], None]  # native id of the thread showing the dialog

# How close() cancels a dialog: Cancel is sent again every _DISMISS_EVERY seconds, since one sent
# while the dialog is still being created reaches no window, for up to _DISMISS_FOR seconds.
_DISMISS_EVERY = 0.05
_DISMISS_FOR = 2.0


def show_tk_dialog(initial: str | None) -> object:
    """Show the folder dialog and return tkinter's answer: a path with forward slashes, or "" or
    () when cancelled. Blocks until the dialog closes; must run on the picker thread."""
    import tkinter
    from tkinter import filedialog

    root = tkinter.Tk()
    try:
        root.withdraw()
        root.attributes("-topmost", True)  # the dialog, owned by the root, comes up above the browser
        options: dict[str, object] = {"parent": root, "title": "Choose a folder", "mustexist": True}
        if initial is not None:
            options["initialdir"] = initial
        return filedialog.askdirectory(**options)
    finally:
        root.destroy()


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    # A private WinDLL so these prototypes don't leak into ctypes.windll.user32 for other code.
    _user32 = ctypes.WinDLL("user32", use_last_error=True)

    _EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    _EnumThreadWindows = _user32.EnumThreadWindows
    _EnumThreadWindows.argtypes = (wintypes.DWORD, _EnumWindowsProc, wintypes.LPARAM)
    _EnumThreadWindows.restype = wintypes.BOOL

    _GetClassNameW = _user32.GetClassNameW
    _GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    _GetClassNameW.restype = ctypes.c_int

    _PostMessageW = _user32.PostMessageW
    _PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    _PostMessageW.restype = wintypes.BOOL

    _WM_CLOSE = 0x0010
    _DIALOG_CLASS = "#32770"  # the window class of standard dialogs, the folder dialog included


def dismiss_dialogs(thread_id: int) -> None:
    """Ask every dialog window of the given thread to close, as if the user pressed Cancel.
    Other windows of the thread (Tk's hidden root) are left alone. Safe from any thread."""
    if sys.platform != "win32":
        return
    dialogs: list[int] = []

    @_EnumWindowsProc
    def collect(hwnd: int, _: int) -> bool:
        name = ctypes.create_unicode_buffer(64)
        if _GetClassNameW(hwnd, name, len(name)) and name.value == _DIALOG_CLASS:
            dialogs.append(hwnd)
        return True

    _EnumThreadWindows(thread_id, collect, 0)
    for hwnd in dialogs:
        _PostMessageW(hwnd, _WM_CLOSE, 0, 0)


def _start_folder(initial: str | None) -> str | None:
    """The folder the dialog opens in: initial itself, or its nearest existing parent (a file's
    folder, or what's left of a folder deleted since). None lets Windows choose."""
    if not initial or not os.path.isabs(initial):
        return None
    path = Path(initial)
    for candidate in (path, *path.parents):
        with contextlib.suppress(OSError, ValueError):
            if candidate.is_dir():
                return str(candidate)
    return None


def _as_path(answer: object) -> str | None:
    """tkinter's answer as a Windows path; None when the dialog was cancelled."""
    if not isinstance(answer, str) or not answer:
        return None
    return os.path.normpath(answer)


_STOP = object()


class FolderPicker:
    """Shows one folder dialog at a time on its own thread (see the module docstring)."""

    def __init__(self, dialog: Dialog = show_tk_dialog, dismiss: Dismiss = dismiss_dialogs):
        self._dialog = dialog
        self._dismiss = dismiss
        self._lock = threading.Lock()
        self._requests: queue.Queue[object] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._current: Future[str | None] | None = None
        self._closed = False

    def pick(self, initial: str | None) -> str | None:
        """The folder the user chose, or None if they cancelled (or the picker is closed). Blocks
        until the dialog closes; a dialog error is raised here, in every caller waiting on it."""
        start = _start_folder(initial)  # touches the disk, so before taking the lock
        with self._lock:
            if self._closed:
                return None
            future = self._current
            if future is None or future.done():
                future = Future()
                self._current = future
                self._requests.put((start, future))
                if self._thread is None:
                    # A daemon: an open dialog never keeps Snag from exiting.
                    self._thread = threading.Thread(target=self._serve, name="snag-folder-picker", daemon=True)
                    self._thread.start()
        return future.result()

    def close(self) -> None:
        """For shutdown: callers waiting on a dialog get None at once, the open dialog is
        cancelled, and later requests return None without a dialog.

        The dialog may still be coming up, with no window yet to cancel, so Cancel is sent again
        every 50 ms until the picker thread ends: close() returns once the dialog is gone, or
        after about two seconds if it won't go."""
        with self._lock:
            self._closed = True
            future, self._current = self._current, None
            thread = self._thread
            self._requests.put(_STOP)
        if future is None or not _resolve(future, None) or thread is None:
            return  # no dialog was open or on its way
        deadline = time.monotonic() + _DISMISS_FOR
        while thread.is_alive():
            if thread.native_id is not None:
                self._dismiss(thread.native_id)
            thread.join(_DISMISS_EVERY)
            if time.monotonic() >= deadline:
                break
        if thread.is_alive():
            log.warning("The folder dialog didn't close within %s seconds", _DISMISS_FOR)

    def _serve(self) -> None:
        while (request := self._requests.get()) is not _STOP:
            start, future = request
            if future.done():  # answered by close() before its dialog opened
                continue
            try:
                answer = self._dialog(start)
            except Exception as exc:
                with contextlib.suppress(InvalidStateError):
                    future.set_exception(_detached(exc))
            else:
                _resolve(future, _as_path(answer))


def _detached(error: BaseException) -> BaseException:
    """error, and the errors it was raised from, without their tracebacks, ready to be raised on
    other threads: a traceback holds the picker thread's frames, Tk's objects among them, and a
    Tk object must never be freed on a thread other than its own."""
    pending, seen = [error], set()
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        current.with_traceback(None)
        pending += [current.__cause__, current.__context__]
    return error


def _resolve(future: Future[str | None], result: str | None) -> bool:
    """Set the result unless the future already has one; True if this call set it."""
    try:
        future.set_result(result)
    except InvalidStateError:
        return False
    return True


_picker = FolderPicker()


def pick_folder(initial: str | None) -> str | None:
    """Show the native folder dialog, starting in initial (or its nearest existing parent), and
    return the chosen folder, or None if cancelled. Blocks until the dialog closes, so call it off
    the event loop (asyncio.to_thread). A request while the dialog is open joins it."""
    return _picker.pick(initial)


def close_folder_picker() -> None:
    """At shutdown: cancel an open dialog and answer its waiting callers with None."""
    _picker.close()


def _explorer_exe() -> Path:
    return Path(os.environ.get("SystemRoot", r"C:\Windows")) / "explorer.exe"


def open_in_explorer(path: Path) -> None:
    """Show a file selected in its folder, or open a folder, in Explorer. Raises
    FileNotFoundError if the path doesn't exist (callers choose a fallback, such as the folder of a
    file that was moved).

    Call it off the event loop (asyncio.to_thread): checking the path touches the disk, which
    can stall on a slow or sleeping drive. Pass only paths Snag itself knows about, a job's
    output or a history entry's file or folder, never a path taken as is from a request: Explorer
    opens whatever it is given, and an arbitrary UNC path (\\\\server\\share) would have Windows
    reach out to that server, handing it the user's credentials.

    Explorer is started with plain Popen, not snag.proc.spawn, which would close its windows when
    Snag exits. The command line is built by hand: Explorer parses /select,"<path>" itself, and
    the quoting Popen gives a list argument breaks paths with spaces."""
    target = Path(os.path.abspath(path))
    if target.is_file():
        command = f'"{_explorer_exe()}" /select,"{target}"'
    elif target.is_dir():
        command = f'"{_explorer_exe()}" "{target}"'
    else:
        raise FileNotFoundError(errno.ENOENT, "No such file or folder", str(target))
    explorer = subprocess.Popen(command)
    # explorer.exe hands the window to the running shell and exits; wait for it so its handle is
    # released and Python doesn't warn about a process still running.
    threading.Thread(target=explorer.wait, name="snag-explorer-reaper", daemon=True).start()
