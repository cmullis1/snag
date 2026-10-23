"""JobQueue lifecycle: preflight checks, starting up after a restart, shutting down, retrying and
batches (plan Task 19, revised)."""
import asyncio
import json
import sqlite3
import textwrap
from dataclasses import replace

import pytest

from snag.errors import ErrorKind, make_error
from snag.jobs.models import Job, JobState, new_id
from snag.jobs.workdir import TMP_FOLDER
from snag.store.jobs import CORRUPT_STATE
from tests.helpers import Recorder, make_spec, wait_for
from tests.queue_env import GONE, M4A, block_folder_removal, block_sweep, video, video_info_with


# --- preflight ---

async def test_folder_that_cannot_be_created(queue_env):
    blocker = queue_env.tmp_path / "afile"
    blocker.write_text("x")
    queue = queue_env.make_queue()
    job = queue.add(make_spec("fake://success", blocker))
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert job.state is JobState.FAILED
    assert job.error.kind is ErrorKind.FOLDER
    assert job.error.message == f"Can't save to {blocker}"


async def test_not_enough_disk_space(queue_env):
    queue = queue_env.make_queue()
    job = queue.add(make_spec("fake://success", queue_env.dest, estimated_size=10**18))
    queue_env.work_of(job).mkdir(parents=True)  # a partial from an earlier attempt
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert job.state is JobState.FAILED and job.error.kind is ErrorKind.DISK_FULL
    assert not queue_env.work_of(job).exists()  # "Not enough disk space": the working folder is deleted
    assert not list(queue_env.dest.glob(".snag-write-test-*"))


async def test_a_destination_unusable_at_retry_keeps_the_partial(queue_env):
    queue = queue_env.make_queue()
    job = queue.add(make_spec("fake://fail-network", queue_env.dest))
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert job.state is JobState.FAILED and job.error.kind is ErrorKind.NETWORK
    partial = queue_env.work_of(job) / "Fake Video.fv.mp4.part"
    partial.write_bytes(b"x" * 100)  # what the failed attempt had downloaded
    queue_env.dest.rmdir()
    queue_env.dest.write_text("not a folder")  # e.g. the drive's folder was replaced meanwhile
    queue.retry(job.id)
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert job.state is JobState.FAILED and job.error.kind is ErrorKind.FOLDER
    assert job.error.message == f"Can't save to {queue_env.dest}"
    assert job.error.keep_partial and partial.read_bytes() == b"x" * 100  # never lost to a bad folder
    queue_env.dest.unlink()  # the folder is usable again
    seen_partial_while_running = []
    queue.subscribe(lambda j: j.state is JobState.FETCHING and seen_partial_while_running.append(partial.exists()))
    job.spec = replace(job.spec, url="fake://success")  # the network is back too
    queue.retry(job.id)
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert job.state is JobState.DONE
    assert seen_partial_while_running == [True]  # the retry resumed in the same working folder


def partial_of(env, job, size: int):
    work = env.work_of(job)
    work.mkdir(parents=True)
    (work / "Fake Video.fv.mp4.part").write_bytes(b"x" * size)


async def test_a_resumed_job_needs_only_the_space_it_has_not_downloaded_yet(queue_env, monkeypatch):
    import shutil

    import snag.jobs.queue as queue_module

    real_usage = shutil.disk_usage
    monkeypatch.setattr(
        queue_module.shutil, "disk_usage", lambda path: real_usage(path)._replace(free=1_000_000),
    )
    queue = queue_env.make_queue()
    queue.pause()
    fits = queue.add(make_spec("fake://success", queue_env.dest, estimated_size=1_500_000))
    partial_of(queue_env, fits, 700_000)  # 800 000 bytes left to download, 880 000 with the margin
    too_big = queue.add(make_spec("fake://success", queue_env.dest, estimated_size=1_500_000))
    partial_of(queue_env, too_big, 500_000)  # 1 000 000 left, 1 100 000 with the margin
    queue.resume()
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert fits.state is JobState.DONE
    assert too_big.state is JobState.FAILED and too_big.error.kind is ErrorKind.DISK_FULL


# --- starting after a restart ---

def stored_job(env, url: str, state: JobState, **spec_overrides) -> Job:
    job = Job(spec=make_spec(url, env.dest, **spec_overrides))
    job.state = state
    if state is JobState.FAILED:
        job.error = make_error(ErrorKind.NETWORK, "youtube")
        job.finished_at = job.created_at
    env.store.save(job)
    return job


