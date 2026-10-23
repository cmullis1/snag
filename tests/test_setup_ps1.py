"""setup.ps1 (spec 7), run with -DryRun from a copy in tmp_path: it checks Python and whether Snag is
running, prints what it would do, and changes nothing. A real run makes shortcuts and starts Snag, so
the tests never do one.

Every run gets its own %LOCALAPPDATA% (in tmp_path) and, through Snag's test hook, its own port
(SNAG_TEST_HOOK=1, SNAG_PORT) and Start menu folder (SNAG_START_MENU, in tmp_path), so a Snag running
on this PC, its data and its shortcuts are never seen."""
import ctypes
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="setup.ps1 is Windows PowerShell")

REPO = Path(__file__).resolve().parents[1]
POWERSHELL = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
PY_VERSION = "{0}.{1}.{2}".format(*sys.version_info)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def repo(tmp_path) -> Path:
    """A folder standing in for the repo, holding a copy of setup.ps1 (it works on its own folder) and
    of the snag package (setup asks its shortcut helper whether the Start menu shortcut is there)."""
    folder = tmp_path / "Snag repo"
    folder.mkdir()
    (folder / "setup.ps1").write_bytes((REPO / "setup.ps1").read_bytes())
    shutil.copytree(REPO / "snag", folder / "snag", ignore=shutil.ignore_patterns("__pycache__"))
    return folder


def local_app_data(repo: Path) -> Path:
    return repo.parent / "LocalAppData"


def start_menu(repo: Path) -> Path:
    """The folder standing in for the user's Start menu Programs folder (its Startup folder inside)."""
    return repo.parent / "Start Menu"


def setup(repo: Path, *args: str, port: int | None = None, interactive: bool = False, stdin: str = "",
          **env: str) -> subprocess.CompletedProcess:
    """Run the copy of setup.ps1 (-NonInteractive unless interactive) with its own LOCALAPPDATA and
    port; env adds variables."""
    flags = [] if interactive else ["-NonInteractive"]
    environ = {**os.environ, "LOCALAPPDATA": str(local_app_data(repo)), "SNAG_TEST_HOOK": "1",
               "SNAG_PORT": str(port or free_port()), "SNAG_START_MENU": str(start_menu(repo))}
    environ.pop("SNAG_SETUP_PARENT", None)
    environ.update(env)
    return subprocess.run(
        [str(POWERSHELL), "-NoLogo", "-NoProfile", *flags, "-ExecutionPolicy", "Bypass",
         "-File", str(repo / "setup.ps1"), *args],
        capture_output=True, text=True, timeout=120, cwd=repo, env=environ, input=stdin,
    )


def lines(done: subprocess.CompletedProcess) -> list[str]:
    return [line.rstrip() for line in done.stdout.splitlines() if line.strip()]


def test_a_dry_run_on_a_fresh_folder_plans_every_step_and_changes_nothing(repo):
    done = setup(repo, "-DryRun", "-Python", sys.executable)
    assert done.returncode == 0, done.stdout + done.stderr
    venv_python = repo / ".venv" / "Scripts" / "python.exe"
    planned = [line for line in lines(done) if line.startswith("[dry run]")]
    assert planned == [
        f'[dry run] Create the virtual environment: & "{sys.executable}" -m venv "{repo / ".venv"}"',
        f'[dry run] Install Snag and its dependencies: & "{venv_python}" -m pip install '
        f'--disable-pip-version-check -e "{repo}"',
        f'[dry run] Make the Start menu and Startup shortcuts: & "{venv_python}" -m snag.platform.startup create',
        f'[dry run] Start Snag: Start-Process "{repo / ".venv" / "Scripts" / "pythonw.exe"}" -m snag --open',
    ]
    assert not local_app_data(repo).exists() and not start_menu(repo).exists()
    out = done.stdout
    assert f"Python {PY_VERSION}" in out
    assert "Load unpacked" in out and str(repo / "extension") in out  # the extension steps, always
    assert "chrome://extensions" in out and "edge://extensions" in out
    assert sorted(p.name for p in repo.iterdir()) == ["setup.ps1", "snag"]  # nothing was made,
    assert not any(repo.rglob("__pycache__"))  # not even the helper's bytecode


