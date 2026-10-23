import contextlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from snag.platform import startup
from snag.platform.icon import ICON_FILE
from snag.platform.startup import (
    STARTUP_APPROVED_KEY,
    Shortcut,
    ShortcutError,
    create_shortcut,
    remove_shortcut,
    set_start_with_windows,
    snag_shortcut,
    start_menu_shortcut_path,
    starts_with_windows,
    startup_shortcut_path,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

windows_only = pytest.mark.skipif(
    sys.platform != "win32", reason="shortcuts are made by the Windows shell (IShellLinkW, through PowerShell)",
)


def test_snags_shortcut_runs_the_venvs_pythonw_from_the_repo():
    shortcut = snag_shortcut(open_tab=False)
    assert shortcut == Shortcut(
        target=REPO_ROOT / ".venv" / "Scripts" / "pythonw.exe",
        args="-m snag",
        workdir=REPO_ROOT,
        icon=ICON_FILE,
    )


def test_the_start_menu_shortcut_also_opens_the_tab():
    assert snag_shortcut(open_tab=True).args == "-m snag --open"


def test_a_different_repo_can_be_named(tmp_path):
    shortcut = snag_shortcut(open_tab=False, repo=tmp_path)
    assert shortcut.target == tmp_path / ".venv" / "Scripts" / "pythonw.exe"
    assert shortcut.workdir == tmp_path
    assert shortcut.icon == tmp_path / "snag" / "platform" / "snag.ico"


@windows_only
def test_the_shortcuts_live_in_the_users_start_menu():
    start_menu = start_menu_shortcut_path()
    autostart = startup_shortcut_path()
    assert start_menu.name == autostart.name == "Snag.lnk"
    assert start_menu.parent.name == "Programs"
    assert autostart.parent.name == "Startup"
    assert autostart.parent.parent == start_menu.parent  # Startup is inside the Start menu's Programs


def test_without_the_shell_the_shortcut_folders_come_from_appdata(monkeypatch, tmp_path):
    monkeypatch.setattr(startup, "known_folder", lambda folder_id: None)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    programs = tmp_path / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    assert start_menu_shortcut_path() == programs / "Snag.lnk"
    assert startup_shortcut_path() == programs / "Startup" / "Snag.lnk"


# Real shortcuts, always inside tmp_path.

# Snag only makes shortcuts; the tests read them back through the same Unicode interface
# (IShellLinkW, declared in startup._SHELL_LINK, whose using directives cover this part too).
_READER = r"""
namespace Snag {
    public static class ShortcutReader {
        public static string[] Read(string lnk) {
            const int size = 32768;
            const uint rawPath = 0x4;  // SLGP_RAWPATH: the target as saved, environment variables unexpanded
            var link = (IShellLinkW)new ShellLink();
            ((IPersistFile)link).Load(lnk, 0);
            var target = new StringBuilder(size);
            var args = new StringBuilder(size);
            var workdir = new StringBuilder(size);
            var icon = new StringBuilder(size);
            int index;
            link.GetPath(target, size, IntPtr.Zero, rawPath);
            link.GetArguments(args, size);
            link.GetWorkingDirectory(workdir, size);
            link.GetIconLocation(icon, size, out index);
            return new string[] { target.ToString(), args.ToString(), workdir.ToString(), icon.ToString() };
        }
    }
}
"""
_READ_SCRIPT = startup._PREAMBLE + """
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$found = [Snag.ShortcutReader]::Read($env:SNAG_LNK)
ConvertTo-Json -Compress @{ target = $found[0]; args = $found[1]; workdir = $found[2]; icon = $found[3] }
"""


def read_shortcut(lnk: Path) -> Shortcut:
    """What the shortcut lnk runs, read back through IShellLinkW."""
    output = startup._run_powershell(_READ_SCRIPT, {"SNAG_SHELL_LINK": startup._SHELL_LINK + _READER, "SNAG_LNK": str(lnk)})
    found = json.loads(output)
    return Shortcut(target=Path(found["target"]), args=found["args"] or "", workdir=Path(found["workdir"]),
                    icon=Path(found["icon"]))

# Spaces, an apostrophe, a dollar sign and a backtick, none of which may be taken as PowerShell
# syntax; accented letters; and Japanese, outside the code page of an English Windows.
AWKWARD = "Snag's $HOME `n Ünïcødé 日本"


@pytest.fixture
def fake_repo(tmp_path):
    repo = tmp_path / AWKWARD / "repo"
    (repo / ".venv" / "Scripts").mkdir(parents=True)
    (repo / ".venv" / "Scripts" / "pythonw.exe").write_bytes(b"")
    (repo / "snag" / "platform").mkdir(parents=True)
    (repo / "snag" / "platform" / "snag.ico").write_bytes(ICON_FILE.read_bytes())
    return repo


@windows_only
def test_create_then_read_back_a_shortcut(tmp_path, fake_repo):
    lnk = tmp_path / "links" / f"{AWKWARD}.lnk"  # its folder doesn't exist yet
    wanted = snag_shortcut(open_tab=True, repo=fake_repo)
    create_shortcut(lnk, wanted.target, wanted.args, wanted.workdir, wanted.icon)
    assert lnk.is_file()
    assert read_shortcut(lnk) == wanted


@windows_only
def test_windows_script_host_reads_the_shortcut_too(tmp_path):
    """An independent reader: WScript.Shell (ANSI-only, so plain paths here) sees the same."""
    lnk = tmp_path / "Snag.lnk"
    wanted = snag_shortcut(open_tab=True)
    create_shortcut(lnk, wanted.target, wanted.args, wanted.workdir, wanted.icon)
    script = (
        "$l = (New-Object -ComObject WScript.Shell).CreateShortcut($env:LNK); "
        "$l.TargetPath; $l.Arguments; $l.WorkingDirectory; $l.IconLocation"
    )
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        env={**os.environ, "LNK": str(lnk)}, capture_output=True, text=True, timeout=60, check=True,
    )
    assert completed.stdout.splitlines() == [
        str(wanted.target), wanted.args, str(wanted.workdir), f"{wanted.icon},0",
    ]


