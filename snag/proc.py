"""Subprocess helpers. Children run without a console window, in their own process group,
with UTF-8 output so titles with non-English characters survive.

On Windows every child is also put in two Job Objects. Snag's own job dies with Snag, so a
crashed Snag never leaves yt-dlp or ffmpeg running. The child's own job, nested inside it,
holds the child and everything it starts, even processes whose parent has already exited:
kill_tree ends them all through it, and whatever the child leaves running when it exits is
ended as soon as the exit is noticed.

The child is put in the jobs just after it starts (Python can't start it suspended), so a
process it starts in that first instant is in neither job and isn't ended with it. yt-dlp
starts nothing that early: it runs ffmpeg only once a download is under way."""
from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
import threading
from asyncio.subprocess import PIPE, Process
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

_CREATION_FLAGS = (
    subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
)


@dataclass(frozen=True)
class Completed:
    returncode: int
    stdout: str
    stderr: str


def child_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _BasicLimitInformation(ctypes.Structure):  # JOBOBJECT_BASIC_LIMIT_INFORMATION
        _fields_ = (
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),  # ULONG_PTR
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        )

    class _IoCounters(ctypes.Structure):  # IO_COUNTERS
        _fields_ = (
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        )

    class _ExtendedLimitInformation(ctypes.Structure):  # JOBOBJECT_EXTENDED_LIMIT_INFORMATION
        _fields_ = (
            ("BasicLimitInformation", _BasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        )

    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _PROCESS_TERMINATE = 0x0001
    _PROCESS_SET_QUOTA = 0x0100
    _SYNCHRONIZE = 0x00100000
    _INFINITE = 0xFFFFFFFF
    _WAIT_OBJECT_0 = 0x00000000
    _WAIT_FAILED = 0xFFFFFFFF

    # A private WinDLL so these prototypes don't leak into ctypes.windll.kernel32 for other code.
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _CreateJobObjectW = _kernel32.CreateJobObjectW
    _CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
    _CreateJobObjectW.restype = wintypes.HANDLE

    _SetInformationJobObject = _kernel32.SetInformationJobObject
    _SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
    _SetInformationJobObject.restype = wintypes.BOOL

    _AssignProcessToJobObject = _kernel32.AssignProcessToJobObject
    _AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    _AssignProcessToJobObject.restype = wintypes.BOOL

    _OpenProcess = _kernel32.OpenProcess
    _OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _OpenProcess.restype = wintypes.HANDLE

    _TerminateJobObject = _kernel32.TerminateJobObject
    _TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _TerminateJobObject.restype = wintypes.BOOL

    _WaitForSingleObject = _kernel32.WaitForSingleObject
    _WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    _WaitForSingleObject.restype = wintypes.DWORD

    _CloseHandle = _kernel32.CloseHandle
    _CloseHandle.argtypes = (wintypes.HANDLE,)
    _CloseHandle.restype = wintypes.BOOL

_job_lock = threading.Lock()
_job_ready = False
_job: int | None = None


def _kill_on_close_job() -> int | None:
    """The Job Object this Snag process puts its children in, created on first use. Its only
    handle lives until Snag exits, however it exits, and Windows then kills every process in
    the job. None (and no retry) if Windows refused to create or configure it."""
    global _job, _job_ready
    with _job_lock:
        if not _job_ready:
            _job = _create_kill_on_close_job()
            _job_ready = True
        return _job


def _create_kill_on_close_job() -> int | None:
    job = _CreateJobObjectW(None, None)
    if not job:
        return None
    info = _ExtendedLimitInformation()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not _SetInformationJobObject(
        job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS, ctypes.byref(info), ctypes.sizeof(info)
    ):
        _CloseHandle(job)
        return None
    return job


class _ChildJob:
    """The kill-on-close Job Object holding one spawned child and everything it starts. Used from
    the event loop (kill_tree) and from the thread that closes it when the child exits."""

    def __init__(self, handle: int):
        self._lock = threading.Lock()
        self.handle: int | None = handle

    def terminate(self) -> bool:
        """End every process in the job; False if the job is already closed or Windows refused."""
        with self._lock:
            return self.handle is not None and bool(_TerminateJobObject(self.handle, 1))

    def close(self) -> None:
        """Release the job. Being kill-on-close, this also ends anything still running in it."""
        with self._lock:
            if self.handle is not None:
                _CloseHandle(self.handle)
                self.handle = None


_JOB_ATTRIBUTE = "_snag_child_job"


def _child_job_of(proc: Process) -> _ChildJob | None:
    return getattr(proc, _JOB_ATTRIBUTE, None)


def _open_child_job(process: int) -> _ChildJob | None:
    """Put the process (an open handle) in a new kill-on-close job. The process is already in
    Snag's job, so on Windows 8 and later the new job is nested inside that one. None if Windows
    refused."""
    job = _create_kill_on_close_job()
    if job is None:
        return None
    if not _AssignProcessToJobObject(job, process):
        _CloseHandle(job)
        return None
    return _ChildJob(job)


def _close_job_when_exited(process: int, job: _ChildJob) -> None:
    """Runs in its own thread: waits for the child to exit, then closes its job, ending whatever
    the child left running (an ffmpeg holding the child's output pipes would otherwise keep
    them open).

    The job is closed only once the wait says the child has exited: closing it ends the child
    too. If the wait fails the job stays open for kill_tree, or until Snag exits."""
    try:
        exited = _WaitForSingleObject(process, _INFINITE) == _WAIT_OBJECT_0
    finally:
        _CloseHandle(process)
    if exited:
        job.close()


def _tie(pid: int) -> _ChildJob | None:
    """Put a freshly started child in Snag's kill-on-close job and in a job of its own; whatever
    it starts later inherits both memberships. Best effort: if Windows refuses a step the child
    runs without that job. Returns the child's own job."""
    if sys.platform != "win32":
        return None
    process = _OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE | _SYNCHRONIZE, False, pid)
    if not process:
        return None
    watched = False
    job: _ChildJob | None = None
    try:
        snag_job = _kill_on_close_job()
        if snag_job is not None:
            _AssignProcessToJobObject(snag_job, process)
        job = _open_child_job(process)
        if job is not None:
            watcher = threading.Thread(
                target=_close_job_when_exited, args=(process, job), name=f"snag-child-{pid}", daemon=True,
            )
            # Without the watcher the job just stays open until Snag exits; kill_tree still uses it.
            with contextlib.suppress(RuntimeError):
                watcher.start()
                watched = True
        return job
    except BaseException:
        if job is not None:
            job.close()  # nobody would ever close it; being kill-on-close, this also ends the child
        raise
    finally:
        if not watched:
            _CloseHandle(process)