def make_venv(repo: Path, version: str) -> None:
    scripts = repo / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "python.exe").write_bytes(b"")
    (repo / ".venv" / "pyvenv.cfg").write_text(f"home = C:\\Python\nversion = {version}\n", encoding="utf-8")


def test_an_existing_virtual_environment_is_reused(repo):
    make_venv(repo, "3.12.10")
    done = setup(repo, "-DryRun", "-Python", sys.executable)
    assert done.returncode == 0, done.stdout + done.stderr
    assert not any("Create the virtual environment" in line for line in lines(done))
    assert f"Using the existing virtual environment {repo / '.venv'}" in done.stdout
    assert any(line.startswith("[dry run] Install Snag") for line in lines(done))


def test_a_virtual_environment_made_with_an_old_python_is_not_reused(repo):
    make_venv(repo, "3.11.4")
    done = setup(repo, "-DryRun", "-Python", sys.executable)
    assert done.returncode == 1
    output = done.stdout + done.stderr
    assert "3.11.4" in output and "Delete the .venv folder" in output
    assert "[dry run]" not in output


def test_python_older_than_3_12_is_refused_plainly(repo, tmp_path):
    old = tmp_path / "old python.bat"
    old.write_text("@echo 3.11.9\n", encoding="ascii")
    done = setup(repo, "-DryRun", "-Python", str(old))
    assert done.returncode == 1
    output = done.stdout + done.stderr
    assert "Snag needs Python 3.12 or newer" in output and "3.11.9" in output
    assert "python.org" in output
    assert "[dry run]" not in output


def test_a_missing_python_is_refused_plainly(repo, tmp_path):
    done = setup(repo, "-DryRun", "-Python", str(tmp_path / "nowhere" / "python.exe"))
    assert done.returncode == 1
    output = done.stdout + done.stderr
    assert "Snag needs Python 3.12 or newer" in output and "python.org" in output
    assert "[dry run]" not in output


def test_the_script_has_no_windows_script_host_and_uses_snags_shortcut_helper():
    text = (REPO / "setup.ps1").read_text(encoding="utf-8")
    assert "WScript.Shell" not in text  # ANSI-only: it breaks non-ANSI folder names
    assert "-m snag.platform.startup" in text  # status, then create or refresh: see the dry runs


# ----- Start with Windows: set up once, then left as the user has it -----

def planned_steps(done: subprocess.CompletedProcess) -> list[str]:
    return [line for line in lines(done) if line.startswith("[dry run]")]


def shortcut_step(repo: Path, action: str) -> str:
    venv_python = repo / ".venv" / "Scripts" / "python.exe"
    what = {"create": "Make the Start menu and Startup shortcuts",
            "refresh": "Make the Start menu shortcut again (and the Startup one, if Start with Windows is on)"}
    return f'[dry run] {what[action]}: & "{venv_python}" -m snag.platform.startup {action}'


def make_database(repo: Path) -> None:
    data = local_app_data(repo) / "Snag"
    data.mkdir(parents=True)
    (data / "snag.db").write_bytes(b"")


def test_the_first_install_makes_both_shortcuts_even_if_snag_has_already_run(repo):
    """Snag makes its database whenever it runs, setup or no setup, so the database says nothing
    about setup: no Start menu shortcut yet (only setup makes it) is the first install."""
    make_venv(repo, "3.12.10")
    make_database(repo)
    done = setup(repo, "-DryRun", "-Python", sys.executable)
    assert done.returncode == 0, done.stdout + done.stderr
    planned = planned_steps(done)
    assert shortcut_step(repo, "create") in planned
    assert not any("startup refresh" in line for line in planned)
    assert not start_menu(repo).exists()  # a dry run makes nothing