@windows_only
def test_creating_again_replaces_the_shortcut(tmp_path, fake_repo):
    lnk = tmp_path / "Snag.lnk"
    first = snag_shortcut(open_tab=True, repo=fake_repo)
    create_shortcut(lnk, first.target, first.args, first.workdir, first.icon)
    create_shortcut(lnk, first.target, "-m snag", first.workdir, first.icon)
    assert read_shortcut(lnk).args == "-m snag"


@windows_only
def test_arguments_may_be_empty(tmp_path, fake_repo):
    lnk = tmp_path / "Snag.lnk"
    wanted = snag_shortcut(open_tab=False, repo=fake_repo)
    create_shortcut(lnk, wanted.target, "", wanted.workdir, wanted.icon)
    assert read_shortcut(lnk).args == ""


def test_a_shortcut_path_must_end_in_lnk(tmp_path):
    with pytest.raises(ValueError, match=".lnk"):
        create_shortcut(tmp_path / "Snag.txt", tmp_path, "", tmp_path, tmp_path)


@windows_only
def test_a_shortcut_folder_that_cannot_be_made_raises_oserror(tmp_path, fake_repo):
    blocker = tmp_path / "not-a-folder"
    blocker.write_bytes(b"")
    wanted = snag_shortcut(open_tab=False, repo=fake_repo)
    with pytest.raises(OSError):
        # Its "folder" is a file, so the shortcut has nowhere to go.
        create_shortcut(blocker / "Snag.lnk", wanted.target, wanted.args, wanted.workdir, wanted.icon)


