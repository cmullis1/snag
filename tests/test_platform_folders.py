import os
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from snag.platform import folders
from snag.platform.folders import FolderPicker, open_in_explorer

WAIT = 10  # seconds; generous so a slow machine never flakes, but a hang still fails the test


class FakeDialog:
    """Stands in for the native dialog: records each call and blocks until the test answers."""

    def __init__(self):
        self.calls: list[tuple[str | None, threading.Thread]] = []
        self.opened = threading.Event()
        self._answer = threading.Event()
        self._result: object = ""
        self._error: BaseException | None = None

    def __call__(self, initial: str | None) -> object:
        self.calls.append((initial, threading.current_thread()))
        self.opened.set()
        assert self._answer.wait(WAIT), "the test never answered the dialog"
        self._answer.clear()
        self.opened.clear()
        if self._error is not None:
            raise self._error
        return self._result

    def answer(self, result: object) -> None:
        self._result = result
        self._error = None
        self._answer.set()

    def fail(self, error: BaseException) -> None:
        self._error = error
        self._answer.set()


class Caller:
    """Calls picker.pick on its own thread, as the server does through asyncio.to_thread."""

    def __init__(self, picker: FolderPicker, initial: str | None):
        self.result: object = None
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._run, args=(picker, initial), daemon=True)
        self.thread.start()

    def _run(self, picker: FolderPicker, initial: str | None) -> None:
        try:
            self.result = picker.pick(initial)
        except BaseException as exc:
            self.error = exc

    def join(self) -> object:
        self.thread.join(WAIT)
        assert not self.thread.is_alive(), "pick() never returned"
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture
def dialog():
    return FakeDialog()


@pytest.fixture
def picker(dialog):
    picker = FolderPicker(dialog=dialog, dismiss=lambda thread_id: dialog.answer(""))
    yield picker
    picker.close()


def test_returns_the_chosen_folder_with_windows_separators(picker, dialog, tmp_path):
    caller = Caller(picker, None)
    assert dialog.opened.wait(WAIT)
    dialog.answer(tmp_path.as_posix())  # Tk answers with forward slashes
    assert caller.join() == str(tmp_path)


@pytest.mark.parametrize("cancelled", ["", ()])
def test_cancelling_returns_none(picker, dialog, cancelled):
    caller = Caller(picker, None)
    assert dialog.opened.wait(WAIT)
    dialog.answer(cancelled)  # Tk gives "" or () when the dialog is cancelled
    assert caller.join() is None


def test_the_dialog_runs_on_one_dedicated_thread_not_the_callers(picker, dialog, tmp_path):
    for _ in range(2):
        caller = Caller(picker, None)
        assert dialog.opened.wait(WAIT)
        dialog.answer(str(tmp_path))
        caller.join()
    (_, first), (_, second) = dialog.calls
    assert first is second
    assert first is not threading.current_thread()
    assert first.daemon  # an open dialog never keeps Snag from exiting


def test_starts_in_the_given_folder(picker, dialog, tmp_path):
    caller = Caller(picker, str(tmp_path))
    assert dialog.opened.wait(WAIT)
    dialog.answer("")
    caller.join()
    assert dialog.calls[0][0] == str(tmp_path)


def test_a_missing_start_folder_starts_in_its_nearest_existing_parent(picker, dialog, tmp_path):
    caller = Caller(picker, str(tmp_path / "gone" / "deeper"))
    assert dialog.opened.wait(WAIT)
    dialog.answer("")
    caller.join()
    assert dialog.calls[0][0] == str(tmp_path)


def test_a_file_as_start_starts_in_its_folder(picker, dialog, tmp_path):
    file = tmp_path / "video.mp4"
    file.write_bytes(b"")
    caller = Caller(picker, str(file))
    assert dialog.opened.wait(WAIT)
    dialog.answer("")
    caller.join()
    assert dialog.calls[0][0] == str(tmp_path)


@pytest.mark.parametrize("initial", [None, ""])
def test_no_start_folder_lets_windows_choose(picker, dialog, initial):
    caller = Caller(picker, initial)
    assert dialog.opened.wait(WAIT)
    dialog.answer("")
    caller.join()
    assert dialog.calls[0][0] is None


