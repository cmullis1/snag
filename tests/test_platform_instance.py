import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from snag.platform import instance
from snag.platform.instance import DEFAULT_MUTEX_NAME, acquire_single_instance

REPO_ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="named mutexes are a Windows feature")

# Takes the mutex named in argv[1], says whether it got it, then holds it until stdin closes.
HOLDER_SCRIPT = (
    "import sys\n"
    "from snag.platform.instance import acquire_single_instance\n"
    "print('held' if acquire_single_instance(sys.argv[1]) else 'busy', flush=True)\n"
    "sys.stdin.read()\n"
)


# Like HOLDER_SCRIPT, but the mutex is made with an empty DACL (SDDL "D:"), which grants no one
# any access: what another user's Snag looks like from this session.
LOCKED_HOLDER_SCRIPT = (
    "import ctypes, sys\n"
    "from ctypes import wintypes\n"
    "class SA(ctypes.Structure):\n"
    "    _fields_ = [('nLength', wintypes.DWORD), ('lpSecurityDescriptor', wintypes.LPVOID),"
    " ('bInheritHandle', wintypes.BOOL)]\n"
    "advapi32 = ctypes.WinDLL('advapi32', use_last_error=True)\n"
    "kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)\n"
    "convert = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW\n"
    "convert.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID), wintypes.LPVOID)\n"
    "kernel32.CreateMutexW.argtypes = (ctypes.POINTER(SA), wintypes.BOOL, wintypes.LPCWSTR)\n"
    "kernel32.CreateMutexW.restype = wintypes.HANDLE\n"
    "descriptor = wintypes.LPVOID()\n"
    "assert convert('D:', 1, ctypes.byref(descriptor), None), ctypes.WinError(ctypes.get_last_error())\n"
    "attributes = SA(ctypes.sizeof(SA), descriptor, False)\n"
    "handle = kernel32.CreateMutexW(ctypes.byref(attributes), False, sys.argv[1])\n"
    "print('held' if handle and ctypes.get_last_error() != 183 else 'failed', flush=True)\n"
    "sys.stdin.read()\n"
)


def _unique_name() -> str:
    # Never the real Snag name: a running Snag must not be disturbed by the tests, nor they by it.
    return f"Local\\Snag-test-{uuid.uuid4().hex}"


def _start_holder(name: str, script: str = HOLDER_SCRIPT) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", script, name], cwd=REPO_ROOT,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )


def _stop(holder: subprocess.Popen[str]) -> None:
    try:
        holder.stdin.close()
        holder.wait(timeout=10)
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=10)
        holder.stdout.close()


def test_default_name_is_per_session_and_names_the_port():
    assert DEFAULT_MUTEX_NAME == "Local\\Snag-47823"


def test_another_process_holding_the_mutex_means_another_instance():
    name = _unique_name()
    holder = _start_holder(name)
    try:
        assert holder.stdout.readline().strip() == "held"
        assert acquire_single_instance(name) is False
    finally:
        _stop(holder)


def test_the_mutex_is_free_again_once_its_holder_exits():
    name = _unique_name()
    holder = _start_holder(name)
    try:
        assert holder.stdout.readline().strip() == "held"
    finally:
        _stop(holder)
    assert acquire_single_instance(name) is True


def test_once_acquired_other_processes_see_it_taken():
    name = _unique_name()
    assert acquire_single_instance(name) is True
    holder = _start_holder(name)
    try:
        assert holder.stdout.readline().strip() == "busy"
    finally:
        _stop(holder)


def test_acquiring_again_in_the_same_process_still_succeeds():
    name = _unique_name()
    assert acquire_single_instance(name) is True
    handle = instance._held[name]
    assert acquire_single_instance(name) is True
    assert instance._held[name] == handle  # the same handle, kept for the process's life


def test_a_mutex_this_process_may_not_open_means_another_instance():
    """Another user's Snag (say, through Run as) holds a mutex this session can't open: Windows
    answers ERROR_ACCESS_DENIED rather than handing over a handle, and that is still another
    instance, not an error."""
    name = _unique_name()
    holder = _start_holder(name, LOCKED_HOLDER_SCRIPT)
    try:
        assert holder.stdout.readline().strip() == "held"
        assert acquire_single_instance(name) is False
        assert name not in instance._held
    finally:
        _stop(holder)