async def test_unfinished_jobs_resume_on_start(queue_env):
    leftover = stored_job(queue_env, "fake://success", JobState.DOWNLOADING)
    queue = queue_env.make_queue()
    recorder = Recorder()
    queue.subscribe(recorder)
    await queue.start()
    await asyncio.wait_for(queue.wait_idle(), 15)
    job = queue.get(leftover.id)
    assert job.state is JobState.DONE
    assert recorder.states(job.id)[:2] == [JobState.INTERRUPTED, JobState.WAITING]


async def test_start_drops_done_and_cancelled_rows(queue_env):
    done = stored_job(queue_env, "fake://success", JobState.DONE)
    cancelled = stored_job(queue_env, "fake://success", JobState.CANCELLED)
    queue = queue_env.make_queue()
    await queue.start()
    assert queue.jobs == []
    assert queue_env.store.load(done.id) is None and queue_env.store.load(cancelled.id) is None


async def test_failed_job_survives_a_restart(queue_env):
    first = queue_env.make_queue()
    job = first.add(make_spec("fake://fail-network", queue_env.dest))
    await asyncio.wait_for(first.wait_idle(), 15)
    await first.shutdown()
    second = queue_env.make_queue()
    recorder = Recorder()
    second.subscribe(recorder)
    await second.start()
    restored = second.get(job.id)
    assert restored is not None and restored.state is JobState.FAILED
    assert restored.error.kind is ErrorKind.NETWORK
    assert recorder.states(job.id) == [JobState.FAILED]  # shown, not re-run
    assert queue_env.work_of(restored).exists()  # its partial is kept for Retry


async def test_retry_after_a_restart_resumes_from_the_kept_partial(queue_env):
    failed = stored_job(queue_env, "fake://success", JobState.FAILED)
    partial = queue_env.work_of(failed) / "Fake Video.fv.mp4.part"
    partial.parent.mkdir(parents=True)
    partial.write_bytes(b"x" * 100)
    queue = queue_env.make_queue()
    await queue.start()
    assert partial.exists()  # not swept
    seen_partial_while_running = []
    queue.subscribe(lambda job: job.state is JobState.FETCHING and seen_partial_while_running.append(partial.exists()))
    job = queue.retry(failed.id)
    assert job.state is JobState.WAITING and job.error is None
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert job.state is JobState.DONE
    assert seen_partial_while_running == [True]  # the attempt ran in the same working folder


async def test_start_sweeps_orphan_working_folders_and_keeps_the_queues(queue_env):
    paths, dest = queue_env.paths, queue_env.dest
    waiting = stored_job(queue_env, "fake://success", JobState.WAITING)
    failed = stored_job(queue_env, "fake://fail-network", JobState.FAILED)
    second_dest = queue_env.tmp_path / "elsewhere"
    on_second_dest = stored_job(queue_env, "fake://fail-network", JobState.FAILED, dest_dir=second_dest)
    kept = [paths.work_dir / waiting.id, paths.work_dir / failed.id, second_dest / TMP_FOLDER / on_second_dest.id]
    orphans = [
        paths.work_dir / new_id(),
        dest / TMP_FOLDER / new_id(),
        second_dest / TMP_FOLDER / new_id(),
    ]
    extra_root = queue_env.tmp_path / "remembered"
    orphans.append(extra_root / TMP_FOLDER / new_id())
    not_a_job = paths.work_dir / "user-stuff"
    for folder in (*kept, *orphans, not_a_job):
        folder.mkdir(parents=True)
    queue = queue_env.make_queue()
    queue.pause()  # so the waiting job doesn't run and finish (which removes its folder)
    await queue.start(extra_roots=[extra_root])
    assert all(folder.exists() for folder in kept)
    assert not any(folder.exists() for folder in orphans)
    assert not_a_job.exists()


def gate_sweeps(monkeypatch, blocked: str):
    """Make sweeps of roots whose path contains `blocked` wait: returns (swept roots, sweeping, release).
    Always set release."""
    import threading

    import snag.jobs.queue as queue_module

    real_sweep = queue_module.sweep_orphans
    swept, sweeping, release = [], threading.Event(), threading.Event()

    def sweep(roots, keep_ids):
        if any(blocked in str(root) for root in roots):
            sweeping.set()
            release.wait(15)
        swept.extend(roots)
        return real_sweep(roots, keep_ids)

    monkeypatch.setattr(queue_module, "sweep_orphans", sweep)
    return swept, sweeping, release