@windows_only
def test_a_powershell_failure_raises_shortcut_error_with_its_message(tmp_path, monkeypatch):
    monkeypatch.setattr(startup, "_CREATE_SCRIPT", startup._PREAMBLE + "throw 'the shortcut broke'")
    with pytest.raises(ShortcutError, match="the shortcut broke") as raised:
        create_shortcut(tmp_path / "Snag.lnk", tmp_path, "", tmp_path, tmp_path)
    assert "CLIXML" not in str(raised.value)  # plain text, not PowerShell's serialized errors


@windows_only
def test_remove_shortcut_deletes_it_and_ignores_a_missing_one(tmp_path, fake_repo):
    lnk = tmp_path / "Snag.lnk"
    wanted = snag_shortcut(open_tab=False, repo=fake_repo)
    create_shortcut(lnk, wanted.target, wanted.args, wanted.workdir, wanted.icon)
    remove_shortcut(lnk)
    assert not lnk.exists()
    remove_shortcut(lnk)


# Task Manager's "disabled" mark, in a scratch registry key: the real StartupApproved key is never
# written by the tests.

if sys.platform == "win32":
    import winreg

# What Task Manager writes when the user disables a startup item: 03, then a timestamp.
DISABLED_MARK = bytes([3, 0, 0, 0]) + bytes(8)


@pytest.fixture
def approved_key():
    """A scratch stand-in for StartupApproved\\StartupFolder, under HKCU\\Software\\SnagTest."""
    root = rf"Software\SnagTest\{uuid.uuid4().hex}"
    key = rf"{root}\StartupFolder"
    winreg.CreateKey(winreg.HKEY_CURRENT_USER, key).Close()
    try:
        yield key
    finally:
        for path in (key, root, r"Software\SnagTest"):
            with contextlib.suppress(OSError):  # SnagTest stays if another run's key is still in it
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)


def _mark_disabled(key: str, name: str) -> None:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as handle:
        winreg.SetValueEx(handle, name, 0, winreg.REG_BINARY, DISABLED_MARK)


def _marks(key: str) -> dict[str, bytes]:
    found = {}
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:
        for index in range(winreg.QueryInfoKey(handle)[1]):
            name, value, _ = winreg.EnumValue(handle, index)
            found[name] = value
    return found


def test_the_disabled_marks_live_in_task_managers_key():
    assert STARTUP_APPROVED_KEY == r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\StartupFolder"


@windows_only
def test_start_with_windows_adds_and_removes_the_startup_shortcut(tmp_path, approved_key):
    lnk = tmp_path / "Startup" / "Snag.lnk"
    set_start_with_windows(True, lnk, approved_key=approved_key)
    assert read_shortcut(lnk) == snag_shortcut(open_tab=False)
    set_start_with_windows(False, lnk, approved_key=approved_key)
    assert not lnk.exists()


@windows_only
def test_enabling_clears_task_managers_disabled_mark(tmp_path, approved_key):
    """A shortcut the user disabled in Task Manager stays disabled while its mark is there, so
    turning the setting on clears it. Other programs' marks are left alone."""
    lnk = tmp_path / "Startup" / "Snag.lnk"
    _mark_disabled(approved_key, "Snag.lnk")
    _mark_disabled(approved_key, "Other.lnk")
    set_start_with_windows(True, lnk, approved_key=approved_key)
    assert lnk.is_file()
    assert _marks(approved_key) == {"Other.lnk": DISABLED_MARK}


@windows_only
def test_disabling_removes_the_shortcut_and_its_mark(tmp_path, approved_key):
    lnk = tmp_path / "Startup" / "Snag.lnk"
    set_start_with_windows(True, lnk, approved_key=approved_key)
    _mark_disabled(approved_key, "Snag.lnk")  # the user has since disabled it in Task Manager
    set_start_with_windows(False, lnk, approved_key=approved_key)
    assert not lnk.exists()
    assert _marks(approved_key) == {}