def test_a_request_while_the_dialog_is_open_joins_it(picker, dialog, tmp_path):
    first = Caller(picker, str(tmp_path))
    assert dialog.opened.wait(WAIT)
    second = Caller(picker, None)
    second.thread.join(0.2)
    assert second.thread.is_alive()  # waiting on the open dialog, not answered with None
    dialog.answer(str(tmp_path))
    assert first.join() == str(tmp_path)
    assert second.join() == str(tmp_path)
    assert len(dialog.calls) == 1  # only ever one dialog on screen


def test_a_new_request_after_the_dialog_closed_opens_a_new_one(picker, dialog, tmp_path):
    first = Caller(picker, None)
    assert dialog.opened.wait(WAIT)
    dialog.answer("")
    assert first.join() is None
    second = Caller(picker, None)
    assert dialog.opened.wait(WAIT)
    dialog.answer(str(tmp_path))
    assert second.join() == str(tmp_path)
    assert len(dialog.calls) == 2


def test_a_dialog_error_reaches_every_waiting_caller_and_the_next_request_works(picker, dialog, tmp_path):
    first = Caller(picker, None)
    assert dialog.opened.wait(WAIT)
    second = Caller(picker, None)
    second.thread.join(0.2)
    dialog.fail(RuntimeError("no display"))
    with pytest.raises(RuntimeError, match="no display"):
        first.join()
    with pytest.raises(RuntimeError, match="no display"):
        second.join()
    third = Caller(picker, None)
    assert dialog.opened.wait(WAIT)
    dialog.answer(str(tmp_path))
    assert third.join() == str(tmp_path)


def test_close_answers_waiting_callers_with_none_and_dismisses_the_dialog(dialog):
    dismissed: list[int] = []

    def dismiss(thread_id: int) -> None:
        dismissed.append(thread_id)
        dialog.answer("C:/chosen/too/late")  # the dialog closes; its answer must be ignored

    picker = FolderPicker(dialog=dialog, dismiss=dismiss)
    caller = Caller(picker, None)
    assert dialog.opened.wait(WAIT)
    picker.close()
    assert caller.join() is None
    assert dismissed and set(dismissed) == {dialog.calls[0][1].native_id}
    assert not dialog.calls[0][1].is_alive()  # close() returns once the picker thread has ended


def test_close_while_the_dialog_is_still_being_created_cancels_it_once_it_appears():
    """Tk and the native dialog take a moment to come up, and a Cancel sent before the dialog's
    window exists goes nowhere: close() keeps sending it until the dialog is gone."""
    started, appeared, cancelled = threading.Event(), threading.Event(), threading.Event()

    def slow_dialog(initial):
        started.set()
        time.sleep(0.3)  # still being created
        appeared.set()
        assert cancelled.wait(WAIT), "the dialog was never cancelled"
        return "C:/chosen/too/late"

    def dismiss(thread_id):
        if appeared.is_set():  # before the window exists, WM_CLOSE reaches nothing
            cancelled.set()

    picker = FolderPicker(dialog=slow_dialog, dismiss=dismiss)
    caller = Caller(picker, None)
    assert started.wait(WAIT)
    picker.close()
    assert cancelled.is_set()
    assert caller.join() is None
    assert not picker._thread.is_alive()


def test_close_gives_up_on_a_dialog_that_never_closes(monkeypatch):
    monkeypatch.setattr(folders, "_DISMISS_FOR", 0.3)
    started, release = threading.Event(), threading.Event()

    def stuck_dialog(initial):
        started.set()
        release.wait(WAIT)
        return ""

    sent: list[int] = []
    picker = FolderPicker(dialog=stuck_dialog, dismiss=sent.append)
    caller = Caller(picker, None)
    assert started.wait(WAIT)
    began = time.monotonic()
    picker.close()
    took = time.monotonic() - began
    release.set()
    assert caller.join() is None
    assert 0.25 <= took < 3
    assert len(sent) >= 3  # sent again and again, about every 50 ms


def test_the_start_folder_is_worked_out_without_holding_up_close(monkeypatch, dialog):
    """Finding the start folder touches the disk (a slow network drive, say); close() mustn't wait
    for it."""
    looking, found = threading.Event(), threading.Event()

    def slow_start_folder(initial):
        looking.set()
        assert found.wait(WAIT)
        return initial

    monkeypatch.setattr(folders, "_start_folder", slow_start_folder)
    picker = FolderPicker(dialog=dialog, dismiss=lambda thread_id: None)
    caller = Caller(picker, "\\\\slow-server\\share")
    assert looking.wait(WAIT)
    closer = threading.Thread(target=picker.close)
    closer.start()
    closer.join(1)
    assert not closer.is_alive()
    found.set()
    assert caller.join() is None  # closed meanwhile: no dialog
    assert dialog.calls == []