async def test_network_and_deferred_roots_are_swept_after_start_returns(queue_env, monkeypatch):
    nas = queue_env.tmp_path / "nas"
    remembered = queue_env.tmp_path / "remembered"
    local = queue_env.tmp_path / "local"
    on_nas = stored_job(queue_env, "fake://fail-network", JobState.FAILED, dest_dir=nas / "jobs")
    orphans = [nas / "videos" / TMP_FOLDER / new_id(), nas / "jobs" / TMP_FOLDER / new_id(),
               remembered / TMP_FOLDER / new_id()]
    local_orphan = local / TMP_FOLDER / new_id()
    kept = nas / "jobs" / TMP_FOLDER / on_nas.id
    for folder in (*orphans, local_orphan, kept):
        folder.mkdir(parents=True)
    swept, sweeping, release = gate_sweeps(monkeypatch, "nas")
    queue = queue_env.make_queue(is_local=lambda folder: "nas" not in str(folder))
    try:
        await asyncio.wait_for(queue.start(extra_roots=[nas / "videos", local], deferred_roots=[remembered]), 5)
        assert not local_orphan.exists()  # local: before start() returned
        assert all(folder.exists() for folder in orphans)
        await wait_for(sweeping.is_set)
    finally:
        release.set()
    await asyncio.wait_for(queue.wait_swept(), 10)
    assert not any(folder.exists() for folder in orphans)
    assert kept.exists()
    later = [root for root in swept if root != queue_env.paths.work_dir and root != local / TMP_FOLDER]
    assert later == [nas / "jobs" / TMP_FOLDER, nas / "videos" / TMP_FOLDER, remembered / TMP_FOLDER]


async def test_a_sweep_that_takes_too_long_is_left_behind(queue_env, monkeypatch, caplog):
    import logging

    leftover = stored_job(queue_env, "fake://success", JobState.DOWNLOADING)
    swept, sweeping, release = gate_sweeps(monkeypatch, str(queue_env.paths.work_dir))
    queue = queue_env.make_queue(sweep_timeout=0.3)
    try:
        with caplog.at_level(logging.WARNING, logger="snag.jobs.queue"):
            await asyncio.wait_for(queue.start(), 5)
        assert "took over" in caplog.text
        await asyncio.wait_for(queue.wait_idle(), 15)  # the queue runs meanwhile
        assert queue.get(leftover.id).state is JobState.DONE
    finally:
        release.set()


async def test_a_late_sweep_keeps_the_folders_of_jobs_added_meanwhile(queue_env, monkeypatch):
    remembered = queue_env.tmp_path / "remembered"
    orphan = remembered / TMP_FOLDER / new_id()
    orphan.mkdir(parents=True)
    swept, sweeping, release = gate_sweeps(monkeypatch, "remembered")
    queue = queue_env.make_queue()
    try:
        await queue.start(deferred_roots=[remembered])
        await wait_for(sweeping.is_set)
        queue.pause()
        job = queue.add(make_spec("fake://success", remembered))
        working = remembered / TMP_FOLDER / job.id  # as if its download had begun there
        working.mkdir()
    finally:
        release.set()
    await asyncio.wait_for(queue.wait_swept(), 10)
    assert not orphan.exists() and working.exists()


async def test_shutdown_stops_the_background_sweep(queue_env, monkeypatch):
    remembered = [queue_env.tmp_path / f"remembered{n}" for n in range(3)]
    swept, sweeping, release = gate_sweeps(monkeypatch, "remembered0")
    queue = queue_env.make_queue()
    try:
        await queue.start(deferred_roots=remembered)
        await wait_for(sweeping.is_set)
        await asyncio.wait_for(queue.shutdown(), 5)  # doesn't wait for the stuck sweep
    finally:
        release.set()
    await asyncio.wait_for(queue.wait_swept(), 5)
    assert remembered[2] / TMP_FOLDER not in swept


def test_local_drive():
    from pathlib import Path

    from snag.jobs.queue import local_drive

    assert local_drive(Path(__file__)) is True
    assert local_drive(Path(r"\\server\share\Videos")) is False
    assert local_drive(Path("relative")) is False


