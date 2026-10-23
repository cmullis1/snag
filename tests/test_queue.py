"""JobQueue: scheduling, cancelling, history, clearing, and keeping one bad job or listener from
breaking the rest (plan Task 17, revised)."""
import asyncio
import logging

import pytest

from snag.errors import ErrorKind
from snag.jobs.models import ACTIVE_STATES, END_STATES, ChoiceKind, FormatChoice, JobState
from snag.store.jobs import JobStore
from tests.helpers import Recorder, make_spec, wait_for
from tests.queue_env import (
    COOKIES, DISK_FULL, FailingHistory, FailingJobStore, FailOnceJobStore, block_folder_removal,
)


async def test_single_download(queue_env):
    queue = queue_env.make_queue()
    recorder = Recorder()
    queue.subscribe(recorder)
    job = queue.add(make_spec("fake://success", queue_env.dest))
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert job.state is JobState.DONE
    assert job.percent == 100.0
    assert job.file_path == queue_env.dest / "Fake Video.mp4" and job.file_path.exists()
    assert job.subtitles_skipped is False
    states = recorder.states(job.id)
    assert states[0] is JobState.WAITING and states[-1] is JobState.DONE
    assert JobState.DOWNLOADING in states
    assert [e.status for e in queue_env.history.list()] == ["done"]
    assert queue_env.store.load(job.id).state is JobState.DONE
    assert not queue_env.work_of(job).exists()


async def test_concurrency_limit(queue_env):
    queue = queue_env.make_queue(max_concurrent=2)
    running: set[str] = set()
    peak = [0]

    def track(job):
        if job.state in ACTIVE_STATES:
            running.add(job.id)
        elif job.state in END_STATES:
            running.discard(job.id)
        peak[0] = max(peak[0], len(running))

    queue.subscribe(track)
    jobs = [queue.add(make_spec("fake://medium", queue_env.dest)) for _ in range(4)]
    await asyncio.wait_for(queue.wait_idle(), 30)
    assert all(job.state is JobState.DONE for job in jobs)
    assert peak[0] == 2


async def test_same_title_twice_gets_unique_names(queue_env):
    queue = queue_env.make_queue(max_concurrent=1)
    first = queue.add(make_spec("fake://success", queue_env.dest))
    second = queue.add(make_spec("fake://success", queue_env.dest))
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert {first.file_path.name, second.file_path.name} == {"Fake Video.mp4", "Fake Video (2).mp4"}


async def test_cancel_running_job(queue_env):
    queue = queue_env.make_queue()
    job = queue.add(make_spec("fake://slow", queue_env.dest))
    await wait_for(lambda: job.state is JobState.DOWNLOADING)
    await queue.cancel(job.id)
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert job.state is JobState.CANCELLED
    assert not queue_env.work_of(job).exists()
    assert queue_env.history.list() == []
    assert queue_env.store.load(job.id).state is JobState.CANCELLED


async def test_cancel_waiting_job(queue_env):
    queue = queue_env.make_queue(max_concurrent=1)
    running = queue.add(make_spec("fake://slow", queue_env.dest))
    waiting = queue.add(make_spec("fake://success", queue_env.dest))
    await wait_for(lambda: running.state is JobState.DOWNLOADING)
    await queue.cancel(waiting.id)
    assert waiting.state is JobState.CANCELLED
    await queue.cancel(running.id)
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert running.state is JobState.CANCELLED


async def test_cancelling_a_waiting_job_deletes_its_kept_partial(queue_env):
    queue = queue_env.make_queue()
    queue.pause()
    job = queue.add(make_spec("fake://success", queue_env.dest))
    queue_env.work_of(job).mkdir(parents=True)  # e.g. left by an interrupted attempt
    await queue.cancel(job.id)
    assert job.state is JobState.CANCELLED
    assert not queue_env.work_of(job).exists()


async def test_cancel_ignores_finished_jobs(queue_env):
    queue = queue_env.make_queue()
    job = queue.add(make_spec("fake://success", queue_env.dest))
    await asyncio.wait_for(queue.wait_idle(), 15)
    await queue.cancel(job.id)
    assert job.state is JobState.DONE


