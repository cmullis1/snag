import asyncio
import ctypes
import os
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

import pytest

from snag import proc as proc_module
from snag.proc import kill_tree, pump_lines, run_capture, spawn

PY = sys.executable
REPO_ROOT = Path(__file__).resolve().parent.parent

# Imports snag.proc, spawns a long sleeper through spawn(), prints the sleeper's pid, then idles
# so the test can hard-kill this helper and watch whether the sleeper goes with it.
ORPHAN_SCRIPT = (
    "import asyncio, sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from snag.proc import spawn\n"
    "async def main():\n"
    "    child = await spawn([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
    "    print(child.pid, flush=True)\n"
    "    await asyncio.sleep(120)\n"
    "asyncio.run(main())\n"
)

# Starts a grandchild, then records "<own pid> <grandchild pid>" atomically and sleeps.
# run_capture owns stdout, so the pids travel through a file instead.
TREE_SCRIPT = (
    "import os, subprocess, sys, time\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
    "tmp = sys.argv[1] + '.tmp'\n"
    "with open(tmp, 'w') as f:\n"
    "    f.write(f'{os.getpid()} {child.pid}')\n"
    "os.replace(tmp, sys.argv[1])\n"
    "time.sleep(60)\n"
)


def _alive(pid: int) -> bool:
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
    return str(pid) in out


def _force_kill(pid: int) -> None:
    subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)


async def _wait_for_pids(pid_file: Path, task: asyncio.Task) -> list[int]:
    deadline = time.monotonic() + 20
    while not pid_file.exists():
        assert not task.done(), "run_capture returned before the child wrote its pids"
        assert time.monotonic() < deadline, "child never wrote its pids"
        await asyncio.sleep(0.05)
    return [int(p) for p in pid_file.read_text().split()]