def test_run_again_it_leaves_start_with_windows_off_as_the_user_left_it(repo):
    """The Start menu shortcut is there, so setup has run here before: it is made again, and the
    Startup one only if it is there. Turned off in Settings, Start with Windows stays off."""
    make_venv(repo, "3.12.10")
    make_database(repo)
    start_menu(repo).mkdir()
    (start_menu(repo) / "Snag.lnk").write_bytes(b"")
    done = setup(repo, "-DryRun", "-Python", sys.executable)
    assert done.returncode == 0, done.stdout + done.stderr
    planned = planned_steps(done)
    assert shortcut_step(repo, "refresh") in planned
    assert not any("startup create" in line for line in planned)
    assert not (start_menu(repo) / "Startup").exists()


def test_run_again_without_snags_database_it_is_still_a_re_run(repo):
    """The database may be gone (deleted to start afresh) while the shortcuts stay: the Start menu
    shortcut alone decides."""
    start_menu(repo).mkdir()
    (start_menu(repo) / "Snag.lnk").write_bytes(b"")
    done = setup(repo, "-DryRun", "-Python", sys.executable)
    assert done.returncode == 0, done.stdout + done.stderr
    assert shortcut_step(repo, "refresh") in planned_steps(done)


@pytest.mark.parametrize("helper, said", [
    ("import sys; print('no such thing', file=sys.stderr); sys.exit(3)", "(exit code 3)"),
    ("print('not JSON')", "its helper said 'not JSON'"),
])
def test_a_shortcut_helper_that_fails_stops_setup_plainly(repo, helper, said):
    (repo / "snag" / "platform" / "startup.py").write_text(helper + "\n", encoding="ascii")
    done = setup(repo, "-DryRun", "-Python", sys.executable)
    assert done.returncode == 1
    output = done.stdout + done.stderr
    assert "Couldn't check for Snag's Start menu shortcut" in output and said in output
    assert not any("snag.platform.startup" in line for line in planned_steps(done))


# ----- a running Snag -----