async def test_unknown_job_ids_raise_key_error(queue_env):
    queue = queue_env.make_queue()
    with pytest.raises(KeyError):
        await queue.cancel("missing")
    with pytest.raises(KeyError):
        queue.retry("missing")
    with pytest.raises(KeyError):
        await queue.clear("missing")


async def test_cancelling_a_waiting_job_twice_finishes_it_once(queue_env, monkeypatch):
    started, release = block_folder_removal(monkeypatch)
    queue = queue_env.make_queue()
    queue.pause()
    recorder = Recorder()
    queue.subscribe(recorder)
    job = queue.add(make_spec("fake://success", queue_env.dest))
    first = asyncio.create_task(queue.cancel(job.id))
    try:
        await wait_for(started.is_set)
        await asyncio.wait_for(queue.cancel(job.id), 5)  # already being cancelled: returns at once
        assert not first.done()
    finally:
        release.set()
    await first
    assert [state for _, state, _ in recorder.events].count(JobState.CANCELLED) == 1


async def test_a_cancel_that_is_cut_short_still_ends_the_job_cancelled(queue_env, monkeypatch):
    started, release = block_folder_removal(monkeypatch)
    queue = queue_env.make_queue()
    queue.pause()
    job = queue.add(make_spec("fake://success", queue_env.dest))
    cancelling = asyncio.create_task(queue.cancel(job.id))
    try:
        await wait_for(started.is_set)
        cancelling.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelling
    finally:
        release.set()
    assert job.state is JobState.CANCELLED  # not left waiting outside the queue
    assert queue_env.store.load(job.id).state is JobState.CANCELLED
    queue.resume()
    await asyncio.wait_for(queue.wait_idle(), 5)
    assert job.state is JobState.CANCELLED


async def test_a_cancel_during_the_failure_cleanup_ends_the_job_cancelled(queue_env, monkeypatch):
    started, release = block_folder_removal(monkeypatch)
    queue = queue_env.make_queue()
    # Fails its disk space check, so the queue deletes the working folder before recording the failure.
    job = queue.add(make_spec("fake://success", queue_env.dest, estimated_size=10**18))
    try:
        await wait_for(started.is_set)
        await queue.cancel(job.id)
    finally:
        release.set()
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert job.state is JobState.CANCELLED and job.error is None
    assert queue_env.history.list() == []
    assert queue_env.store.load(job.id).state is JobState.CANCELLED


async def test_failure_recorded_in_history(queue_env):
    queue = queue_env.make_queue()
    job = queue.add(make_spec("fake://fail-network", queue_env.dest))
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert job.state is JobState.FAILED
    assert job.error.kind is ErrorKind.NETWORK
    entries = queue_env.history.list()
    assert entries[0].status == "failed" and entries[0].error_message == "Lost connection"
    assert queue_env.work_of(job).exists()  # resumable, so the partial is kept for Retry


async def test_pause_and_resume(queue_env):
    queue = queue_env.make_queue()
    queue.pause()
    assert queue.paused
    job = queue.add(make_spec("fake://success", queue_env.dest))
    await asyncio.sleep(0.3)
    assert job.state is JobState.WAITING
    queue.resume()
    assert not queue.paused
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert job.state is JobState.DONE


async def test_set_max_concurrent_validates(queue_env):
    queue = queue_env.make_queue()
    for bad in (0, 11):
        with pytest.raises(ValueError):
            queue.set_max_concurrent(bad)
    queue.set_max_concurrent(3)
    assert queue.current_limit == 3


async def test_the_queue_accepts_exactly_the_limits_settings_allow(queue_env, monkeypatch):
    import snag.jobs.queue as queue_module
    from snag.store.settings import MAX_CONCURRENT_RANGE

    queue = queue_env.make_queue()
    queue.pause()
    for limit in MAX_CONCURRENT_RANGE:
        queue.set_max_concurrent(limit)
    for bad in (MAX_CONCURRENT_RANGE.start - 1, MAX_CONCURRENT_RANGE.stop):
        with pytest.raises(ValueError):
            queue.set_max_concurrent(bad)
    # One constant: a different range in settings changes what the queue accepts too.
    monkeypatch.setattr(queue_module, "MAX_CONCURRENT_RANGE", range(1, 4))
    with pytest.raises(ValueError, match="from 1 to 3"):
        queue.set_max_concurrent(4)