async def test_start_deletes_leftover_cookie_files(queue_env):
    cookies = queue_env.paths.cookies_dir
    cookies.mkdir(parents=True)
    (cookies / f"{new_id()}.txt").write_text("secret")
    queue = queue_env.make_queue()
    await queue.start()
    assert list(cookies.iterdir()) == []


async def test_start_prunes_corrupt_rows_and_sweeps_their_folders(queue_env):
    corrupt_id = new_id()
    queue_env.conn.execute(
        "INSERT INTO jobs(id, state, data, created_at) VALUES(?, 'waiting', '{', 1.0)", (corrupt_id,),
    )
    folder = queue_env.paths.work_dir / corrupt_id
    folder.mkdir(parents=True)
    queue = queue_env.make_queue()
    await queue.start()
    assert queue.jobs == []
    assert not folder.exists()
    assert queue_env.conn.execute("SELECT count(*) FROM jobs WHERE state = ?", (CORRUPT_STATE,)).fetchone()[0] == 0
    evidence = queue_env.paths.root / "jobs-corrupt.jsonl"
    assert [json.loads(line)["id"] for line in evidence.read_text(encoding="utf-8").splitlines()] == [corrupt_id]


async def test_start_restores_the_queue_when_its_cleanup_fails(queue_env, monkeypatch, caplog):
    import logging

    import snag.jobs.queue as queue_module

    def refuse(*args):
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(queue_module, "clear_cookies_dir", refuse)
    monkeypatch.setattr(queue_module, "sweep_orphans", refuse)
    leftover = stored_job(queue_env, "fake://success", JobState.DOWNLOADING)
    failed = stored_job(queue_env, "fake://fail-network", JobState.FAILED)
    queue = queue_env.make_queue()
    with caplog.at_level(logging.WARNING, logger="snag.jobs.queue"):
        await queue.start()
        await asyncio.wait_for(queue.wait_idle(), 15)
    assert queue.get(leftover.id).state is JobState.DONE
    assert queue.get(failed.id).state is JobState.FAILED
    logged = [r.exc_info[1] for r in caplog.records if r.exc_info]
    assert len(logged) == 2 and all(isinstance(error, PermissionError) for error in logged)


async def test_a_job_added_while_starting_up_waits_for_the_sweep(queue_env, monkeypatch):
    sweeping, release = block_sweep(monkeypatch)
    queue = queue_env.make_queue()
    starting = asyncio.create_task(queue.start())
    try:
        await wait_for(sweeping.is_set)
        job = queue.add(make_spec("fake://success", queue_env.dest))
        await asyncio.sleep(0.3)
        assert job.state is JobState.WAITING  # its working folder could be swept as an orphan
    finally:
        release.set()
    await starting
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert job.state is JobState.DONE


async def test_wait_idle_waits_for_start(queue_env, monkeypatch):
    sweeping, release = block_sweep(monkeypatch)
    queue = queue_env.make_queue()
    starting = asyncio.create_task(queue.start())
    try:
        await wait_for(sweeping.is_set)
        idle = asyncio.create_task(queue.wait_idle())
        await asyncio.sleep(0.2)
        assert not idle.done()
    finally:
        release.set()
    await starting
    await asyncio.wait_for(idle, 5)


async def test_start_twice_or_after_downloads_began_is_refused(queue_env):
    queue = queue_env.make_queue()
    await queue.start()
    with pytest.raises(RuntimeError):
        await queue.start()
    busy = queue_env.make_queue()
    job = busy.add(make_spec("fake://slow", queue_env.dest))
    await wait_for(lambda: job.state is JobState.DOWNLOADING)
    with pytest.raises(RuntimeError):
        await busy.start()
    await busy.cancel(job.id)


# --- shutting down ---

async def test_shutdown_interrupts_and_keeps_partial(queue_env):
    queue = queue_env.make_queue()
    job = queue.add(make_spec("fake://slow", queue_env.dest))
    await wait_for(lambda: job.state is JobState.DOWNLOADING)
    await queue.shutdown()
    assert job.state is JobState.INTERRUPTED
    assert queue_env.store.load(job.id).state is JobState.INTERRUPTED
    assert queue_env.work_of(job).exists()
    assert queue_env.history.list() == []