class _FakeSnag(BaseHTTPRequestHandler):
    answer = {"version": "0.1.0", "tools": {}}

    def do_GET(self):
        body = json.dumps(self.answer if self.path == "/api/status" else {"kind": "not_found"}).encode()
        self.send_response(200 if self.path == "/api/status" else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def responder():
    """Something answering on a port of its own: Snag's /api/status by default."""
    servers = []

    def start(handler=_FakeSnag) -> int:
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return server.server_address[1]

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


RUNNING = "Snag is running: quit it from the tray icon, then run setup.ps1 again"


def test_a_running_snag_stops_setup_before_anything_is_installed(repo, responder):
    """pip can't replace the files a running Snag has open, and would leave .venv half updated."""
    done = setup(repo, "-DryRun", "-Python", sys.executable, port=responder())
    assert done.returncode == 1
    output = done.stdout + done.stderr
    assert RUNNING in output
    assert "[dry run]" not in output


def test_another_program_on_the_port_is_not_taken_for_snag(repo, responder):
    class NotSnag(_FakeSnag):
        answer = {"hello": "world"}

    done = setup(repo, "-DryRun", "-Python", sys.executable, port=responder(NotSnag))
    assert done.returncode == 0, done.stdout + done.stderr
    assert RUNNING not in done.stdout + done.stderr


@pytest.fixture
def held_mutex():
    """Snag's single-instance mutex for a port, held as a running Snag holds it (while it starts,
    before it answers on its port)."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    handles = []

    def hold(port: int) -> None:
        handle = kernel32.CreateMutexW(None, False, f"Local\\Snag-{port}")
        assert handle
        handles.append(handle)

    yield hold
    for handle in handles:
        kernel32.CloseHandle(handle)


def test_a_starting_snag_is_found_by_its_single_instance_mutex(repo, held_mutex):
    port = free_port()
    held_mutex(port)
    done = setup(repo, "-DryRun", "-Python", sys.executable, port=port)
    assert done.returncode == 1
    assert RUNNING in done.stdout + done.stderr


# ----- Pythons that can't run Snag -----

def test_the_microsoft_store_python_is_refused_plainly(repo, tmp_path):
    """The Store's Python keeps what it writes to %LOCALAPPDATA% in a private folder of its own, so
    Snag's data, and what the tray and the shortcuts start, wouldn't be where Snag looks."""
    store = tmp_path / "store python.bat"
    base = r"C:\Users\me\AppData\Local\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.12_qbz5n2kfra8p0\python.exe"
    store.write_text(f"@echo 3.12.10\n@echo {base}\n", encoding="ascii")
    done = setup(repo, "-DryRun", "-Python", str(store))
    assert done.returncode == 1
    output = done.stdout + done.stderr
    assert "Microsoft Store" in output and "python.org" in output
    assert "[dry run]" not in output


def test_the_store_stub_that_offers_to_install_python_counts_as_no_python(repo, tmp_path):
    """Without Python, Windows' python.exe is a stub that prints where to get it, to stderr, and
    exits 9009."""
    stub = tmp_path / "stub python.bat"
    stub.write_text("@echo Python was not found; run without arguments to install from the Microsoft Store. 1>&2\n"
                    "@exit /b 9009\n", encoding="ascii")
    done = setup(repo, "-DryRun", "-Python", str(stub))
    assert done.returncode == 1
    output = done.stdout + done.stderr
    assert "Snag needs Python 3.12 or newer" in output and "python.org" in output
    assert "9009" not in output and "[dry run]" not in output


def test_a_virtual_environment_made_with_the_store_python_is_not_reused(repo):
    make_venv(repo, "3.12.10")
    cfg = repo / ".venv" / "pyvenv.cfg"
    cfg.write_text("home = C:\\Users\\me\\AppData\\Local\\Microsoft\\WindowsApps\\"
                   "PythonSoftwareFoundation.Python.3.12_qbz5n2kfra8p0\nversion = 3.12.10\n", encoding="utf-8")
    done = setup(repo, "-DryRun", "-Python", sys.executable)
    assert done.returncode == 1
    output = done.stdout + done.stderr
    assert "Microsoft Store" in output and "Delete the .venv folder" in output


# ----- its own window -----

PAUSE = "Press Enter to close this window."


def test_started_from_explorer_it_waits_before_its_window_closes(repo, tmp_path):
    """Run with PowerShell (Explorer's menu) opens a window that closes when the script ends, so the
    outcome would vanish unread. SNAG_SETUP_PARENT stands in for the process that started it."""
    done = setup(repo, "-DryRun", "-Python", sys.executable, interactive=True, stdin="\n",
                 SNAG_SETUP_PARENT="explorer")
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout.rstrip().endswith(PAUSE)
    failed = setup(repo, "-DryRun", "-Python", str(tmp_path / "nowhere.exe"), interactive=True, stdin="\n",
                   SNAG_SETUP_PARENT="explorer")
    assert failed.returncode == 1 and failed.stdout.rstrip().endswith(PAUSE)


def test_started_from_a_console_it_does_not_wait(repo):
    for parent in ("powershell", "cmd"):
        done = setup(repo, "-DryRun", "-Python", sys.executable, interactive=True, SNAG_SETUP_PARENT=parent)
        assert done.returncode == 0 and PAUSE not in done.stdout
    done = setup(repo, "-DryRun", "-Python", sys.executable, SNAG_SETUP_PARENT="explorer")  # -NonInteractive
    assert done.returncode == 0 and PAUSE not in done.stdout


def test_the_script_is_ascii():
    """Windows PowerShell 5.1 reads a .ps1 without a byte order mark in the PC's ANSI code page."""
    assert (REPO / "setup.ps1").read_bytes().isascii()
