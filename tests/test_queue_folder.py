"""JobQueue.retry(job_id, dest_dir): retrying a download in another folder, taking its partial along."""
import asyncio
import errno
import logging
import threading
from dataclasses import replace
from pathlib import Path

import pytest

import snag.jobs.queue as queue_module
import snag.jobs.workdir as workdir
from snag.errors import ErrorKind
from snag.jobs.models import JobState
from snag.jobs.workdir import TMP_FOLDER
from tests.helpers import make_spec, wait_for

PARTIAL = "Fake Video.fv.mp4.part"


def other_drive(monkeypatch, root: Path) -> Path:
    """Make everything under root count as another drive than Snag's own work folder, so a destination
    there gets its working folders in <destination>\\.snag-tmp."""
    root.mkdir(parents=True, exist_ok=True)
    real = workdir._drive
    resolved_root = root.resolve()

    def drive(path: Path) -> str:
        return "other:" if path.resolve().is_relative_to(resolved_root) else real(path)

    monkeypatch.setattr(workdir, "_drive", drive)
    return root


async def failed_with_partial(env, queue, dest: Path, **spec_overrides):
    """A job that failed with a network error and kept 100 bytes; its next attempt would work."""
    job = queue.add(make_spec("fake://fail-network", dest, **spec_overrides))
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert job.state is JobState.FAILED and job.error.kind is ErrorKind.NETWORK
    (env.work_of(job) / PARTIAL).write_bytes(b"x" * 100)
    job.spec = replace(job.spec, url="fake://success")
    return job


def watch_partial(env, queue, job) -> list[bool]:
    """Whether the partial is in the job's (current) working folder when each attempt starts."""
    seen: list[bool] = []

    def listener(update):
        if update.id == job.id and update.state is JobState.FETCHING:
            seen.append((env.work_of(queue.get(job.id)) / PARTIAL).exists())

    queue.subscribe(listener)
    return seen


async def test_retry_in_another_folder_on_the_same_drive(queue_env):
    queue = queue_env.make_queue()
    job = await failed_with_partial(queue_env, queue, queue_env.dest)
    old_work = queue_env.work_of(job)
    seen = watch_partial(queue_env, queue, job)
    new_dest = queue_env.tmp_path / "elsewhere"
    assert queue.retry(job.id, dest_dir=new_dest) is job
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert job.state is JobState.DONE
    assert job.spec.dest_dir == new_dest and job.file_path.parent == new_dest
    assert queue_env.work_of(job) == old_work  # both in Snag's own work folder: nothing to move
    assert seen == [True]
    assert queue_env.store.load(job.id).spec.dest_dir == new_dest


async def test_the_partial_moves_to_the_new_folders_drive(queue_env, monkeypatch):
    other = other_drive(monkeypatch, queue_env.tmp_path / "other-drive")
    queue = queue_env.make_queue()
    job = await failed_with_partial(queue_env, queue, queue_env.dest)
    old_work = queue_env.work_of(job)
    seen = watch_partial(queue_env, queue, job)
    new_dest = other / "Videos"
    queue.retry(job.id, dest_dir=new_dest)
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert job.state is JobState.DONE and job.file_path.parent == new_dest
    assert seen == [True]  # resumed from the moved partial
    assert queue_env.work_of(job).parent == new_dest / TMP_FOLDER
    assert not old_work.exists()


async def test_the_partial_is_copied_when_it_cannot_be_renamed(queue_env, monkeypatch):
    other = other_drive(monkeypatch, queue_env.tmp_path / "other-drive")
    queue = queue_env.make_queue()
    job = await failed_with_partial(queue_env, queue, queue_env.dest)
    old_work = queue_env.work_of(job)
    real_rename = queue_module._rename_dir

    def rename(source, target):
        if source == old_work:  # really on another drive
            raise OSError(errno.EXDEV, "cross-device link")
        real_rename(source, target)

    monkeypatch.setattr(queue_module, "_rename_dir", rename)
    seen = watch_partial(queue_env, queue, job)
    queue.retry(job.id, dest_dir=other / "Videos")
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert job.state is JobState.DONE and seen == [True]
    assert not old_work.exists()
    assert not [p for p in (other / "Videos" / TMP_FOLDER).glob("*") if p.name != job.id]  # no staging left