async def test_shutdown_leaves_waiting_jobs_for_next_time(queue_env):
    queue = queue_env.make_queue(max_concurrent=1)
    running = queue.add(make_spec("fake://slow", queue_env.dest))
    waiting = queue.add(make_spec("fake://success", queue_env.dest))
    await wait_for(lambda: running.state is JobState.DOWNLOADING)
    await queue.shutdown()
    assert waiting.state is JobState.WAITING
    after_restart = queue_env.make_queue()
    after_restart.pause()
    await after_restart.start()
    assert {j.id for j in after_restart.jobs} == {running.id, waiting.id}
    assert all(j.state is JobState.WAITING for j in after_restart.jobs)


async def test_shutdown_while_throttled_does_not_wait_for_the_pause(queue_env):
    import uuid

    queue = queue_env.make_queue(throttle_pause=300.0)
    job = queue.add(make_spec(f"fake://throttle-once/{uuid.uuid4().hex}", queue_env.dest))
    await wait_for(lambda: queue.throttled)
    await asyncio.wait_for(queue.shutdown(), 5)
    assert job.state is JobState.WAITING


async def test_shutdown_waits_for_a_waiting_job_being_cancelled(queue_env, monkeypatch):
    started, release = block_folder_removal(monkeypatch)
    queue = queue_env.make_queue()
    queue.pause()
    job = queue.add(make_spec("fake://success", queue_env.dest))
    cancelling = asyncio.create_task(queue.cancel(job.id))
    try:
        await wait_for(started.is_set)
        closing = asyncio.create_task(queue.shutdown())
        await asyncio.sleep(0.2)
        assert not closing.done()
    finally:
        release.set()
    await asyncio.wait_for(closing, 15)
    assert job.state is JobState.CANCELLED
    assert queue_env.store.load(job.id).state is JobState.CANCELLED
    await cancelling


async def test_shutdown_waits_for_a_clear(queue_env, monkeypatch):
    failed = stored_job(queue_env, "fake://fail-network", JobState.FAILED)
    queue_env.work_of(failed).mkdir(parents=True)
    queue = queue_env.make_queue()
    await queue.start()
    started, release = block_folder_removal(monkeypatch)
    clearing = asyncio.create_task(queue.clear(failed.id))
    try:
        await wait_for(started.is_set)
        closing = asyncio.create_task(queue.shutdown())
        await asyncio.sleep(0.2)
        assert not closing.done()
    finally:
        release.set()
    await asyncio.wait_for(closing, 15)
    assert not queue_env.work_of(failed).exists()
    await clearing


# Runs in a child process so asyncio.run can tear its own event loop down, as the app's would.
_TEARDOWN_SCRIPT = textwrap.dedent("""
    import asyncio, gc, sys
    from pathlib import Path
    from snag.jobs.models import JobState
    from snag.jobs.queue import JobQueue
    from snag.paths import AppPaths, Tools
    from snag.store.db import open_db
    from snag.store.history import HistoryStore
    from snag.store.jobs import JobStore
    from tests.helpers import make_spec
    from tests.queue_env import no_lookup

    folder = Path(sys.argv[1])
    tools = Tools(ytdlp=(sys.executable, sys.argv[2]), ffmpeg_dir=folder / "bin", deno=folder / "bin" / "deno.exe")
    paths = AppPaths(folder / "Snag")
    conn = open_db(paths.db_file)
    seen = []

    async def main():
        queue = JobQueue(tools=tools, paths=paths, job_store=JobStore(conn), history=HistoryStore(conn),
                         lookup=no_lookup, cookies=lambda url: None, max_concurrent=1)
        queue.subscribe(lambda job: seen.append((job.id, job.state.value)))
        running = queue.add(make_spec("fake://slow", folder / "out"))
        waiting = queue.add(make_spec("fake://success", folder / "out"))
        while running.state is not JobState.DOWNLOADING:
            await asyncio.sleep(0.02)
        return running.id, waiting.id  # no shutdown(): asyncio.run cancels the running job's task

    running_id, waiting_id = asyncio.run(main())
    gc.collect()
    store = JobStore(conn)
    print(store.load(running_id).state.value, store.load(waiting_id).state.value,
          ",".join(sorted({state for job_id, state in seen if job_id == waiting_id})))
    conn.close()
""")


