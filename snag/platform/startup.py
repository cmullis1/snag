"""Snag's shortcuts: one in the Start menu (opens the app tab) and one in the Startup folder
(starts Snag with Windows, quietly in the tray). Both run the project's own pythonw.exe, so no
console window opens, from the repo folder.

Shortcuts are made by PowerShell, through the shell's IShellLinkW (see _SHELL_LINK). PowerShell
is started with subprocess.run, not snag.proc.spawn: it is short-lived and needs none of spawn's
ties to Snag. It takes a second or so to start, so call these off the event loop
(asyncio.to_thread). The script is fed to PowerShell on stdin (-Command -), a plain invocation
that security tools don't take for an obfuscated one as they do -EncodedCommand. The paths reach
PowerShell as environment variables, never pasted into the script, so no folder name can be read
as PowerShell code, and the script itself stays ASCII, which any console code page reads alike.

Task Manager's Startup tab doesn't delete a shortcut the user disables there: it records the
choice in the registry (see STARTUP_APPROVED_KEY), and Windows skips the shortcut while that mark
stands. So turning Start with Windows on clears the mark, and turning it off removes both.

Whether Snag starts with Windows is read from Windows itself (starts_with_windows()): the Startup
shortcut is there and Task Manager hasn't disabled it. No stored setting stands in for it, so
Settings never says on while Windows won't start Snag, or off while it will.

setup.ps1 uses this module's command line (main()):
    python -m snag.platform.startup status    where the two shortcuts are and whether each exists,
                                              as one line of JSON (ASCII: any console reads it)
    python -m snag.platform.startup create    the Start menu and Startup shortcuts (the first
                                              install: no Start menu shortcut yet)
    python -m snag.platform.startup refresh   the Start menu shortcut made again, and the Startup one
                                              too only if it is there (setup run again: Start with
                                              Windows stays as the user left it)
    python -m snag.platform.startup remove    both gone
Only setup.ps1 makes the Start menu shortcut, so it is what tells the first install from a re-run:
Snag's database doesn't, since Snag makes it whenever it runs, setup or no setup.

Under Snag's test hook (snag.hook), SNAG_START_MENU names a folder that stands in for the user's
Start menu Programs folder, its Startup folder inside it, so setup.ps1's tests never see or touch
the user's own shortcuts."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from snag.hook import test_hook_on
from snag.paths import known_folder

REPO_ROOT = Path(__file__).resolve().parents[2]
SHORTCUT_NAME = "Snag.lnk"

FOLDERID_PROGRAMS = "{A77F5D77-2E2B-44C3-A6A2-ABA601054A51}"  # the user's Start menu\Programs
FOLDERID_STARTUP = "{B97D20BB-F46A-4C97-BA10-5E3608430854}"  # the user's Startup folder

# Under HKEY_CURRENT_USER: one value per Startup folder shortcut, named after its file, that Task
# Manager writes when the user enables or disables it (first byte 02 enabled, 03 disabled).
STARTUP_APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\StartupFolder"

_POWERSHELL_TIMEOUT = 60.0  # seconds; generous for a cold start on a busy PC
_MAX_PATH = 260  # a shortcut's full path must be shorter, leaving room for the closing NUL


class ShortcutError(OSError):
    """Windows couldn't make a shortcut; the message says why (often PowerShell's own)."""


@dataclass(frozen=True)
class Shortcut:
    target: Path
    args: str
    workdir: Path
    icon: Path


def snag_shortcut(*, open_tab: bool, repo: Path = REPO_ROOT) -> Shortcut:
    """How a shortcut starts Snag: the venv's pythonw.exe -m snag from the repo, with --open for
    the Start menu shortcut (the user wants to see Snag) and without it at startup."""
    return Shortcut(
        target=repo / ".venv" / "Scripts" / "pythonw.exe",
        args="-m snag --open" if open_tab else "-m snag",
        workdir=repo,
        icon=repo / "snag" / "platform" / "snag.ico",
    )


def _test_start_menu() -> Path | None:
    """The folder standing in for the Start menu's Programs folder under the test hook, or None."""
    stand_in = os.environ.get("SNAG_START_MENU")
    return Path(stand_in) if stand_in and test_hook_on() else None