async def test_a_partial_that_cannot_be_moved_is_dropped_and_the_job_starts_over(queue_env, monkeypatch, caplog):
    other = other_drive(monkeypatch, queue_env.tmp_path / "other-drive")
    queue = queue_env.make_queue()
    job = await failed_with_partial(queue_env, queue, queue_env.dest)
    old_work = queue_env.work_of(job)

    def no_rename(source, target):
        raise OSError(errno.EXDEV, "cross-device link")

    def no_copy(source, target):
        target.mkdir(parents=True)
        (target / "half").write_bytes(b"x")
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(queue_module, "_rename_dir", no_rename)
    monkeypatch.setattr(queue_module, "_copy_dir", no_copy)
    seen = watch_partial(queue_env, queue, job)
    with caplog.at_level(logging.WARNING, logger="snag.jobs.queue"):
        queue.retry(job.id, dest_dir=other / "Videos")
        await asyncio.wait_for(queue.wait_idle(), 15)
    assert job.state is JobState.DONE and job.file_path.parent == other / "Videos"
    assert seen == [False]  # started over
    assert not old_work.exists()
    assert "Couldn't move" in caplog.text and job.id in caplog.text
    assert not [p for p in (other / "Videos" / TMP_FOLDER).glob("*") if p.name != job.id]


async def test_an_unusable_new_folder_fails_the_retry_and_keeps_the_partial(queue_env):
    queue = queue_env.make_queue()
    job = await failed_with_partial(queue_env, queue, queue_env.dest)
    old_dest, old_work = job.spec.dest_dir, queue_env.work_of(job)
    blocker = queue_env.tmp_path / "a-file"
    blocker.write_text("not a folder")
    queue.retry(job.id, dest_dir=blocker)
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert job.state is JobState.FAILED and job.error.kind is ErrorKind.FOLDER
    assert job.error.message == f"Can't save to {blocker}"
    assert job.spec.dest_dir == old_dest  # still where its partial is
    assert (old_work / PARTIAL).read_bytes() == b"x" * 100
    seen = watch_partial(queue_env, queue, job)
    queue.retry(job.id, dest_dir=queue_env.tmp_path / "good")
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert job.state is JobState.DONE and seen == [True]


async def test_an_unusable_new_folder_on_another_drive_keeps_the_partial(queue_env, monkeypatch):
    other = other_drive(monkeypatch, queue_env.tmp_path / "other-drive")
    queue = queue_env.make_queue()
    job = await failed_with_partial(queue_env, queue, queue_env.dest)
    old_work = queue_env.work_of(job)
    blocker = other / "a-file"
    blocker.write_text("not a folder")
    queue.retry(job.id, dest_dir=blocker / "Videos")
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert job.state is JobState.FAILED and job.error.kind is ErrorKind.FOLDER
    assert (old_work / PARTIAL).exists()


async def test_a_playlist_job_keeps_its_playlist_folder(queue_env):
    queue = queue_env.make_queue()
    playlist_folder = queue_env.dest / "My List"
    job = await failed_with_partial(queue_env, queue, playlist_folder, playlist_index=2, playlist_count=3)
    queue.retry(job.id, dest_dir=queue_env.tmp_path / "Elsewhere")
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert job.spec.dest_dir == queue_env.tmp_path / "Elsewhere" / "My List"
    assert job.file_path == queue_env.tmp_path / "Elsewhere" / "My List" / "02 - Fake Video.mp4"