def test_a_dialog_error_reaches_callers_without_the_picker_threads_frames(picker, dialog):
    """The traceback would hold the picker thread's frames, Tk's objects among them, and Tk
    objects must not be freed on another thread."""
    try:
        try:
            raise OSError("Tcl couldn't start")
        except OSError as inner:
            raise RuntimeError("no display") from inner
    except RuntimeError as error:
        failure = error
    first = Caller(picker, None)
    assert dialog.opened.wait(WAIT)
    dialog.fail(failure)
    with pytest.raises(RuntimeError, match="no display") as raised:
        first.join()
    frames = []
    tb = raised.value.__traceback__
    while tb is not None:
        frames.append(tb.tb_frame.f_code.co_name)
        tb = tb.tb_next
    assert "_serve" not in frames and "__call__" not in frames
    assert raised.value.__cause__.__traceback__ is None


def test_after_close_requests_return_none_without_a_dialog(dialog):
    picker = FolderPicker(dialog=dialog, dismiss=lambda thread_id: None)
    picker.close()
    assert picker.pick(None) is None
    assert dialog.calls == []


def test_close_without_any_request_is_harmless(dialog):
    picker = FolderPicker(dialog=dialog, dismiss=lambda thread_id: pytest.fail("nothing to dismiss"))
    picker.close()
    picker.close()


def test_pick_folder_uses_the_shared_picker(monkeypatch):
    seen = []

    class Recorder:
        def pick(self, initial):
            seen.append(initial)
            return "C:\\chosen"

    monkeypatch.setattr(folders, "_picker", Recorder())
    assert folders.pick_folder("C:\\start") == "C:\\chosen"
    assert seen == ["C:\\start"]


# The real dialog function, with tkinter replaced at the boundary.


class FakeTk:
    instances: list["FakeTk"] = []

    def __init__(self):
        self.log: list[tuple] = [("created", threading.current_thread())]
        FakeTk.instances.append(self)

    def withdraw(self):
        self.log.append(("withdraw",))

    def attributes(self, *args):
        self.log.append(("attributes", *args))

    def update(self):
        self.log.append(("update",))

    def destroy(self):
        self.log.append(("destroy",))


@pytest.fixture
def fake_tkinter(monkeypatch):
    FakeTk.instances = []
    asked: list[dict] = []
    outcome: dict[str, object] = {"result": "C:/picked"}

    def askdirectory(**options):
        asked.append(options)
        if isinstance(outcome["result"], BaseException):
            raise outcome["result"]
        return outcome["result"]

    tkinter = types.ModuleType("tkinter")
    tkinter.Tk = FakeTk
    filedialog = types.ModuleType("tkinter.filedialog")
    filedialog.askdirectory = askdirectory
    tkinter.filedialog = filedialog
    monkeypatch.setitem(sys.modules, "tkinter", tkinter)
    monkeypatch.setitem(sys.modules, "tkinter.filedialog", filedialog)
    return types.SimpleNamespace(asked=asked, outcome=outcome)


def test_the_tk_dialog_is_parented_to_a_hidden_topmost_root(fake_tkinter):
    assert folders.show_tk_dialog("C:\\start") == "C:/picked"
    (root,) = FakeTk.instances
    names = [entry[0] for entry in root.log]
    assert names.index("withdraw") < names.index("destroy")
    assert ("attributes", "-topmost", True) in root.log
    (options,) = fake_tkinter.asked
    assert options["parent"] is root
    assert options["initialdir"] == "C:\\start"
    assert options["mustexist"] is True
    assert root.log[-1] == ("destroy",)


def test_the_tk_dialog_leaves_the_start_folder_to_windows_when_there_is_none(fake_tkinter):
    folders.show_tk_dialog(None)
    assert "initialdir" not in fake_tkinter.asked[0]


def test_the_tk_root_is_destroyed_even_when_the_dialog_fails(fake_tkinter):
    fake_tkinter.outcome["result"] = RuntimeError("Tcl broke")
    with pytest.raises(RuntimeError, match="Tcl broke"):
        folders.show_tk_dialog(None)
    assert FakeTk.instances[0].log[-1] == ("destroy",)