def _programs_folder() -> Path:
    stand_in = _test_start_menu()
    if stand_in is not None:
        return stand_in
    found = known_folder(FOLDERID_PROGRAMS)
    if found is not None:
        return found
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return base / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def start_menu_shortcut_path() -> Path:
    return _programs_folder() / SHORTCUT_NAME


def startup_shortcut_path() -> Path:
    stand_in = _test_start_menu()
    if stand_in is not None:
        return stand_in / "Startup" / SHORTCUT_NAME
    return (known_folder(FOLDERID_STARTUP) or _programs_folder() / "Startup") / SHORTCUT_NAME


# Windows Script Host's WScript.Shell would be shorter, but it is ANSI-only: a path with
# characters outside the PC's code page (say a Japanese folder name on an English Windows) makes
# it fail, or worse, save "?" in their place and leave a shortcut that silently doesn't work. So
# the script uses the shell's own Unicode interface, IShellLinkW, through a small C# helper.
_SHELL_LINK = r"""
using System;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Text;

namespace Snag {
    [ComImport, Guid("000214F9-0000-0000-C000-000000000046"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IShellLinkW {
        void GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder file, int size, IntPtr data, uint flags);
        void GetIDList(out IntPtr idList);
        void SetIDList(IntPtr idList);
        void GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder name, int size);
        void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string name);
        void GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder dir, int size);
        void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string dir);
        void GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder args, int size);
        void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string args);
        void GetHotkey(out short hotkey);
        void SetHotkey(short hotkey);
        void GetShowCmd(out int showCmd);
        void SetShowCmd(int showCmd);
        void GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int size, out int index);
        void SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string path, int index);
        void SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string path, uint reserved);
        void Resolve(IntPtr window, uint flags);
        void SetPath([MarshalAs(UnmanagedType.LPWStr)] string file);
    }

    [ComImport, Guid("00021401-0000-0000-C000-000000000046")]
    class ShellLink {}

    public static class Shortcut {
        public static void Save(string lnk, string target, string args, string workdir, string icon) {
            var link = (IShellLinkW)new ShellLink();
            link.SetPath(target);
            link.SetArguments(args);
            link.SetWorkingDirectory(workdir);
            link.SetIconLocation(icon, 0);
            link.SetDescription("Snag");
            ((IPersistFile)link).Save(lnk, true);
        }
    }
}
"""

# Errors go to stderr as plain text: PowerShell's own error output would be serialized XML.
_PREAMBLE = """
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
trap { [Console]::Error.WriteLine($_.Exception.Message); exit 1 }
Add-Type -TypeDefinition $env:SNAG_SHELL_LINK
"""

_CREATE_SCRIPT = _PREAMBLE + """
[Snag.Shortcut]::Save($env:SNAG_LNK, $env:SNAG_LNK_TARGET, [string]$env:SNAG_LNK_ARGS, $env:SNAG_LNK_WORKDIR, $env:SNAG_LNK_ICON)
"""


def _powershell_exe() -> Path:
    root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    return root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"