async def spawn(args: list[str], cwd: Path | None = None, *, merge_stderr: bool = False) -> Process:
    """Start args with its output piped; cwd is the child's working folder (default: ours).

    merge_stderr sends the child's stderr into its stdout pipe, so the two keep their true order
    (proc.stderr is then None). Once the child exits, anything it left running is ended.

    The child and everything it starts die with Snag (see the module docstring), so nothing
    that must outlive Snag, such as relaunching it after an update or running a setup program,
    may be started with spawn.

    If the child starts but can't be put in its jobs because of an error (not a refusal by
    Windows, which spawn tolerates), it is killed before the error propagates, so it never runs
    untracked."""
    proc = await asyncio.create_subprocess_exec(
        *args, stdin=subprocess.DEVNULL, stdout=PIPE, stderr=subprocess.STDOUT if merge_stderr else PIPE,
        env=child_env(), creationflags=_CREATION_FLAGS, cwd=cwd,
    )
    try:
        job = _tie(proc.pid)
    except BaseException:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        # Reap it so asyncio doesn't warn about a process still running; the kill is already done.
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), 10)
        raise
    setattr(proc, _JOB_ATTRIBUTE, job)
    return proc


async def run_capture(args: list[str], timeout: float | None = None) -> Completed:
    """Run to completion and collect both streams. On timeout (TimeoutError), cancellation or
    any other failure the whole process tree is killed before the exception propagates.

    The kill always runs to the end, even if the task is cancelled again meanwhile. The
    original exception is what propagates; if the kill itself fails, that is recorded as a
    note on it. One exception: a cancellation that arrives during the kill of a non-cancel
    failure (say a timeout) propagates as CancelledError, so the cancel is never lost."""
    proc = await spawn(args)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except BaseException as original:
        cleanup = asyncio.ensure_future(kill_tree(proc))
        cancelled_meanwhile = False
        while not cleanup.done():
            try:
                await asyncio.wait((cleanup,))  # never cancels cleanup, unlike awaiting it
            except asyncio.CancelledError:
                cancelled_meanwhile = True
        if not cleanup.cancelled() and (failure := cleanup.exception()) is not None:
            original.add_note(f"Killing the process tree also failed: {failure!r}")
        if cancelled_meanwhile and not isinstance(original, asyncio.CancelledError):
            raise asyncio.CancelledError()  # the original stays attached as __context__
        raise
    return Completed(proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"))


async def pump_lines(stream: asyncio.StreamReader, on_line: Callable[[str], None]) -> None:
    """Feed each line to on_line until EOF. If on_line raises, the rest of the stream is still
    read and discarded (so the child never blocks on a full pipe), on_line is not called again,
    and the exception is re-raised at EOF.

    Lines longer than 64 KiB raise ValueError, so yt-dlp must be run with --newline to keep
    its progress output on separate lines."""
    error: Exception | None = None
    while raw := await stream.readline():
        if error is not None:
            continue
        try:
            on_line(raw.decode("utf-8", "replace").rstrip("\r\n"))
        except Exception as exc:
            error = exc
    if error is not None:
        raise error


_TASKKILL_TIMEOUT = 10.0  # seconds to let taskkill work before giving up on it


def _taskkill_exe() -> Path:
    return Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"


async def kill_tree(proc: Process) -> None:
    """Stop a process and everything it started (yt-dlp runs ffmpeg as a child).

    On Windows the child's own job is terminated, which ends every process in it, including
    ones that outlived their parent (or the child itself). Without that job, taskkill /T takes
    down the tree; if taskkill can't be started or leaves the direct child running, the child is
    killed directly so it never outlives the call. Raises TimeoutError if the child still hasn't
    exited 10 seconds later.

    Through the child's job this takes milliseconds, and at worst the 10 seconds above. Without
    the job, taskkill may take up to 10 seconds and 5 more to reap if it hangs, so the call can
    take about 25 seconds before raising TimeoutError."""
    job = _child_job_of(proc)
    ended = job is not None and job.terminate()
    if proc.returncode is not None:
        return
    if not ended:
        await _kill_without_job(proc)
    await asyncio.wait_for(proc.wait(), 10)


async def _kill_without_job(proc: Process) -> None:
    if sys.platform != "win32":
        proc.kill()
        return
    with contextlib.suppress(OSError):
        killer = await asyncio.create_subprocess_exec(
            str(_taskkill_exe()), "/T", "/F", "/PID", str(proc.pid),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=_CREATION_FLAGS,
        )
        try:
            await asyncio.wait_for(killer.wait(), _TASKKILL_TIMEOUT)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):  # it may have just finished
                killer.kill()
            # Wait for it so asyncio doesn't warn "subprocess still running" later.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(killer.wait(), 5)
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