async def test_raising_the_limit_starts_waiting_jobs(queue_env):
    queue = queue_env.make_queue(max_concurrent=1)
    jobs = [queue.add(make_spec("fake://slow", queue_env.dest)) for _ in range(2)]
    await wait_for(lambda: jobs[0].state is JobState.DOWNLOADING)
    assert jobs[1].state is JobState.WAITING
    queue.set_max_concurrent(2)
    await wait_for(lambda: jobs[1].state is JobState.DOWNLOADING)
    for job in jobs:
        await queue.cancel(job.id)
    await asyncio.wait_for(queue.wait_idle(), 15)


async def test_jobs_and_get(queue_env):
    queue = queue_env.make_queue()
    queue.pause()
    first = queue.add(make_spec("fake://success", queue_env.dest))
    second = queue.add(make_spec("fake://success", queue_env.dest))
    assert [j.id for j in queue.jobs] == [first.id, second.id]
    assert queue.get(second.id) is second and queue.get("missing") is None


# --- one bad job or listener never breaks the queue ---

async def test_a_listener_that_raises_does_not_break_the_queue(queue_env, caplog):
    queue = queue_env.make_queue()

    def broken(job):
        raise RuntimeError("listener bug")

    recorder = Recorder()
    queue.subscribe(broken)
    queue.subscribe(recorder)
    with caplog.at_level(logging.ERROR, logger="snag.jobs.queue"):
        job = queue.add(make_spec("fake://success", queue_env.dest))
        await asyncio.wait_for(queue.wait_idle(), 15)
    assert job.state is JobState.DONE
    assert recorder.states(job.id)[-1] is JobState.DONE and JobState.DOWNLOADING in recorder.states(job.id)
    assert "listener bug" in caplog.text


async def test_an_unexpected_error_fails_only_that_job(queue_env):
    async def broken_lookup(url):
        raise RuntimeError("lookup bug")

    queue = queue_env.make_queue(lookup=broken_lookup, max_concurrent=1)
    spec = make_spec("fake://format-gone", queue_env.dest, choice=FormatChoice(ChoiceKind.VIDEO, "gone", "140", 1080, "mp4"))
    broken = queue.add(spec)
    fine = queue.add(make_spec("fake://success", queue_env.dest))
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert broken.state is JobState.FAILED
    assert broken.error.kind is ErrorKind.UNKNOWN and "lookup bug" in broken.error.details
    assert fine.state is JobState.DONE


async def test_retrying_from_a_listener_runs_the_job_again_once(queue_env):
    queue = queue_env.make_queue()
    retried = []

    def retry_once(job):
        if job.state is JobState.FAILED and not retried:
            retried.append(job.id)
            queue.retry(job.id)

    recorder = Recorder()
    queue.subscribe(retry_once)
    queue.subscribe(recorder)
    job = queue.add(make_spec("fake://fail-network", queue_env.dest))
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert job.state is JobState.FAILED
    assert recorder.states(job.id).count(JobState.FETCHING) == 2


# --- clearing ---

async def test_clear_deletes_the_working_folder_and_the_row_and_notifies(queue_env):
    queue = queue_env.make_queue()
    removed: list[str] = []
    queue.subscribe_removed(removed.append)
    job = queue.add(make_spec("fake://fail-network", queue_env.dest))
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert queue_env.work_of(job).exists()
    await queue.clear(job.id)
    assert not queue_env.work_of(job).exists()
    assert queue_env.store.load(job.id) is None
    assert queue.get(job.id) is None and job not in queue.jobs
    assert removed == [job.id]
    assert [e.status for e in queue_env.history.list()] == ["failed"]  # History keeps its record


async def test_clear_a_cancelled_job(queue_env):
    queue = queue_env.make_queue()
    queue.pause()
    job = queue.add(make_spec("fake://success", queue_env.dest))
    await queue.cancel(job.id)
    await queue.clear(job.id)
    assert queue.get(job.id) is None and queue_env.store.load(job.id) is None