async def _wait_dead(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while _alive(pid):
        if time.monotonic() > deadline:
            return False
        await asyncio.sleep(0.1)
    return True


async def test_run_capture_collects_both_streams():
    done = await run_capture([PY, "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"])
    assert done.returncode == 3
    assert done.stdout.strip() == "out"
    assert done.stderr.strip() == "err"


async def test_run_capture_decodes_utf8():
    done = await run_capture([PY, "-c", "print('Café ✓')"])
    assert done.stdout.strip() == "Café ✓"


async def test_run_capture_cancel_kills_the_tree(tmp_path):
    pid_file = tmp_path / "pids"
    task = asyncio.create_task(run_capture([PY, "-c", TREE_SCRIPT, str(pid_file)]))
    pids: list[int] = []
    try:
        pids = await _wait_for_pids(pid_file, task)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        for pid in pids:
            assert await _wait_dead(pid), f"pid {pid} survived cancellation"
    finally:
        for pid in pids:
            _force_kill(pid)


async def test_run_capture_timeout_kills_the_tree(tmp_path):
    pid_file = tmp_path / "pids"
    task = asyncio.create_task(run_capture([PY, "-c", TREE_SCRIPT, str(pid_file)], timeout=2))
    pids: list[int] = []
    try:
        pids = await _wait_for_pids(pid_file, task)
        with pytest.raises(TimeoutError):
            await task
        for pid in pids:
            assert await _wait_dead(pid), f"pid {pid} survived the timeout"
    finally:
        for pid in pids:
            _force_kill(pid)


async def test_pump_lines_reads_each_line():
    proc = await spawn([PY, "-c", "print('a'); print('b')"])
    lines: list[str] = []
    await pump_lines(proc.stdout, lines.append)
    await proc.wait()
    assert lines == ["a", "b"]


async def test_spawn_runs_the_child_in_the_given_folder(tmp_path):
    folder = tmp_path / "100% %SNAGTESTVAR% ünï"
    folder.mkdir()
    proc = await spawn([PY, "-c", "import os; print(os.getcwd())"], cwd=folder)
    out, _ = await asyncio.wait_for(proc.communicate(), 20)
    assert Path(out.decode("utf-8").strip()) == folder


async def test_spawn_defaults_to_the_current_folder():
    proc = await spawn([PY, "-c", "import os; print(os.getcwd())"])
    out, _ = await asyncio.wait_for(proc.communicate(), 20)
    assert Path(out.decode("utf-8").strip()) == Path.cwd()


async def test_spawn_gives_the_child_no_stdin(monkeypatch):
    seen: dict = {}
    real_exec = asyncio.create_subprocess_exec

    async def recording_exec(*args, **kwargs):
        seen.update(kwargs)
        return await real_exec(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", recording_exec)
    proc = await spawn([PY, "-c", "import sys; print(repr(sys.stdin.read()))"])
    out, _ = await asyncio.wait_for(proc.communicate(), 20)
    assert seen["stdin"] == subprocess.DEVNULL
    assert out.decode().strip() == "''"


async def test_pump_lines_drains_to_eof_when_the_callback_fails():
    # Far more than the pipe and StreamReader buffers hold, so an undrained pipe would block the child.
    proc = await spawn([PY, "-c", "for i in range(20000): print('x' * 20, i)"])
    calls: list[str] = []

    def explode(line: str) -> None:
        calls.append(line)
        raise ValueError("boom")

    try:
        with pytest.raises(ValueError, match="boom"):
            await pump_lines(proc.stdout, explode)
        assert proc.stdout.at_eof()
        assert await asyncio.wait_for(proc.wait(), 20) == 0
        assert len(calls) == 1
    finally:
        _force_kill(proc.pid)


async def test_kill_tree_kills_children():
    script = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "print(child.pid, flush=True)\n"
        "time.sleep(60)\n"
    )
    proc = await spawn([PY, "-c", script])
    child_pid: int | None = None
    try:
        child_pid = int((await proc.stdout.readline()).decode())
        assert _alive(child_pid)
        await kill_tree(proc)
        assert proc.returncode is not None
        assert await _wait_dead(child_pid)
    finally:
        _force_kill(proc.pid)
        if child_pid is not None:
            _force_kill(child_pid)


async def test_kill_tree_on_finished_process_is_harmless():
    proc = await spawn([PY, "-c", "import sys; sys.exit(7)"])
    await proc.wait()
    await kill_tree(proc)
    assert proc.returncode == 7


async def test_kill_tree_runs_taskkill_by_absolute_path(no_child_job, monkeypatch):
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    proc = await spawn([PY, "-c", "import time; time.sleep(60)"])
    launched: list[str] = []
    real_exec = asyncio.create_subprocess_exec

    async def recording_exec(program, *args, **kwargs):
        launched.append(program)
        return await real_exec(program, *args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", recording_exec)
    try:
        await kill_tree(proc)
    finally:
        _force_kill(proc.pid)
    assert launched == [str(system_root / "System32" / "taskkill.exe")]
    assert proc.returncode is not None


async def test_kill_tree_falls_back_when_taskkill_is_missing(no_child_job, monkeypatch, tmp_path):
    proc = await spawn([PY, "-c", "import time; time.sleep(600)"])
    monkeypatch.setenv("SystemRoot", str(tmp_path))  # no System32\taskkill.exe under here
    try:
        await asyncio.wait_for(kill_tree(proc), 20)
        assert proc.returncode is not None
    finally:
        _force_kill(proc.pid)


def test_job_limit_structures_have_the_windows_x64_layout():
    assert ctypes.sizeof(ctypes.c_void_p) == 8, "Snag targets 64-bit Python"
    assert ctypes.sizeof(proc_module._BasicLimitInformation) == 64
    assert ctypes.sizeof(proc_module._IoCounters) == 48
    assert ctypes.sizeof(proc_module._ExtendedLimitInformation) == 144


async def test_spawn_puts_the_child_in_the_kill_on_close_job():
    job = proc_module._kill_on_close_job()
    assert job is not None
    child = await spawn([PY, "-c", "import time; time.sleep(60)"])
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.IsProcessInJob.argtypes = (wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL))
    kernel32.IsProcessInJob.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x1000, False, child.pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    try:
        assert handle
        in_job = wintypes.BOOL()
        assert kernel32.IsProcessInJob(handle, job, ctypes.byref(in_job))
        assert in_job.value
    finally:
        if handle:
            kernel32.CloseHandle(handle)
        await kill_tree(child)


async def test_spawn_still_works_when_the_job_cannot_be_used(monkeypatch):
    monkeypatch.setattr(proc_module, "_AssignProcessToJobObject", lambda job, process: 0)
    done = await run_capture([PY, "-c", "print('ok')"])
    assert done.stdout.strip() == "ok"

    monkeypatch.setattr(proc_module, "_job_ready", False)
    monkeypatch.setattr(proc_module, "_job", None)
    monkeypatch.setattr(proc_module, "_CreateJobObjectW", lambda attributes, name: None)
    done = await run_capture([PY, "-c", "print('still ok')"])
    assert done.stdout.strip() == "still ok"
    assert proc_module._job is None


async def test_spawn_kills_the_child_if_tying_it_to_its_jobs_fails(monkeypatch):
    pids: list[int] = []

    def broken_tie(pid):
        pids.append(pid)
        raise RuntimeError("tie failed")

    monkeypatch.setattr(proc_module, "_tie", broken_tie)
    with pytest.raises(RuntimeError, match="tie failed"):
        await spawn([PY, "-c", "import time; time.sleep(60)"])
    [pid] = pids
    try:
        assert await _wait_dead(pid), "the child outlived spawn's failure"
    finally:
        _force_kill(pid)


async def test_a_failure_after_the_childs_job_is_made_closes_that_job(monkeypatch):
    import threading
    import types

    jobs = []
    real_open = proc_module._open_child_job

    def recording_open(process):
        job = real_open(process)
        jobs.append(job)
        return job

    def no_threads(*args, **kwargs):
        raise MemoryError("can't start the watcher")

    monkeypatch.setattr(proc_module, "_open_child_job", recording_open)
    monkeypatch.setattr(proc_module, "threading", types.SimpleNamespace(Thread=no_threads, Lock=threading.Lock))
    with pytest.raises(MemoryError):
        await spawn([PY, "-c", "import time; time.sleep(60)"])
    [job] = jobs
    assert job is not None and job.handle is None


def _readline_within(stream, timeout: float) -> str:
    """stream.readline(), but "" after timeout seconds instead of hanging (Windows pipes can't be
    select()ed, so the read runs in a thread; it ends once the caller kills the writer)."""
    lines: list[str] = []
    reader = threading.Thread(target=lambda: lines.append(stream.readline()), daemon=True)
    reader.start()
    reader.join(timeout)
    return lines[0] if lines else ""


def test_children_die_when_snag_is_hard_killed():
    helper = subprocess.Popen(
        [PY, "-c", ORPHAN_SCRIPT, str(REPO_ROOT)],
        stdout=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True,
    )
    child_pid: int | None = None
    try:
        line = _readline_within(helper.stdout, 30)
        assert line.strip(), "helper never reported its child's pid"
        child_pid = int(line)
        assert _alive(child_pid)
        _force_kill(helper.pid)  # TerminateProcess on the helper alone, no /T
        helper.wait(10)
        deadline = time.monotonic() + 5
        while _alive(child_pid):
            assert time.monotonic() < deadline, f"child {child_pid} outlived the killed helper"
            time.sleep(0.1)
    finally:
        _force_kill(helper.pid)
        if child_pid is not None:
            _force_kill(child_pid)
        helper.wait(10)  # its exit closes the pipe, ending a read that timed out, before the close
        helper.stdout.close()


async def test_second_cancel_during_cleanup_still_kills_the_tree(tmp_path, monkeypatch):
    real_kill_tree = proc_module.kill_tree
    cleanup_started = asyncio.Event()
    cleanup_finished = asyncio.Event()

    async def slow_kill_tree(proc):
        cleanup_started.set()
        await asyncio.sleep(0.5)
        await real_kill_tree(proc)
        cleanup_finished.set()

    monkeypatch.setattr(proc_module, "kill_tree", slow_kill_tree)
    pid_file = tmp_path / "pids"
    task = asyncio.create_task(run_capture([PY, "-c", TREE_SCRIPT, str(pid_file)]))
    pids: list[int] = []
    try:
        pids = await _wait_for_pids(pid_file, task)
        task.cancel()
        await asyncio.wait_for(cleanup_started.wait(), 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleanup_finished.is_set(), "run_capture returned before the kill finished"
        for pid in pids:
            assert await _wait_dead(pid), f"pid {pid} survived a double cancellation"
    finally:
        for pid in pids:
            _force_kill(pid)


async def test_cleanup_failure_does_not_replace_the_cancellation(monkeypatch):
    real_kill_tree = proc_module.kill_tree

    async def failing_kill_tree(proc):
        await real_kill_tree(proc)
        raise TimeoutError("child did not exit")

    monkeypatch.setattr(proc_module, "kill_tree", failing_kill_tree)
    started = asyncio.Event()
    real_spawn = proc_module.spawn
    spawned: list = []

    async def recording_spawn(args):
        proc = await real_spawn(args)
        spawned.append(proc)
        started.set()
        return proc

    monkeypatch.setattr(proc_module, "spawn", recording_spawn)
    task = asyncio.create_task(run_capture([PY, "-c", "import time; time.sleep(60)"]))
    try:
        await asyncio.wait_for(started.wait(), 10)
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        assert any("child did not exit" in note for note in getattr(caught.value, "__notes__", []))
    finally:
        for proc in spawned:
            _force_kill(proc.pid)


async def test_cancel_during_timeout_cleanup_is_not_lost(monkeypatch):
    real_kill_tree = proc_module.kill_tree
    cleanup_started = asyncio.Event()
    cleanup_finished = asyncio.Event()
    killed: list = []

    async def slow_kill_tree(proc):
        killed.append(proc)
        cleanup_started.set()
        await asyncio.sleep(0.5)
        await real_kill_tree(proc)
        cleanup_finished.set()

    monkeypatch.setattr(proc_module, "kill_tree", slow_kill_tree)
    task = asyncio.create_task(run_capture([PY, "-c", "import time; time.sleep(60)"], timeout=0.5))
    try:
        await asyncio.wait_for(cleanup_started.wait(), 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        assert isinstance(caught.value.__context__, TimeoutError)
        assert cleanup_finished.is_set(), "run_capture returned before the kill finished"
    finally:
        for proc in killed:
            _force_kill(proc.pid)


async def test_kill_tree_reaps_a_taskkill_that_timed_out(no_child_job, monkeypatch):
    proc = await spawn([PY, "-c", "import time; time.sleep(60)"])
    real_exec = asyncio.create_subprocess_exec
    killers: list = []
    events: list[str] = []

    async def hanging_taskkill(program, *args, **kwargs):
        # Stands in for a taskkill that never finishes.
        killer = await real_exec(PY, "-c", "import time; time.sleep(60)", **kwargs)
        killers.append(killer)
        real_kill, real_wait = killer.kill, killer.wait

        def kill():
            events.append("kill")
            real_kill()

        async def wait():
            result = await real_wait()
            events.append("reaped")
            return result

        killer.kill, killer.wait = kill, wait
        return killer

    monkeypatch.setattr(asyncio, "create_subprocess_exec", hanging_taskkill)
    monkeypatch.setattr(proc_module, "_TASKKILL_TIMEOUT", 0.5)
    try:
        await asyncio.wait_for(kill_tree(proc), 20)
        assert proc.returncode is not None
        assert events[-2:] == ["kill", "reaped"]
        assert killers[0].returncode is not None
    finally:
        _force_kill(proc.pid)
        for killer in killers:
            _force_kill(killer.pid)


# --- each child in its own job ---

# Starts a sleeper (not tied to our pipes) and exits at once, printing the sleeper's pid.
MIDDLE_SCRIPT = (
    "import subprocess, sys\n"
    "sleeper = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],"
    " stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
    "print(sleeper.pid, flush=True)\n"
)

# Runs MIDDLE_SCRIPT (passed as argv[1]), so the sleeper's parent is gone and taskkill /T (which follows
# parent links) can't find it. Prints the sleeper's pid, then sleeps.
DETACHED_SCRIPT = (
    "import subprocess, sys, time\n"
    "out = subprocess.run([sys.executable, '-c', sys.argv[1]], capture_output=True, text=True).stdout\n"
    "print(out.strip(), flush=True)\n"
    "time.sleep(60)\n"
)

# Starts a sleeper that inherits (and so holds open) this process's stdout, prints its pid, and exits.
LEAVES_PIPE_HOLDER_SCRIPT = (
    "import subprocess, sys\n"
    "sleeper = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
    "print(sleeper.pid, flush=True)\n"
)


@pytest.fixture
def no_child_job(monkeypatch):
    """Children get no job of their own, as when Windows refuses one, so kill_tree must use taskkill."""
    monkeypatch.setattr(proc_module, "_open_child_job", lambda pid: None)


def _kernel32_is_process_in_job():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.IsProcessInJob.argtypes = (wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL))
    kernel32.IsProcessInJob.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    def in_job(pid: int, job) -> bool:
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        assert handle
        try:
            result = wintypes.BOOL()
            assert kernel32.IsProcessInJob(handle, job, ctypes.byref(result))
            return bool(result.value)
        finally:
            kernel32.CloseHandle(handle)

    return in_job


async def test_spawn_gives_each_child_its_own_job_inside_snags():
    in_job = _kernel32_is_process_in_job()
    first = await spawn([PY, "-c", "import time; time.sleep(60)"])
    second = await spawn([PY, "-c", "import time; time.sleep(60)"])
    try:
        first_job, second_job = proc_module._child_job_of(first), proc_module._child_job_of(second)
        assert first_job is not None and second_job is not None
        assert in_job(first.pid, first_job.handle) and not in_job(first.pid, second_job.handle)
        assert in_job(first.pid, proc_module._kill_on_close_job())
    finally:
        await kill_tree(first)
        await kill_tree(second)


async def test_the_childs_job_is_closed_once_it_exits():
    proc = await spawn([PY, "-c", "pass"])
    job = proc_module._child_job_of(proc)
    assert job is not None and job.handle
    await asyncio.wait_for(proc.wait(), 20)
    await wait_until(lambda: job.handle is None)


@pytest.mark.parametrize(("wait_result", "job_closed"), [
    pytest.param(lambda: proc_module._WAIT_OBJECT_0, True, id="exited"),
    pytest.param(lambda: proc_module._WAIT_FAILED, False, id="wait-failed"),
])
def test_the_watcher_closes_the_childs_job_only_once_the_child_has_exited(monkeypatch, wait_result, job_closed):
    # Closing a kill-on-close job ends everything in it, so it must not happen while the child may
    # still be running; after a failed wait the job is left for kill_tree (or Snag's exit).
    closed: list[int] = []
    monkeypatch.setattr(proc_module, "_CloseHandle", lambda handle: closed.append(handle) or True)
    monkeypatch.setattr(proc_module, "_WaitForSingleObject", lambda handle, milliseconds: wait_result())
    job = proc_module._ChildJob(456)
    proc_module._close_job_when_exited(123, job)
    assert 123 in closed  # the process handle is released either way
    assert (job.handle is None) is job_closed and (456 in closed) is job_closed


async def test_kill_tree_ends_what_an_exited_child_left_when_its_wait_failed(monkeypatch):
    monkeypatch.setattr(proc_module, "_WaitForSingleObject", lambda handle, milliseconds: proc_module._WAIT_FAILED)
    proc = await spawn([PY, "-c", LEAVES_PIPE_HOLDER_SCRIPT])
    sleeper = None
    try:
        sleeper = int((await asyncio.wait_for(proc.stdout.readline(), 20)).decode())
        await wait_until(lambda: proc.returncode is not None, 20)  # wait() would also wait for the pipe
        job = proc_module._child_job_of(proc)
        assert job is not None and job.handle is not None  # left open by the failed wait
        assert _alive(sleeper)
        await kill_tree(proc)
        assert await _wait_dead(sleeper)
    finally:
        if sleeper is not None:
            _force_kill(sleeper)
        await kill_tree(proc)
        if (job := proc_module._child_job_of(proc)) is not None:
            job.close()


async def test_kill_tree_ends_descendants_whose_parent_is_gone():
    proc = await spawn([PY, "-c", DETACHED_SCRIPT, MIDDLE_SCRIPT])
    sleeper: int | None = None
    try:
        sleeper = int((await asyncio.wait_for(proc.stdout.readline(), 20)).decode())
        assert _alive(sleeper)
        await kill_tree(proc)
        assert proc.returncode is not None
        assert await _wait_dead(sleeper), "taskkill alone can't find it; the child's job must end it"
    finally:
        _force_kill(proc.pid)
        if sleeper is not None:
            _force_kill(sleeper)


async def test_descendants_holding_the_pipes_end_when_the_child_exits():
    proc = await spawn([PY, "-c", LEAVES_PIPE_HOLDER_SCRIPT])
    sleeper: int | None = None
    try:
        sleeper = int((await asyncio.wait_for(proc.stdout.readline(), 20)).decode())
        # The sleeper still holds stdout, so EOF only comes once it has been ended.
        assert await asyncio.wait_for(proc.stdout.read(), 10) == b""
        assert await _wait_dead(sleeper)
        assert await asyncio.wait_for(proc.wait(), 10) == 0
    finally:
        _force_kill(proc.pid)
        if sleeper is not None:
            _force_kill(sleeper)


async def test_kill_tree_still_works_without_a_job_of_its_own(no_child_job):
    script = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "print(child.pid, flush=True)\n"
        "time.sleep(60)\n"
    )
    proc = await spawn([PY, "-c", script])
    assert proc_module._child_job_of(proc) is None
    child_pid: int | None = None
    try:
        child_pid = int((await proc.stdout.readline()).decode())
        await kill_tree(proc)
        assert proc.returncode is not None
        assert await _wait_dead(child_pid)
    finally:
        _force_kill(proc.pid)
        if child_pid is not None:
            _force_kill(child_pid)


async def test_spawn_can_merge_stderr_into_stdout_in_order():
    script = "import sys\nfor i in range(50):\n    print('out', i, flush=True)\n    print('err', i, file=sys.stderr, flush=True)\n"
    proc = await spawn([PY, "-c", script], merge_stderr=True)
    assert proc.stderr is None
    out, _ = await asyncio.wait_for(proc.communicate(), 20)
    expected = [f"{stream} {i}" for i in range(50) for stream in ("out", "err")]
    assert out.decode().splitlines() == expected


async def wait_until(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.02)
