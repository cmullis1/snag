"""One Snag per Windows session, enforced with a named mutex.

The mutex is only ever checked for existence (nobody waits on it), so it is created unowned. Its
handle is kept until the process exits, however it exits; Windows then deletes the mutex once no
process holds a handle to it, so a crashed Snag never blocks the next start."""
from __future__ import annotations

import ctypes
import sys
import threading

DEFAULT_MUTEX_NAME = "Local\\Snag-47823"  # Local\: per logged-in session; 47823 is Snag's port

_ERROR_ALREADY_EXISTS = 183
_ERROR_ACCESS_DENIED = 5  # the mutex exists but was made by a process we can't open (another user)

if sys.platform == "win32":
    from ctypes import wintypes

    # A private WinDLL so these prototypes don't leak into ctypes.windll.kernel32 for other code.
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _CreateMutexW = _kernel32.CreateMutexW
    _CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
    _CreateMutexW.restype = wintypes.HANDLE

    _CloseHandle = _kernel32.CloseHandle
    _CloseHandle.argtypes = (wintypes.HANDLE,)
    _CloseHandle.restype = wintypes.BOOL

_lock = threading.Lock()
_held: dict[str, int] = {}  # mutex name -> handle, for the life of the process


def acquire_single_instance(name: str = DEFAULT_MUTEX_NAME) -> bool:
    """True if this process is now the one Snag running (or already was); False if another
    process holds the mutex. Raises OSError if Windows can't create the mutex for another reason.

    Outside Windows there is nothing to enforce it with, and this always returns True."""
    if sys.platform != "win32":
        return True
    with _lock:
        if name in _held:
            return True
        handle = _CreateMutexW(None, False, name)
        error = ctypes.get_last_error()
        if not handle:
            if error == _ERROR_ACCESS_DENIED:
                return False
            raise ctypes.WinError(error)
        if error == _ERROR_ALREADY_EXISTS:
            _CloseHandle(handle)  # holding it would keep the other instance's mutex alive after it exits
            return False
        _held[name] = handle
        return True