async def test_clear_refuses_jobs_that_are_waiting_or_running(queue_env):
    queue = queue_env.make_queue()
    queue.pause()
    waiting = queue.add(make_spec("fake://success", queue_env.dest))
    with pytest.raises(ValueError):
        await queue.clear(waiting.id)
    assert queue.get(waiting.id) is waiting
    queue.resume()
    running = queue.add(make_spec("fake://slow", queue_env.dest))
    await wait_for(lambda: running.state is JobState.DOWNLOADING)
    with pytest.raises(ValueError):
        await queue.clear(running.id)
    assert queue.get(running.id) is running
    await queue.cancel(running.id)


async def test_clear_a_done_job_keeps_its_file(queue_env):
    queue = queue_env.make_queue()
    removed: list[str] = []
    queue.subscribe_removed(removed.append)
    job = queue.add(make_spec("fake://success", queue_env.dest))
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert job.state is JobState.DONE and job.file_path.is_file()
    assert queue_env.store.load(job.id) is not None  # done rows stay until the next start
    await queue.clear(job.id)
    assert queue.get(job.id) is None and queue_env.store.load(job.id) is None
    assert removed == [job.id]
    assert job.file_path.is_file()
    assert [e.status for e in queue_env.history.list()] == ["done"]


async def test_unsubscribed_listeners_hear_nothing_more(queue_env):
    queue = queue_env.make_queue()
    jobs, removed, states = [], [], []
    queue.subscribe(jobs.append)
    queue.subscribe_removed(removed.append)
    queue.subscribe_state(states.append)
    for listener in (jobs.append, removed.append, states.append):
        queue.unsubscribe(listener)
    job = queue.add(make_spec("fake://success", queue_env.dest))
    await asyncio.wait_for(queue.wait_idle(), 15)
    await queue.clear(job.id)
    assert (jobs, removed, states) == ([], [], [])


async def test_clear_finished_drops_done_and_cancelled_but_keeps_failed(queue_env):
    queue = queue_env.make_queue()
    removed: list[str] = []
    queue.subscribe_removed(removed.append)
    done = queue.add(make_spec("fake://success", queue_env.dest))
    failed = queue.add(make_spec("fake://fail-network", queue_env.dest))
    await asyncio.wait_for(queue.wait_idle(), 15)
    queue.pause()
    cancelled = queue.add(make_spec("fake://success", queue_env.dest))
    await queue.cancel(cancelled.id)
    queue.clear_finished()
    assert queue.jobs == [failed]
    assert sorted(removed) == sorted([done.id, cancelled.id])


async def test_a_removal_listener_that_raises_does_not_stop_the_others(queue_env, caplog):
    queue = queue_env.make_queue()
    removed: list[str] = []

    def broken(job_id):
        raise RuntimeError("removal listener bug")

    queue.subscribe_removed(broken)
    queue.subscribe_removed(removed.append)
    job = queue.add(make_spec("fake://fail-network", queue_env.dest))
    await asyncio.wait_for(queue.wait_idle(), 15)
    with caplog.at_level(logging.ERROR, logger="snag.jobs.queue"):
        await queue.clear(job.id)
    assert removed == [job.id]
    assert "removal listener bug" in caplog.text


async def test_clearing_deletes_the_row_before_the_folder(queue_env, monkeypatch):
    queue = queue_env.make_queue()
    removed: list[str] = []
    queue.subscribe_removed(removed.append)
    job = queue.add(make_spec("fake://fail-network", queue_env.dest))
    await asyncio.wait_for(queue.wait_idle(), 15)
    started, release = block_folder_removal(monkeypatch)
    clearing = asyncio.create_task(queue.clear(job.id))
    try:
        await wait_for(started.is_set)
        assert queue.get(job.id) is None and queue_env.store.load(job.id) is None
        assert removed == [job.id]
        clearing.cancel()  # e.g. the app closing: the job is gone, its folder is swept at the next start
        with pytest.raises(asyncio.CancelledError):
            await clearing
    finally:
        release.set()
    assert queue.get(job.id) is None and queue_env.store.load(job.id) is None
    assert removed == [job.id]
    await asyncio.wait_for(queue.wait_idle(), 5)