@windows_only
def test_no_mark_and_no_key_are_both_fine(tmp_path, approved_key):
    lnk = tmp_path / "Startup" / "Snag.lnk"
    set_start_with_windows(True, lnk, approved_key=approved_key)  # the key exists, the value doesn't
    missing = rf"{approved_key}\missing"
    set_start_with_windows(False, lnk, approved_key=missing)  # the key itself is missing
    set_start_with_windows(True, lnk, approved_key=missing)
    assert lnk.is_file()


# What Task Manager writes when the user enables a startup item again: 02, then zeros.
ENABLED_MARK = bytes([2, 0, 0, 0]) + bytes(8)


@windows_only
def test_snag_starts_with_windows_while_its_startup_shortcut_is_there(tmp_path, approved_key):
    """Reality, not a stored setting: the shortcut's file (its content doesn't matter here)."""
    lnk = tmp_path / "Startup" / "Snag.lnk"
    assert starts_with_windows(lnk, approved_key=approved_key) is False
    lnk.parent.mkdir()
    lnk.write_bytes(b"")
    assert starts_with_windows(lnk, approved_key=approved_key) is True
    lnk.unlink()
    assert starts_with_windows(lnk, approved_key=approved_key) is False


@windows_only
def test_a_startup_shortcut_disabled_in_task_manager_does_not_count(tmp_path, approved_key):
    lnk = tmp_path / "Startup" / "Snag.lnk"
    lnk.parent.mkdir()
    lnk.write_bytes(b"")
    _mark_disabled(approved_key, "Other.lnk")  # another program's mark changes nothing
    assert starts_with_windows(lnk, approved_key=approved_key) is True
    _mark_disabled(approved_key, "Snag.lnk")
    assert starts_with_windows(lnk, approved_key=approved_key) is False
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, approved_key, 0, winreg.KEY_SET_VALUE) as handle:
        winreg.SetValueEx(handle, "Snag.lnk", 0, winreg.REG_BINARY, ENABLED_MARK)  # enabled again there
    assert starts_with_windows(lnk, approved_key=approved_key) is True
    assert starts_with_windows(lnk, approved_key=rf"{approved_key}\missing") is True  # no key: no mark


def test_starts_with_windows_defaults_to_the_real_startup_folder_and_key(monkeypatch, tmp_path):
    asked: list[tuple] = []
    lnk = tmp_path / "Startup" / "Snag.lnk"
    lnk.parent.mkdir()
    lnk.write_bytes(b"")
    monkeypatch.setattr(startup, "startup_shortcut_path", lambda: lnk)
    monkeypatch.setattr(startup, "startup_disabled", lambda name, key: asked.append((name, key)) or False)
    assert starts_with_windows() is True
    assert asked == [("Snag.lnk", STARTUP_APPROVED_KEY)]


def test_start_with_windows_defaults_to_the_real_startup_folder_and_key(monkeypatch, tmp_path):
    made: list[tuple] = []
    cleared: list[tuple] = []
    monkeypatch.setattr(startup, "startup_shortcut_path", lambda: tmp_path / "Startup" / "Snag.lnk")
    monkeypatch.setattr(startup, "create_shortcut", lambda *args, **kwargs: made.append(args))
    monkeypatch.setattr(startup, "clear_disabled_mark", lambda name, key: cleared.append((name, key)))
    set_start_with_windows(True)
    assert made[0][0] == tmp_path / "Startup" / "Snag.lnk"
    assert cleared == [("Snag.lnk", STARTUP_APPROVED_KEY)]


# How PowerShell is run, and what happens when it misbehaves.


class FakeRun:
    """Stands in for subprocess.run: records the call and answers as told."""

    def __init__(self, stdout=b"", returncode=0, make=None, error=None):
        self.calls: list[tuple] = []
        self.stdout, self.returncode, self.make, self.error = stdout, returncode, make, error

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        if self.error is not None:
            raise self.error
        if self.make is not None:
            self.make.write_bytes(b"")
        return subprocess.CompletedProcess(args, self.returncode, self.stdout, b"")