def test_ending_the_event_loop_without_shutdown_starts_nothing_new(tmp_path, fake_tools):
    import subprocess
    import sys
    from pathlib import Path

    result = subprocess.run(
        [sys.executable, "-X", "dev", "-c", _TEARDOWN_SCRIPT, str(tmp_path), fake_tools.ytdlp[1]],
        cwd=Path(__file__).parents[1], capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["interrupted", "waiting", "waiting"], result.stderr
    for warning in ("Task was destroyed", "Event loop is closed", "Exception ignored", "never awaited"):
        assert warning not in result.stderr, result.stderr


def job_task(job) -> asyncio.Task:
    [task] = [t for t in asyncio.all_tasks() if t.get_name() == f"snag-job-{job.id}"]
    return task


async def test_a_job_whose_task_is_cancelled_is_left_to_resume(queue_env):
    queue = queue_env.make_queue(max_concurrent=1)
    running = queue.add(make_spec("fake://slow", queue_env.dest))
    waiting = queue.add(make_spec("fake://success", queue_env.dest))
    await wait_for(lambda: running.state is JobState.DOWNLOADING)
    task = job_task(running)
    task.cancel()  # as when the event loop ends without shutdown()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert running.state is JobState.INTERRUPTED
    assert queue_env.store.load(running.id).state is JobState.INTERRUPTED
    await asyncio.sleep(0.3)
    assert waiting.state is JobState.WAITING  # nothing new starts


async def test_a_cancelled_job_whose_task_is_cancelled_stays_cancelled(queue_env, monkeypatch):
    started, release = block_folder_removal(monkeypatch)
    queue = queue_env.make_queue()
    job = queue.add(make_spec("fake://slow", queue_env.dest))
    await wait_for(lambda: job.state is JobState.DOWNLOADING)
    task = job_task(job)
    await queue.cancel(job.id)
    try:
        await wait_for(started.is_set)  # deleting the cancelled job's folder
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
    assert job.state is JobState.CANCELLED
    assert queue_env.store.load(job.id).state is JobState.CANCELLED
    assert queue_env.history.list() == []


async def test_jobs_added_after_shutdown_wait_for_next_time(queue_env):
    queue = queue_env.make_queue()
    await queue.shutdown()
    job = queue.add(make_spec("fake://success", queue_env.dest))
    await asyncio.sleep(0.2)
    assert job.state is JobState.WAITING
    assert queue_env.store.load(job.id).state is JobState.WAITING


# --- retrying ---

async def test_retry_failed_job(queue_env):
    queue = queue_env.make_queue()
    job = queue.add(make_spec("fake://fail-network", queue_env.dest))
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert job.state is JobState.FAILED
    queue.retry(job.id)
    assert job.state is JobState.WAITING and job.error is None and job.finished_at is None
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert job.state is JobState.FAILED


async def test_retry_refused_when_pointless(queue_env):
    queue = queue_env.make_queue()
    job = queue.add(make_spec("fake://unavailable", queue_env.dest))
    await asyncio.wait_for(queue.wait_idle(), 15)
    with pytest.raises(ValueError):
        queue.retry(job.id)


async def test_retry_refused_for_jobs_still_going(queue_env):
    queue = queue_env.make_queue()
    queue.pause()
    job = queue.add(make_spec("fake://success", queue_env.dest))
    with pytest.raises(ValueError):
        queue.retry(job.id)


async def test_retry_a_cancelled_job(queue_env):
    queue = queue_env.make_queue()
    queue.pause()
    job = queue.add(make_spec("fake://success", queue_env.dest))
    await queue.cancel(job.id)
    queue.resume()
    queue.retry(job.id)
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert job.state is JobState.DONE


async def test_batch_summary_and_retry_failed(queue_env):
    queue = queue_env.make_queue()
    batch = queue.new_batch_id()
    dest = queue_env.dest
    queue.add_batch([
        make_spec("fake://success", dest, batch_id=batch, playlist_index=1, playlist_count=3),
        make_spec("fake://fail-network", dest, batch_id=batch, playlist_index=2, playlist_count=3),
        make_spec("fake://success", dest, batch_id=batch, playlist_index=3, playlist_count=3),
        make_spec("fake://unavailable", dest, batch_id=batch, playlist_index=4, playlist_count=4),
    ])
    await asyncio.wait_for(queue.wait_idle(), 20)
    summary = queue.batch_summary(batch)
    assert (summary.total, summary.done, summary.failed, summary.cancelled, summary.pending) == (4, 2, 2, 0, 0)
    retried = queue.retry_failed(batch)
    assert len(retried) == 1  # retrying the unavailable one wouldn't help
    assert queue.batch_summary(batch).pending == 1
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert queue.batch_summary("other") == type(summary)(0, 0, 0, 0, 0)


def summary_tuple(summary):
    return (summary.total, summary.done, summary.failed, summary.cancelled, summary.pending)


async def test_a_batch_summary_survives_a_restart(queue_env):
    first = queue_env.make_queue()
    batch = first.new_batch_id()
    dest = queue_env.dest
    first.add_batch([
        make_spec("fake://success", dest, batch_id=batch, playlist_index=1, playlist_count=3),
        make_spec("fake://fail-network", dest, batch_id=batch, playlist_index=2, playlist_count=3),
        make_spec("fake://success", dest, batch_id=batch, playlist_index=3, playlist_count=3),
    ])
    await asyncio.wait_for(first.wait_idle(), 20)
    await first.shutdown()
    second = queue_env.make_queue()
    await second.start()
    assert len(second.jobs) == 1  # only the failed one comes back; the done ones are only in History
    assert summary_tuple(second.batch_summary(batch)) == (3, 2, 1, 0, 0)
    [retried] = second.retry_failed(batch)
    assert summary_tuple(second.batch_summary(batch)) == (3, 2, 0, 0, 1)  # the queue's state wins
    await asyncio.wait_for(second.wait_idle(), 15)
    assert summary_tuple(second.batch_summary(batch)) == (3, 2, 1, 0, 0)


async def test_a_batch_summary_counts_jobs_cleared_from_the_queue(queue_env):
    queue = queue_env.make_queue()
    batch = queue.new_batch_id()
    done, failed, cancelled = queue.add_batch([
        make_spec("fake://success", queue_env.dest, batch_id=batch),
        make_spec("fake://fail-network", queue_env.dest, batch_id=batch),
        make_spec("fake://slow", queue_env.dest, batch_id=batch),
    ])
    await wait_for(lambda: cancelled.state is JobState.DOWNLOADING)
    await queue.cancel(cancelled.id)
    await asyncio.wait_for(queue.wait_idle(), 15)
    queue.clear_finished()
    await queue.clear(failed.id)
    assert queue.jobs == []
    # Done and failed downloads are counted from History; a cancelled one has no History entry.
    assert summary_tuple(queue.batch_summary(batch)) == (2, 1, 1, 0, 0)
    assert queue.retry_failed(batch) == []  # nothing left in the queue to retry


async def test_a_batch_summary_without_history_counts_the_queue(queue_env, caplog):
    from snag.store.history import HistoryStore

    class UnreadableHistory(HistoryStore):
        def batch_statuses(self, batch_id):
            raise sqlite3.OperationalError("disk I/O error")

    queue = queue_env.make_queue(history=UnreadableHistory(queue_env.conn))
    queue.pause()
    batch = queue.new_batch_id()
    queue.add(make_spec("fake://success", queue_env.dest, batch_id=batch))
    assert summary_tuple(queue.batch_summary(batch)) == (1, 0, 0, 0, 1)
    assert "Couldn't read batch" in caplog.text


async def test_a_job_cancelled_while_starting_up_stays_cancelled(queue_env, monkeypatch):
    leftover = stored_job(queue_env, "fake://success", JobState.DOWNLOADING)
    sweeping, release = block_sweep(monkeypatch)
    queue = queue_env.make_queue()
    starting = asyncio.create_task(queue.start())
    try:
        await wait_for(sweeping.is_set)
        await queue.cancel(leftover.id)
    finally:
        release.set()
    await starting
    job = queue.get(leftover.id)
    assert job.state is JobState.CANCELLED
    await asyncio.sleep(0.2)
    assert job.state is JobState.CANCELLED and queue_env.store.load(job.id).state is JobState.CANCELLED


async def test_a_resumed_job_may_look_for_a_replacement_format_again(queue_env):
    leftover = stored_job(queue_env, "fake://format-gone", JobState.INTERRUPTED, choice=GONE)
    leftover.refetched = True  # an earlier session already replaced a format once
    queue_env.store.save(leftover)

    async def lookup(url):
        return video_info_with([M4A, video("299", 1080, "avc1.64002A")])

    queue = queue_env.make_queue(lookup=lookup)
    await queue.start()
    await asyncio.wait_for(queue.wait_idle(), 15)
    job = queue.get(leftover.id)
    assert job.state is JobState.DONE and job.spec.choice.format_id == "299"
