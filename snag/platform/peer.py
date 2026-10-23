"""Which Windows user owns the other end of a TCP connection to Snag (spec 3.4).

Fast user switching keeps several users signed in at once, and they all reach the same
127.0.0.1:<port>: without this, another user's browser and extension could use this user's Snag
(handing it that user's YouTube cookies, or reading this user's queue and history). So the server
asks, for each caller, whether its process runs as the same user as Snag (snag.server.guard).

The caller's process is found in Windows' TCP table (GetExtendedTcpTable, TCP_TABLE_OWNER_PID_ALL):
the row whose local end is the caller's address and whose remote end is Snag's. Its access token's
user SID is then compared with Snag's own (OpenProcessToken, GetTokenInformation(TokenUser),
EqualSid). Snag listens on 127.0.0.1 only (snag/__main__.py), so only the IPv4 table is read.

Anything that can't be told (no such connection, a process that can't be opened, as other users'
and system processes can't) raises OSError: the guard refuses the caller then."""
from __future__ import annotations

import ctypes
import os
import socket
import struct
import sys
from ctypes import wintypes

if sys.platform == "win32":
    # Private WinDLLs so these prototypes don't leak into ctypes.windll for other code.
    _iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

    _GetExtendedTcpTable = _iphlpapi.GetExtendedTcpTable
    _GetExtendedTcpTable.argtypes = (ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
                                     wintypes.ULONG, ctypes.c_int, wintypes.ULONG)
    _GetExtendedTcpTable.restype = wintypes.DWORD

    _OpenProcess = _kernel32.OpenProcess
    _OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _OpenProcess.restype = wintypes.HANDLE

    _GetCurrentProcess = _kernel32.GetCurrentProcess
    _GetCurrentProcess.argtypes = ()
    _GetCurrentProcess.restype = wintypes.HANDLE

    _CloseHandle = _kernel32.CloseHandle
    _CloseHandle.argtypes = (wintypes.HANDLE,)
    _CloseHandle.restype = wintypes.BOOL

    _OpenProcessToken = _advapi32.OpenProcessToken
    _OpenProcessToken.argtypes = (wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE))
    _OpenProcessToken.restype = wintypes.BOOL

    _GetTokenInformation = _advapi32.GetTokenInformation
    _GetTokenInformation.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                     ctypes.POINTER(wintypes.DWORD))
    _GetTokenInformation.restype = wintypes.BOOL

    _IsValidSid = _advapi32.IsValidSid
    _IsValidSid.argtypes = (ctypes.c_void_p,)
    _IsValidSid.restype = wintypes.BOOL

    _GetLengthSid = _advapi32.GetLengthSid
    _GetLengthSid.argtypes = (ctypes.c_void_p,)
    _GetLengthSid.restype = wintypes.DWORD

    _EqualSid = _advapi32.EqualSid
    _EqualSid.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    _EqualSid.restype = wintypes.BOOL

_AF_INET = 2
_TCP_TABLE_OWNER_PID_ALL = 5
_NO_ERROR = 0
_ERROR_INSUFFICIENT_BUFFER = 122
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1  # TOKEN_INFORMATION_CLASS.TokenUser


class _TcpRow(ctypes.Structure):
    """MIB_TCPROW_OWNER_PID: addresses and ports in network byte order."""

    _fields_ = [("state", wintypes.DWORD), ("local_addr", wintypes.DWORD), ("local_port", wintypes.DWORD),
                ("remote_addr", wintypes.DWORD), ("remote_port", wintypes.DWORD), ("pid", wintypes.DWORD)]


def _ipv4(address: str) -> int:
    """A dotted IPv4 address as the table stores it; OSError for anything else (IPv6 included)."""
    try:
        return struct.unpack("<I", socket.inet_aton(address))[0]
    except OSError:
        raise OSError(f"Not an IPv4 address: {address!r}") from None


def _port(value: int) -> int:
    return socket.ntohs(value & 0xFFFF)


def _tcp_rows() -> list[_TcpRow]:
    size = wintypes.DWORD(0)
    for _ in range(5):  # the table can grow between the size query and the read
        buffer = ctypes.create_string_buffer(size.value or 4)
        result = _GetExtendedTcpTable(buffer, ctypes.byref(size), False, _AF_INET, _TCP_TABLE_OWNER_PID_ALL, 0)
        if result == _NO_ERROR:
            count = wintypes.DWORD.from_buffer(buffer).value
            rows = (_TcpRow * count).from_buffer(buffer, ctypes.sizeof(wintypes.DWORD))
            return list(rows)
        if result != _ERROR_INSUFFICIENT_BUFFER:
            raise ctypes.WinError(result)
    raise OSError("Windows' TCP table kept changing size")


def owning_pid(local: tuple[str, int], remote: tuple[str, int]) -> int:
    """The process owning the TCP connection whose own end is local and whose other end is remote."""
    local_addr, remote_addr = _ipv4(local[0]), _ipv4(remote[0])
    for row in _tcp_rows():
        if (row.local_addr == local_addr and _port(row.local_port) == local[1]
                and row.remote_addr == remote_addr and _port(row.remote_port) == remote[1]):
            return int(row.pid)
    raise OSError(f"There is no TCP connection from {local[0]}:{local[1]} to {remote[0]}:{remote[1]}")


def _process_user(process: int) -> ctypes.Array:
    """The user SID of the process handle's access token, copied into a buffer of its own."""
    token = wintypes.HANDLE()
    if not _OpenProcessToken(process, _TOKEN_QUERY, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        needed = wintypes.DWORD(0)
        _GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(needed))
        if not needed.value:
            raise ctypes.WinError(ctypes.get_last_error())
        info = ctypes.create_string_buffer(needed.value)
        if not _GetTokenInformation(token, _TOKEN_USER, info, needed, ctypes.byref(needed)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid = ctypes.c_void_p.from_buffer(info).value  # TOKEN_USER starts with SID_AND_ATTRIBUTES.Sid
        if not sid or not _IsValidSid(sid):
            raise OSError("The process's token has no valid user SID")
        return ctypes.create_string_buffer(ctypes.string_at(sid, _GetLengthSid(sid)))
    finally:
        _CloseHandle(token)


def _user_of(pid: int) -> ctypes.Array:
    process = _OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not process:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return _process_user(process)
    finally:
        _CloseHandle(process)


_own_user: ctypes.Array | None = None


def _snags_user() -> ctypes.Array:
    global _own_user
    if _own_user is None:
        _own_user = _process_user(_GetCurrentProcess())  # a pseudo-handle: nothing to close
    return _own_user


def same_user_peer(peer: tuple[str, int], server: tuple[str, int]) -> bool:
    """Whether the process at the other end of the connection from peer to server (Snag's
    listening address) runs as the same Windows user as Snag. Raises OSError when it can't be told."""
    pid = owning_pid(peer, server)
    if pid == os.getpid():
        return True
    theirs, ours = _user_of(pid), _snags_user()  # both kept referenced while EqualSid reads them
    return bool(_EqualSid(ctypes.addressof(theirs), ctypes.addressof(ours)))