def test_powershell_reads_the_script_from_stdin_with_the_paths_in_the_environment(monkeypatch, tmp_path):
    lnk = tmp_path / "Snag.lnk"
    fake = FakeRun(make=lnk)
    monkeypatch.setattr(startup.subprocess, "run", fake)
    create_shortcut(lnk, tmp_path / "pythonw.exe", "-m snag", tmp_path, tmp_path / "snag.ico")
    ((args, kwargs),) = fake.calls
    assert args[-2:] == ["-Command", "-"]
    assert "-NoProfile" in args and "-NonInteractive" in args
    assert "-EncodedCommand" not in args and "-ExecutionPolicy" not in args
    assert str(tmp_path) not in " ".join(args)
    script = kwargs["input"].decode("ascii")
    assert "[Snag.Shortcut]::Save" in script and str(tmp_path) not in script
    assert kwargs["env"]["SNAG_LNK"] == str(lnk)
    assert kwargs["env"]["SNAG_LNK_TARGET"] == str(tmp_path / "pythonw.exe")


def test_the_scripts_are_plain_ascii():
    """PowerShell decodes its stdin with the console's code page; anything outside ASCII (paths
    included) travels in environment variables instead."""
    for script in (startup._SHELL_LINK, startup._CREATE_SCRIPT, _READER, _READ_SCRIPT):
        assert script.isascii()


def test_a_powershell_timeout_raises_shortcut_error(monkeypatch, tmp_path):
    monkeypatch.setattr(startup.subprocess, "run", FakeRun(error=subprocess.TimeoutExpired(["powershell.exe"], 60)))
    with pytest.raises(ShortcutError, match="didn't finish"):
        create_shortcut(tmp_path / "Snag.lnk", tmp_path, "", tmp_path, tmp_path)


def test_a_shortcut_missing_after_saving_raises_shortcut_error(monkeypatch, tmp_path):
    monkeypatch.setattr(startup.subprocess, "run", FakeRun())  # "succeeds" without saving anything
    with pytest.raises(ShortcutError, match="wasn't saved"):
        create_shortcut(tmp_path / "Snag.lnk", tmp_path, "", tmp_path, tmp_path)


def test_a_shortcut_path_too_long_for_windows_is_refused_before_anything_runs(monkeypatch, tmp_path):
    monkeypatch.setattr(startup.subprocess, "run", lambda *args, **kwargs: pytest.fail("PowerShell ran"))
    folder = tmp_path / "deep"
    lnk = folder / ("x" * (260 - len(str(folder)) - 1 - len(".lnk")) + ".lnk")
    assert len(str(lnk)) == 260
    with pytest.raises(ShortcutError, match="too long") as raised:
        create_shortcut(lnk, tmp_path, "", tmp_path, tmp_path)
    assert "260" in str(raised.value)
    assert not folder.exists()


def test_a_shortcut_path_just_under_the_limit_is_accepted(monkeypatch, tmp_path):
    folder = tmp_path / "deep"
    lnk = folder / ("x" * (259 - len(str(folder)) - 1 - len(".lnk")) + ".lnk")
    assert len(str(lnk)) == 259
    monkeypatch.setattr(startup.subprocess, "run", FakeRun(make=lnk))
    create_shortcut(lnk, tmp_path, "", tmp_path, tmp_path)
    assert lnk.is_file()