async def test_a_clear_whose_row_cannot_be_deleted_keeps_the_job(queue_env):
    class UndeletableStore(JobStore):
        def delete(self, job_id):
            raise DISK_FULL

    queue = queue_env.make_queue(job_store=UndeletableStore(queue_env.conn))
    removed: list[str] = []
    queue.subscribe_removed(removed.append)
    job = queue.add(make_spec("fake://fail-network", queue_env.dest))
    await asyncio.wait_for(queue.wait_idle(), 15)
    with pytest.raises(type(DISK_FULL)):
        await queue.clear(job.id)
    assert queue.get(job.id) is job and job.state is JobState.FAILED
    assert queue_env.store.load(job.id).state is JobState.FAILED
    assert queue_env.work_of(job).exists() and removed == []
    queue.retry(job.id)  # still an ordinary failed job
    await asyncio.wait_for(queue.wait_idle(), 15)


# --- listeners ---

async def test_every_listener_sees_a_failure_before_another_listener_retries_it(queue_env):
    queue = queue_env.make_queue()
    retried = []

    def retry_once(job):
        if job.state is JobState.FAILED and not retried:
            retried.append(job.id)
            queue.retry(job.id)

    before, after = Recorder(), Recorder()
    queue.subscribe(before)
    queue.subscribe(retry_once)
    queue.subscribe(after)
    job = queue.add(make_spec("fake://fail-network", queue_env.dest))
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert job.state is JobState.FAILED
    assert after.events == before.events
    states = after.states(job.id)
    assert states.count(JobState.FAILED) == 2
    assert states.index(JobState.FAILED) < states.index(JobState.WAITING, 1)


async def test_listeners_get_the_job_as_it_was_at_each_change(queue_env):
    queue = queue_env.make_queue()
    seen = []
    queue.subscribe(seen.append)
    job = queue.add(make_spec("fake://success", queue_env.dest))
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert seen[0].state is JobState.WAITING and seen[-1].state is JobState.DONE
    assert all(snapshot.id == job.id for snapshot in seen)


# --- saving is best effort ---

async def test_a_download_finishes_even_when_nothing_can_be_saved(queue_env, caplog):
    store = FailingJobStore(queue_env.conn)
    queue = queue_env.make_queue(job_store=store)
    recorder = Recorder()
    queue.subscribe(recorder)
    with caplog.at_level(logging.ERROR, logger="snag.jobs.queue"):
        job = queue.add(make_spec("fake://success", queue_env.dest))  # queued and run although unsaved
        await asyncio.wait_for(queue.wait_idle(), 15)
    assert job.state is JobState.DONE and job.file_path.exists()
    states = recorder.states(job.id)
    assert states[0] is JobState.WAITING and JobState.DOWNLOADING in states and states[-1] is JobState.DONE
    assert [e.status for e in queue_env.history.list()] == ["done"]
    assert store.failures > 0 and queue_env.store.load(job.id) is None
    assert "database or disk is full" in caplog.text


async def test_a_failed_save_is_tried_again_at_the_next_update(queue_env):
    store = FailingJobStore(queue_env.conn)
    queue = queue_env.make_queue(job_store=store)
    job = queue.add(make_spec("fake://slow", queue_env.dest))
    await wait_for(lambda: job.state is JobState.DOWNLOADING)
    store.failing = False  # the disk has room again: the next progress update saves the job
    await wait_for(lambda: queue_env.store.load(job.id) is not None, timeout=5)
    assert queue_env.store.load(job.id).state is JobState.DOWNLOADING
    await queue.cancel(job.id)
    await asyncio.wait_for(queue.wait_idle(), 15)


async def test_a_done_download_stays_done_when_history_cannot_be_written(queue_env, caplog):
    queue = queue_env.make_queue(history=FailingHistory(queue_env.conn))
    recorder = Recorder()
    queue.subscribe(recorder)
    with caplog.at_level(logging.ERROR, logger="snag.jobs.queue"):
        job = queue.add(make_spec("fake://success", queue_env.dest))
        await asyncio.wait_for(queue.wait_idle(), 15)
    assert job.state is JobState.DONE and recorder.states(job.id)[-1] is JobState.DONE
    assert queue_env.store.load(job.id).state is JobState.DONE
    assert "History" in caplog.text


async def test_a_failure_is_still_reported_when_nothing_can_be_saved(queue_env):
    queue = queue_env.make_queue(job_store=FailingJobStore(queue_env.conn), history=FailingHistory(queue_env.conn))
    recorder = Recorder()
    queue.subscribe(recorder)
    job = queue.add(make_spec("fake://fail-network", queue_env.dest))
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert job.state is JobState.FAILED and job.error.kind is ErrorKind.NETWORK
    assert recorder.states(job.id)[-1] is JobState.FAILED