async def test_a_playlist_job_moved_to_a_folder_of_the_same_name_is_not_nested(queue_env):
    queue = queue_env.make_queue()
    job = await failed_with_partial(queue_env, queue, queue_env.dest / "My List", playlist_index=1, playlist_count=1)
    queue.retry(job.id, dest_dir=queue_env.tmp_path / "Elsewhere" / "my list")
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert job.spec.dest_dir == queue_env.tmp_path / "Elsewhere" / "my list"


async def test_retry_in_the_same_folder_is_a_plain_retry(queue_env):
    queue = queue_env.make_queue()
    job = await failed_with_partial(queue_env, queue, queue_env.dest)
    with pytest.raises(ValueError):
        queue.retry(job.id, dest_dir=Path("relative/folder"))
    assert job.state is JobState.FAILED
    queue.retry(job.id, dest_dir=queue_env.dest)
    assert job.state is JobState.WAITING and job.stage is None
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert job.state is JobState.DONE


def block_moves(monkeypatch) -> tuple[threading.Event, threading.Event]:
    """Make the queue's partial moves wait: returns (moving, release). Always set release."""
    moving, release = threading.Event(), threading.Event()
    real_move = queue_module._move_partial

    def slow_move(*args):
        moving.set()
        release.wait(15)
        return real_move(*args)

    monkeypatch.setattr(queue_module, "_move_partial", slow_move)
    return moving, release


async def test_a_job_waits_while_its_partial_moves(queue_env, monkeypatch):
    other = other_drive(monkeypatch, queue_env.tmp_path / "other-drive")
    queue = queue_env.make_queue()
    job = await failed_with_partial(queue_env, queue, queue_env.dest)
    moving, release = block_moves(monkeypatch)
    try:
        queue.retry(job.id, dest_dir=other / "Videos")
        await wait_for(moving.is_set)
        assert job.state is JobState.WAITING and job.error is None
        assert job.stage == "Moving to the new folder…"
        assert queue.state.waiting == 1
        with pytest.raises(ValueError):
            queue.retry(job.id)
        with pytest.raises(ValueError):
            await queue.clear(job.id)
    finally:
        release.set()
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert job.state is JobState.DONE


async def test_cancel_while_the_partial_moves(queue_env, monkeypatch):
    other = other_drive(monkeypatch, queue_env.tmp_path / "other-drive")
    queue = queue_env.make_queue()
    job = await failed_with_partial(queue_env, queue, queue_env.dest)
    old_work = queue_env.work_of(job)
    moving, release = block_moves(monkeypatch)
    try:
        queue.retry(job.id, dest_dir=other / "Videos")
        await wait_for(moving.is_set)
        cancelling = asyncio.create_task(queue.cancel(job.id))
        await asyncio.sleep(0.1)
        assert not cancelling.done()  # waits for the move, which can't be interrupted
    finally:
        release.set()
    await asyncio.wait_for(cancelling, 15)
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert job.state is JobState.CANCELLED
    assert not old_work.exists() and not queue_env.work_of(job).exists()
    assert queue_env.store.load(job.id).state is JobState.CANCELLED


async def test_shutdown_waits_for_a_move_and_the_next_start_resumes_in_the_new_folder(queue_env, monkeypatch):
    other = other_drive(monkeypatch, queue_env.tmp_path / "other-drive")
    queue = queue_env.make_queue()
    job = await failed_with_partial(queue_env, queue, queue_env.dest)
    moving, release = block_moves(monkeypatch)
    queue.retry(job.id, dest_dir=other / "Videos")
    await wait_for(moving.is_set)
    stopping = asyncio.create_task(queue.shutdown())
    await asyncio.sleep(0.1)
    assert not stopping.done()
    release.set()
    await asyncio.wait_for(stopping, 15)
    stored = queue_env.store.load(job.id)
    assert stored.state is JobState.WAITING and stored.spec.dest_dir == other / "Videos"
    again = queue_env.make_queue()
    seen = watch_partial(queue_env, again, job)
    await again.start()
    await asyncio.wait_for(again.wait_idle(), 15)
    assert again.get(job.id).state is JobState.DONE and seen == [True]