def test_the_default_picker_runs_the_tk_dialog_on_its_thread(fake_tkinter):
    picker = FolderPicker()
    try:
        assert picker.pick(None) == os.path.normpath("C:/picked")
    finally:
        picker.close()
    (root,) = FakeTk.instances
    assert root.log[0][1] is not threading.current_thread()


# Opening Explorer, with Popen replaced at the boundary.


class FakePopen:
    launched: list["FakePopen"] = []

    def __init__(self, args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.waited = threading.Event()
        FakePopen.launched.append(self)

    def wait(self, timeout=None):
        self.waited.set()
        return 1  # explorer.exe exits with 1 even when it worked


@pytest.fixture
def popen(monkeypatch):
    FakePopen.launched = []
    monkeypatch.setattr(folders.subprocess, "Popen", FakePopen)
    monkeypatch.setenv("SystemRoot", "C:\\Windows")
    return FakePopen.launched


def test_a_file_is_shown_selected_in_its_folder(popen, tmp_path):
    file = tmp_path / "My video, part 1.mp4"
    file.write_bytes(b"")
    open_in_explorer(file)
    (launched,) = popen
    # One command line: Explorer parses /select,"<path>" itself, so a list (which Python would
    # quote as a whole, "/select,<path>") would not work for paths with spaces.
    assert launched.args == f'"C:\\Windows\\explorer.exe" /select,"{file}"'


def test_a_folder_is_opened(popen, tmp_path):
    folder = tmp_path / "Snag videos"
    folder.mkdir()
    open_in_explorer(folder)
    (launched,) = popen
    assert launched.args == f'"C:\\Windows\\explorer.exe" "{folder}"'


def test_a_relative_path_is_made_absolute(popen, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "clip.mp4").write_bytes(b"")
    open_in_explorer(Path("clip.mp4"))
    assert popen[0].args.endswith(f'/select,"{tmp_path / "clip.mp4"}"')


def test_a_missing_path_raises_and_opens_nothing(popen, tmp_path):
    with pytest.raises(FileNotFoundError):
        open_in_explorer(tmp_path / "gone.mp4")
    assert popen == []


def test_explorer_is_not_tied_to_snag_and_is_reaped(popen, tmp_path):
    open_in_explorer(tmp_path)
    (launched,) = popen  # plain Popen, not snag.proc.spawn: Explorer windows must outlive Snag
    assert launched.waited.wait(WAIT)  # reaped in the background, so no ResourceWarning later


# Dismissing the open dialog at shutdown, against real (never shown) windows.

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _user32.CreateWindowExW.argtypes = (
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
    )
    _user32.CreateWindowExW.restype = wintypes.HWND
    _user32.DestroyWindow.argtypes = (wintypes.HWND,)
    _user32.PeekMessageW.argtypes = (
        ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT,
    )
    _WM_CLOSE = 0x0010
    _PM_REMOVE = 0x0001


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 windows")
def test_dismissing_posts_close_to_the_threads_dialogs_only():
    """A thread owning one dialog-class window (what the native folder dialog is) and one other
    window, like Tk's hidden root. Only the dialog may be told to close."""
    ready = threading.Event()
    go = threading.Event()
    received: dict[str, bool] = {}

    def owner() -> None:
        dialog = _user32.CreateWindowExW(0, "#32770", "Choose a folder", 0, 0, 0, 0, 0, None, None, None, None)
        other = _user32.CreateWindowExW(0, "STATIC", "Tk root", 0, 0, 0, 0, 0, None, None, None, None)
        try:
            assert dialog and other, ctypes.WinError(ctypes.get_last_error())
            ready.set()
            assert go.wait(WAIT)
            msg = wintypes.MSG()
            for name, hwnd in (("dialog", dialog), ("other", other)):
                received[name] = bool(_user32.PeekMessageW(ctypes.byref(msg), hwnd, _WM_CLOSE, _WM_CLOSE, _PM_REMOVE))
        finally:
            _user32.DestroyWindow(dialog)
            _user32.DestroyWindow(other)

    thread = threading.Thread(target=owner, daemon=True)
    thread.start()
    assert ready.wait(WAIT)
    folders.dismiss_dialogs(thread.native_id)
    go.set()
    thread.join(WAIT)
    assert received == {"dialog": True, "other": False}