def test_the_length_limit_counts_utf16_units_not_characters(monkeypatch, tmp_path):
    """Windows' 260 counts UTF-16 code units: an emoji is one character to Python but two units."""
    monkeypatch.setattr(startup.subprocess, "run", lambda *args, **kwargs: pytest.fail("PowerShell ran"))
    folder = tmp_path / "deep"
    emoji = "🎬" * 20  # 20 characters, 40 units
    stem = "x" * (259 - len(str(folder)) - 1 - len(".lnk") - len(emoji)) + emoji
    lnk = folder / (stem + ".lnk")
    assert len(str(lnk)) == 259  # under the limit by characters
    assert len(str(lnk).encode("utf-16-le")) // 2 == 279  # over it in UTF-16 units
    with pytest.raises(ShortcutError, match="too long") as raised:
        create_shortcut(lnk, tmp_path, "", tmp_path, tmp_path)
    assert "279" in str(raised.value)


# The command line setup.ps1 uses: python -m snag.platform.startup status | create | refresh | remove


@pytest.fixture
def shortcut_calls(monkeypatch, tmp_path):
    """Where the CLI's shortcuts would go (tmp_path), with nothing made: records each call."""
    calls: list[tuple] = []
    monkeypatch.setattr(startup, "start_menu_shortcut_path", lambda: tmp_path / "Programs" / "Snag.lnk")
    monkeypatch.setattr(startup, "startup_shortcut_path", lambda: tmp_path / "Startup" / "Snag.lnk")
    monkeypatch.setattr(startup, "create_shortcut", lambda *args: calls.append(("create", *args)))
    monkeypatch.setattr(startup, "remove_shortcut", lambda lnk: calls.append(("remove", lnk)))
    monkeypatch.setattr(startup, "clear_disabled_mark", lambda name, key: calls.append(("clear", name, key)))
    return calls


def test_the_cli_creates_both_shortcuts(shortcut_calls, tmp_path, capsys):
    assert startup.main(["create"]) == 0
    menu, at_startup = snag_shortcut(open_tab=True), snag_shortcut(open_tab=False)
    assert shortcut_calls == [
        ("create", tmp_path / "Programs" / "Snag.lnk", menu.target, menu.args, menu.workdir, menu.icon),
        ("create", tmp_path / "Startup" / "Snag.lnk", at_startup.target, at_startup.args, at_startup.workdir,
         at_startup.icon),
        ("clear", "Snag.lnk", STARTUP_APPROVED_KEY),
    ]
    out = capsys.readouterr().out
    assert str(tmp_path / "Programs" / "Snag.lnk") in out and str(tmp_path / "Startup" / "Snag.lnk") in out


def test_the_cli_refreshes_the_start_menu_shortcut_and_leaves_start_with_windows_off(shortcut_calls, tmp_path,
                                                                                       capsys):
    """setup.ps1 run again: Start with Windows was turned off, so it stays off."""
    assert startup.main(["refresh"]) == 0
    menu = snag_shortcut(open_tab=True)
    assert shortcut_calls == [
        ("create", tmp_path / "Programs" / "Snag.lnk", menu.target, menu.args, menu.workdir, menu.icon),
    ]
    out = capsys.readouterr().out
    assert str(tmp_path / "Programs" / "Snag.lnk") in out and "Startup" not in out


def test_the_cli_refreshes_an_existing_startup_shortcut_without_touching_its_mark(shortcut_calls, tmp_path):
    """Start with Windows on: the shortcut is re-made (the Snag folder may have moved). A mark Task
    Manager keeps for it stays: if the user disabled it there, it stays disabled."""
    (tmp_path / "Startup").mkdir()
    (tmp_path / "Startup" / "Snag.lnk").write_bytes(b"")
    assert startup.main(["refresh"]) == 0
    menu, at_startup = snag_shortcut(open_tab=True), snag_shortcut(open_tab=False)
    assert shortcut_calls == [
        ("create", tmp_path / "Programs" / "Snag.lnk", menu.target, menu.args, menu.workdir, menu.icon),
        ("create", tmp_path / "Startup" / "Snag.lnk", at_startup.target, at_startup.args, at_startup.workdir,
         at_startup.icon),
    ]