def _run_powershell(script: str, env: dict[str, str]) -> str:
    """Run script and return its output; raise ShortcutError with PowerShell's message if it
    fails or doesn't finish in time.

    Fed on stdin, PowerShell runs the script a line at a time, as if typed at its prompt, so a
    trap on one line wouldn't cover the next. Wrapped in one script block, it runs as a whole;
    the blank line after it tells PowerShell the block is complete."""
    try:
        completed = subprocess.run(
            [str(_powershell_exe()), "-NoLogo", "-NoProfile", "-NonInteractive", "-OutputFormat", "Text",
             "-Command", "-"],
            input=f"& {{\n{script}\n}}\n\n".encode("ascii"),
            env={**os.environ, "SNAG_SHELL_LINK": _SHELL_LINK, **env}, capture_output=True,
            timeout=_POWERSHELL_TIMEOUT, creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except subprocess.TimeoutExpired as exc:
        raise ShortcutError(f"PowerShell didn't finish within {_POWERSHELL_TIMEOUT:g} seconds") from exc
    stdout = completed.stdout.decode("utf-8", "replace")
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", "replace").strip() or stdout.strip()
        raise ShortcutError(f"PowerShell failed ({completed.returncode}): {message}")
    return stdout


def _check_lnk(lnk: Path) -> None:
    if lnk.suffix.lower() != ".lnk":
        raise ValueError(f"A shortcut's file name must end in .lnk: {lnk}")


def create_shortcut(lnk: Path, target: Path, args: str, workdir: Path, icon: Path) -> None:
    """Make (or replace) the shortcut lnk, creating its folder if needed. Raises OSError
    (ShortcutError when Windows refuses, the path is too long, or no shortcut was saved)."""
    _check_lnk(lnk)
    # Windows counts UTF-16 code units, in which a character outside the Basic Multilingual Plane
    # (most emoji) takes two.
    units = len(str(lnk).encode("utf-16-le", "surrogatepass")) // 2
    if units >= _MAX_PATH:
        raise ShortcutError(
            f"The shortcut's path is too long for Windows ({units} characters; "
            f"the most it allows is {_MAX_PATH - 1}): {lnk}"
        )
    lnk.parent.mkdir(parents=True, exist_ok=True)
    _run_powershell(_CREATE_SCRIPT, {
        "SNAG_LNK": str(lnk), "SNAG_LNK_TARGET": str(target), "SNAG_LNK_ARGS": args,
        "SNAG_LNK_WORKDIR": str(workdir), "SNAG_LNK_ICON": str(icon),
    })
    if not lnk.is_file():
        raise ShortcutError(f"Windows reported no error, but the shortcut wasn't saved: {lnk}")


def remove_shortcut(lnk: Path) -> None:
    """Delete the shortcut lnk; nothing happens if it isn't there."""
    _check_lnk(lnk)
    lnk.unlink(missing_ok=True)


def clear_disabled_mark(name: str, key: str = STARTUP_APPROVED_KEY) -> None:
    """Delete Task Manager's enabled or disabled mark for the Startup shortcut called name (see
    STARTUP_APPROVED_KEY; key, under HKEY_CURRENT_USER, is overridable for tests). Nothing
    happens if there is no mark, or no key; other shortcuts' marks are left alone."""
    if sys.platform != "win32":
        return
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as handle:
            winreg.DeleteValue(handle, name)
    except FileNotFoundError:
        pass


def startup_disabled(name: str, key: str = STARTUP_APPROVED_KEY) -> bool:
    """True when Task Manager marks the Startup shortcut called name as disabled: its value's first
    byte is odd (03 when disabled; 02, or 06, when enabled again). No mark, or no key, is False."""
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:
            value, _ = winreg.QueryValueEx(handle, name)
    except FileNotFoundError:
        return False
    return isinstance(value, bytes) and len(value) > 0 and bool(value[0] & 1)


def starts_with_windows(lnk: Path | None = None, *, approved_key: str = STARTUP_APPROVED_KEY) -> bool:
    """Whether Windows will start Snag when the user signs in: the Startup shortcut is there and Task
    Manager hasn't disabled it. Cheap (a file check and a registry read), so it is read afresh
    whenever Settings are shown. lnk and approved_key override where they live, for tests."""
    lnk = lnk or startup_shortcut_path()
    return lnk.is_file() and not startup_disabled(lnk.name, approved_key)


def set_start_with_windows(enabled: bool, lnk: Path | None = None, *, approved_key: str = STARTUP_APPROVED_KEY) -> None:
    """Add or remove the Startup shortcut (the start_with_windows setting), clearing any mark Task
    Manager keeps for it either way, so a shortcut the user once disabled there starts again
    when they turn the setting back on. lnk and approved_key override where the shortcut and the
    marks live, for tests."""
    lnk = lnk or startup_shortcut_path()
    if enabled:
        shortcut = snag_shortcut(open_tab=False)
        create_shortcut(lnk, shortcut.target, shortcut.args, shortcut.workdir, shortcut.icon)
    else:
        remove_shortcut(lnk)
    clear_disabled_mark(lnk.name, approved_key)


# ----- the command line (setup.ps1) -----

def create_shortcuts() -> list[Path]:
    """The Start menu shortcut (opens the app tab) and the Startup one (Snag starts with Windows,
    clearing a mark Task Manager kept for it). Returns where they are."""
    menu = start_menu_shortcut_path()
    shortcut = snag_shortcut(open_tab=True)
    create_shortcut(menu, shortcut.target, shortcut.args, shortcut.workdir, shortcut.icon)
    at_startup = startup_shortcut_path()
    set_start_with_windows(True, at_startup, approved_key=STARTUP_APPROVED_KEY)
    return [menu, at_startup]


def refresh_shortcuts() -> list[Path]:
    """setup.ps1 run again: the Start menu shortcut made again (the Snag folder may have moved),
    and the Startup one too only if it is there, leaving any mark Task Manager keeps for it, so
    Start with Windows stays as the user left it. Returns the shortcuts made."""
    menu = start_menu_shortcut_path()
    shortcut = snag_shortcut(open_tab=True)
    create_shortcut(menu, shortcut.target, shortcut.args, shortcut.workdir, shortcut.icon)
    made = [menu]
    at_startup = startup_shortcut_path()
    if at_startup.is_file():
        shortcut = snag_shortcut(open_tab=False)
        create_shortcut(at_startup, shortcut.target, shortcut.args, shortcut.workdir, shortcut.icon)
        made.append(at_startup)
    return made


def shortcut_status() -> dict[str, dict[str, object]]:
    """Where the Start menu and Startup shortcuts are, and whether each is there (setup.ps1: no
    Start menu shortcut means the first install)."""
    return {
        name: {"path": str(lnk), "exists": lnk.is_file()}
        for name, lnk in (("start_menu", start_menu_shortcut_path()), ("startup", startup_shortcut_path()))
    }


def remove_shortcuts() -> list[Path]:
    menu = start_menu_shortcut_path()
    remove_shortcut(menu)
    at_startup = startup_shortcut_path()
    set_start_with_windows(False, at_startup, approved_key=STARTUP_APPROVED_KEY)
    return [menu, at_startup]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m snag.platform.startup",
                                     description="Check, make or remove Snag's Start menu and Startup shortcuts.")
    parser.add_argument("action", choices=["status", "create", "refresh", "remove"],
                        help="status: where both shortcuts are and whether each exists, as JSON; "
                             "create: both shortcuts (again, if they exist); refresh: the Start menu one, "
                             "and the Startup one only if it exists; remove: both gone")
    action = parser.parse_args(argv).action
    for stream in (sys.stdout, sys.stderr):  # a folder name the console's code page can't show
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    if action == "status":
        print(json.dumps(shortcut_status()))  # ensure_ascii: escaped, so no code page garbles a path
        return 0
    try:
        if action in ("create", "refresh"):
            for lnk in create_shortcuts() if action == "create" else refresh_shortcuts():
                print(f"Made {lnk}")
        else:
            for lnk in remove_shortcuts():
                print(f"Removed {lnk}")
    except OSError as error:
        verb = "remove" if action == "remove" else "make"
        print(f"Couldn't {verb} Snag's shortcuts: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