async def test_recoveries_work_when_nothing_can_be_saved(queue_env):
    import uuid

    queue = queue_env.make_queue(
        job_store=FailingJobStore(queue_env.conn), cookies=lambda url: COOKIES,
        throttle_pause=0.2, throttle_reduced=0.2,
    )
    signin = queue.add(make_spec("fake://signin", queue_env.dest))
    throttled = queue.add(make_spec(f"fake://throttle-once/{uuid.uuid4().hex}", queue_env.dest))
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert signin.state is JobState.DONE and throttled.state is JobState.DONE


async def test_a_done_job_whose_save_failed_is_saved_at_shutdown(queue_env):
    queue = queue_env.make_queue(job_store=FailOnceJobStore(queue_env.conn, JobState.DONE))
    job = queue.add(make_spec("fake://success", queue_env.dest))
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert job.state is JobState.DONE
    assert queue_env.store.load(job.id).state is not JobState.DONE  # the row still says it's going
    await asyncio.wait_for(queue.shutdown(), 15)
    assert queue_env.store.load(job.id).state is JobState.DONE
    restarted = queue_env.make_queue()
    await restarted.start()
    assert restarted.jobs == []  # not downloaded again


async def test_a_cancelled_job_whose_save_failed_is_not_resumed_at_the_next_start(queue_env):
    queue = queue_env.make_queue(job_store=FailOnceJobStore(queue_env.conn, JobState.CANCELLED))
    job = queue.add(make_spec("fake://slow", queue_env.dest))
    await wait_for(lambda: job.state is JobState.DOWNLOADING)
    await queue.cancel(job.id)
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert job.state is JobState.CANCELLED
    assert queue_env.store.load(job.id).state is not JobState.CANCELLED
    await asyncio.wait_for(queue.shutdown(), 15)
    restarted = queue_env.make_queue()
    await restarted.start()
    assert restarted.jobs == [] and queue_env.store.load(job.id) is None


async def test_clear_finished_saves_a_job_whose_save_failed_before_dropping_it(queue_env):
    queue = queue_env.make_queue(job_store=FailOnceJobStore(queue_env.conn, JobState.DONE))
    job = queue.add(make_spec("fake://success", queue_env.dest))
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert queue_env.store.load(job.id).state is not JobState.DONE
    queue.clear_finished()
    assert queue.get(job.id) is None
    assert queue_env.store.load(job.id).state is JobState.DONE


async def test_the_next_save_that_works_also_saves_jobs_whose_save_failed(queue_env):
    queue = queue_env.make_queue(job_store=FailOnceJobStore(queue_env.conn, JobState.DONE))
    first = queue.add(make_spec("fake://success", queue_env.dest))
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert queue_env.store.load(first.id).state is not JobState.DONE
    queue.pause()
    queue.add(make_spec("fake://success", queue_env.dest))  # saved as waiting
    assert queue_env.store.load(first.id).state is JobState.DONE


async def test_shutdown_interrupts_when_nothing_can_be_saved(queue_env):
    queue = queue_env.make_queue(job_store=FailingJobStore(queue_env.conn))
    recorder = Recorder()
    queue.subscribe(recorder)
    job = queue.add(make_spec("fake://slow", queue_env.dest))
    await wait_for(lambda: job.state is JobState.DOWNLOADING)
    await asyncio.wait_for(queue.shutdown(), 15)
    assert job.state is JobState.INTERRUPTED and recorder.states(job.id)[-1] is JobState.INTERRUPTED


async def test_shutdown_says_how_many_jobs_could_not_be_saved(queue_env, caplog):
    queue = queue_env.make_queue(job_store=FailingJobStore(queue_env.conn))
    queue.pause()
    queue.add(make_spec("fake://success", queue_env.dest))
    queue.add(make_spec("fake://success", queue_env.dest))
    with caplog.at_level(logging.WARNING, logger="snag.jobs.queue"):
        await asyncio.wait_for(queue.shutdown(), 15)
    assert "Couldn't save 2 job(s) before shutting down" in caplog.text
