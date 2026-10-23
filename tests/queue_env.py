"""Shared setup for the JobQueue tests (see the queue_env fixture in conftest.py)."""
from __future__ import annotations

import asyncio
import sqlite3
import threading
from pathlib import Path

import pytest

import snag.jobs.queue as queue_module
import snag.jobs.runner as runner_module
from snag.info.formats import build_format_lists
from snag.info.models import VideoInfo
from snag.jobs.models import ChoiceKind, FormatChoice, Job, JobState
from snag.jobs.queue import JobQueue
from snag.jobs.workdir import work_dir_path
from snag.paths import AppPaths, Tools
from snag.store.db import open_db
from snag.store.history import HistoryStore
from snag.store.jobs import JobStore

GONE = FormatChoice(ChoiceKind.VIDEO, "gone", "140", 1080, "mp4", "avc1.640028")
M4A = {"format_id": "140", "ext": "m4a", "protocol": "https", "vcodec": "none", "acodec": "mp4a.40.2", "abr": 129.5, "filesize": 1}


def video_info_with(formats):
    return VideoInfo(
        site="youtube", video_id="abc123", url="fake://format-gone", title="Fake Video", channel=None,
        duration=10, thumbnail=None, upload_date=None, formats=build_format_lists(formats, 10),
        subtitle_langs=(), auto_subtitle_langs=(), has_chapters=False,
    )


def video(format_id, height, vcodec, ext="mp4"):
    return {"format_id": format_id, "ext": ext, "protocol": "https", "vcodec": vcodec, "acodec": "none",
            "height": height, "filesize": 1}


COOKIES = "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tsecret\n"


async def no_lookup(url: str):
    raise AssertionError("lookup should not be called")


def record_spawns(monkeypatch: pytest.MonkeyPatch) -> list[asyncio.subprocess.Process]:
    procs: list[asyncio.subprocess.Process] = []
    real_spawn = runner_module.spawn

    async def spawn(args, cwd=None, **kwargs):
        proc = await real_spawn(args, cwd=cwd, **kwargs)
        procs.append(proc)
        return proc

    monkeypatch.setattr(runner_module, "spawn", spawn)
    return procs


def block_folder_removal(monkeypatch: pytest.MonkeyPatch) -> tuple[threading.Event, threading.Event]:
    """Make the queue's working-folder deletions wait: returns (started, release). Always set release."""
    started, release = threading.Event(), threading.Event()
    real_remove = queue_module.remove_work_dir

    def slow_remove(work):
        started.set()
        release.wait(15)
        return real_remove(work)

    monkeypatch.setattr(queue_module, "remove_work_dir", slow_remove)
    return started, release


def block_sweep(monkeypatch: pytest.MonkeyPatch) -> tuple[threading.Event, threading.Event]:
    """Make start()'s orphan sweep wait: returns (sweeping, release). Always set release."""
    sweeping, release = threading.Event(), threading.Event()
    real_sweep = queue_module.sweep_orphans

    def slow_sweep(roots, keep_ids):
        sweeping.set()
        release.wait(15)
        return real_sweep(roots, keep_ids)

    monkeypatch.setattr(queue_module, "sweep_orphans", slow_sweep)
    return sweeping, release


DISK_FULL = sqlite3.OperationalError("database or disk is full")


class FailingJobStore(JobStore):
    """A JobStore whose saves fail, as on a full disk, while `failing` is set."""

    def __init__(self, conn, *, failing: bool = True):
        super().__init__(conn)
        self.failing = failing
        self.failures = 0

    def save(self, job: Job) -> None:
        if self.failing:
            self.failures += 1
            raise DISK_FULL
        super().save(job)


class FailOnceJobStore(JobStore):
    """A JobStore whose first save of a job in `state` fails, as on a disk that was briefly full."""

    def __init__(self, conn, state: JobState):
        super().__init__(conn)
        self.state = state
        self.failed: set[str] = set()

    def save(self, job: Job) -> None:
        if job.state is self.state and job.id not in self.failed:
            self.failed.add(job.id)
            raise sqlite3.OperationalError("database or disk is full")
        super().save(job)


class FailingHistory(HistoryStore):
    """A HistoryStore that can't add anything, as on a full disk."""

    def add(self, job: Job) -> int:
        raise DISK_FULL


class QueueEnv:
    def __init__(self, tmp_path: Path, tools: Tools):
        self.tmp_path = tmp_path
        self.tools = tools
        self.paths = AppPaths(tmp_path / "Snag")
        self.dest = tmp_path / "out"
        self.conn = open_db(self.paths.db_file)
        self.queues: list[JobQueue] = []

    def make_queue(self, **overrides) -> JobQueue:
        params = dict(
            tools=self.tools, paths=self.paths, job_store=JobStore(self.conn), history=HistoryStore(self.conn),
            lookup=no_lookup, cookies=lambda url: None,
        )
        params.update(overrides)
        queue = JobQueue(**params)
        self.queues.append(queue)
        return queue

    @property
    def store(self) -> JobStore:
        return JobStore(self.conn)

    @property
    def history(self) -> HistoryStore:
        return HistoryStore(self.conn)

    def work_of(self, job: Job) -> Path:
        return work_dir_path(job.spec.dest_dir, job.id, self.paths.work_dir)

    async def close(self) -> None:
        try:
            for queue in self.queues:
                await asyncio.wait_for(queue.shutdown(), 30)
        finally:
            self.conn.close()