def test_the_cli_removes_both_shortcuts(shortcut_calls, tmp_path):
    assert startup.main(["remove"]) == 0
    assert shortcut_calls == [
        ("remove", tmp_path / "Programs" / "Snag.lnk"),
        ("remove", tmp_path / "Startup" / "Snag.lnk"),
        ("clear", "Snag.lnk", STARTUP_APPROVED_KEY),
    ]


def test_the_cli_reports_a_failure_plainly(shortcut_calls, monkeypatch, capsys):
    def refuse(*args):
        raise ShortcutError("PowerShell failed (1): no")

    monkeypatch.setattr(startup, "create_shortcut", refuse)
    assert startup.main(["create"]) == 1
    assert "Couldn't make Snag's shortcuts: PowerShell failed (1): no" in capsys.readouterr().err


def test_the_cli_reports_where_the_shortcuts_are_and_whether_each_exists(shortcut_calls, tmp_path, capsys):
    """setup.ps1 asks before making them: no Start menu shortcut yet means the first install."""
    assert startup.main(["status"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "start_menu": {"path": str(tmp_path / "Programs" / "Snag.lnk"), "exists": False},
        "startup": {"path": str(tmp_path / "Startup" / "Snag.lnk"), "exists": False},
    }
    (tmp_path / "Programs").mkdir()
    (tmp_path / "Programs" / "Snag.lnk").write_bytes(b"")
    assert startup.main(["status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["start_menu"]["exists"] is True and status["startup"]["exists"] is False
    assert shortcut_calls == []  # it only looks


def test_the_status_is_ascii_whatever_the_folder_is_called(monkeypatch, tmp_path, capsys):
    """setup.ps1 reads it through the console's code page, which may not have the folder's characters."""
    folder = tmp_path / "Démarrer 開始"
    monkeypatch.setattr(startup, "start_menu_shortcut_path", lambda: folder / "Snag.lnk")
    monkeypatch.setattr(startup, "startup_shortcut_path", lambda: folder / "Startup" / "Snag.lnk")
    assert startup.main(["status"]) == 0
    out = capsys.readouterr().out
    assert out.isascii()
    assert json.loads(out)["start_menu"]["path"] == str(folder / "Snag.lnk")


def test_under_the_test_hook_snag_start_menu_stands_in_for_the_start_menu(monkeypatch, tmp_path):
    monkeypatch.setenv("SNAG_START_MENU", str(tmp_path))
    monkeypatch.setenv("SNAG_TEST_HOOK", "1")
    assert start_menu_shortcut_path() == tmp_path / "Snag.lnk"
    assert startup_shortcut_path() == tmp_path / "Startup" / "Snag.lnk"
    monkeypatch.setenv("SNAG_TEST_HOOK", "0")  # without the hook, it is ignored
    assert start_menu_shortcut_path().parent != tmp_path
    assert startup_shortcut_path().parent != tmp_path / "Startup"


def test_the_cli_status_runs_as_a_module_against_the_stand_in(tmp_path):
    (tmp_path / "Startup").mkdir()
    (tmp_path / "Startup" / "Snag.lnk").write_bytes(b"")
    done = subprocess.run([sys.executable, "-m", "snag.platform.startup", "status"], capture_output=True,
                          text=True, cwd=REPO_ROOT, timeout=60,
                          env={**os.environ, "SNAG_TEST_HOOK": "1", "SNAG_START_MENU": str(tmp_path)})
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == {
        "start_menu": {"path": str(tmp_path / "Snag.lnk"), "exists": False},
        "startup": {"path": str(tmp_path / "Startup" / "Snag.lnk"), "exists": True},
    }


def test_the_cli_runs_as_a_module():
    done = subprocess.run([sys.executable, "-m", "snag.platform.startup", "--help"], capture_output=True,
                          text=True, cwd=REPO_ROOT, timeout=60)
    assert done.returncode == 0
    assert all(action in done.stdout for action in ("status", "create", "refresh", "remove"))
