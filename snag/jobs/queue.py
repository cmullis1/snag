"""The download queue: runs up to N jobs at once, recovers from subtitle, sign-in, stale-format,
stale-link (403) and throttling errors, saves every state change, and restores the queue when the app starts.

Failed jobs stay in the queue, across restarts too, until they are retried or cleared. Done and
cancelled ones stay in memory until cleared and are dropped from the database at the next start
(History keeps their record).

Saving is best effort: if the database can't be written (say the disk is full) the failure is logged
and the job carries on exactly as it would have. An unsaved job is saved again by its next change, by
any later save that works, and at the latest by shutdown() (or clear_finished(), before dropping it),
so a download that finished while the disk was full isn't restored as unfinished, and downloaded
again, at the next start. Only a job's restore after a restart depends on its row.
"""
from __future__ import annotations

import asyncio
import copy
import errno
import logging
import math
import os
import shutil
import sys
import threading
import time
import traceback
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path, PureWindowsPath
from typing import Any
from urllib.parse import urlsplit

from snag.errors import ErrorInfo, ErrorKind, make_error
from snag.paths import AppPaths, Tools
from snag.store.history import HistoryStore
from snag.store.jobs import JobStore
from snag.store.settings import MAX_CONCURRENT_RANGE

from .models import END_STATES, Job, JobSpec, JobState, new_id
from .recovery import CookieProvider, JobRun, LookupFn
from .runner import AttemptResult, JobRunner, Outcome, folder_error
from .workdir import (
    TMP_FOLDER, clear_cookies_dir, make_work_dir, remove_work_dir, sweep_orphans, work_dir_path,
)

log = logging.getLogger(__name__)

Listener = Callable[[Job], None]
RemovedListener = Callable[[str], None]
StateListener = Callable[["QueueState"], None]

DISK_MARGIN = 1.1  # free space needed, relative to what is left to download
SWEEP_TIMEOUT = 30.0  # seconds a start-up sweep of leftover working folders is waited for
MOVING_STAGE = "Moving to the new folder…"  # a retried job's stage while its partial moves
_ERROR_NOT_SAME_DEVICE = 17  # Windows: the rename would cross drives


@dataclass(frozen=True)
class QueueState:
    """The queue as a whole, for the UI (see JobQueue.subscribe_state)."""

    paused: bool  # by the user: pause() / resume()
    held: bool  # by the updater: hold() / release()
    throttled: bool
    # While throttled: when downloads may start again, in wall-clock seconds (time.time()), so the UI can
    # show a countdown or a time of day. None when not throttled.
    throttled_until: float | None
    current_limit: int  # how many jobs may run at once now: 1 while recovering from throttling
    running: int
    waiting: int


@dataclass(frozen=True)
class BatchSummary:
    total: int
    done: int
    failed: int
    cancelled: int
    pending: int


class JobQueue:
    """Runs on one event loop; every method must be called from it.

    Stop jobs with cancel() and the whole queue with shutdown(). Cancelling a job's task instead (only
    the event loop ending without shutdown() should) closes the queue: it starts nothing more, as a new
    task would never run to its end, and the job is left to resume at the next start."""

    def __init__(
        self,
        *,
        tools: Tools,
        paths: AppPaths,
        job_store: JobStore,
        history: HistoryStore,
        lookup: LookupFn,
        cookies: CookieProvider,
        max_concurrent: int = 5,
        throttle_pause: float = 300.0,
        throttle_reduced: float = 1800.0,
        clock: Callable[[], float] = time.monotonic,
        is_local: Callable[[Path], bool] | None = None,
        sweep_timeout: float = SWEEP_TIMEOUT,
    ):
        """throttle_pause: seconds every download waits after a throttling error; throttle_reduced: seconds
        after that pause during which only one job runs at a time.

        is_local: whether a folder is on a local drive, which start() sweeps before it returns (by default
        local_drive); the others, network shares above all, are swept in the background. sweep_timeout:
        seconds each of those sweeps is waited for (see start()).

        clock: the time those deadlines are measured in. It must track the event loop's loop.time() (by
        default both are time.monotonic): the queue measures the deadlines with clock but wakes itself
        for them with the loop's timers, so a clock that runs apart from loop.time() would end the pause
        and the reduced period at the wrong moment.
        """
        _check_limit(max_concurrent)
        self._runner = JobRunner(tools, paths, self._on_runner_update)
        self._paths = paths
        self._job_store = job_store
        self._history = history
        self._lookup = lookup
        self._cookies = cookies
        self._max = max_concurrent
        self._throttle_pause = throttle_pause
        self._throttle_reduced = throttle_reduced
        self._clock = clock
        self._is_local = is_local or local_drive
        self._sweep_timeout = sweep_timeout
        self._sweeper: asyncio.Task[None] | None = None  # start()'s background sweep
        self._jobs: dict[str, Job] = {}
        self._waiting: deque[str] = deque()
        self._running: dict[str, asyncio.Task[None]] = {}
        self._stop_events: dict[str, asyncio.Event] = {}
        self._cancel_requested: set[str] = set()
        self._cancelling: set[str] = set()  # waiting jobs whose working folder cancel() is deleting
        self._clearing: set[str] = set()  # cleared jobs whose working folder clear() is deleting
        self._moving: dict[str, asyncio.Task[None]] = {}  # jobs retried in another folder, being moved
        self._no_subtitles: set[str] = set()  # throttled after dropping subtitles; the requeued run skips them
        self._saved_state: dict[str, JobState] = {}  # the state each job's row was last saved with
        self._unsaved: set[str] = set()  # jobs whose last save failed (logged once until one works)
        self._listeners: list[Listener] = []
        self._removed_listeners: list[RemovedListener] = []
        self._state_listeners: list[StateListener] = []
        # (listeners, what they are told): a job's copy, a removed job's id, or a QueueState
        self._notifications: deque[tuple[list, Job | str | QueueState]] = deque()
        self._notifying = False
        # Set whenever something wait_idle() or shutdown() waits for may have changed.
        self._activity = asyncio.Event()
        self._paused = False
        self._holds = 0
        self._started = False
        self._starting = False
        self._closing = False
        self._throttled_until = float("-inf")
        self._throttled_until_wall = 0.0  # the same moment in wall-clock time, for QueueState
        self._reduced_until = float("-inf")
        self._wake: asyncio.TimerHandle | None = None
        self._state_timer: asyncio.TimerHandle | None = None  # reports the end of a throttle on time
        self._last_state = self.state  # the state the state listeners were last told about

    # ----- reading state -----

    def subscribe(self, listener: Listener) -> None:
        """Called after every change with a copy of the job as it was then (get() returns the live job).

        Notifications arrive one at a time and in order: a change a listener makes (retry() on a failed
        job, say) reaches every listener after the change being reported. A listener that raises is
        logged and skipped."""
        self._listeners.append(listener)

    def subscribe_removed(self, listener: RemovedListener) -> None:
        """Called with a job's id when clear() or clear_finished() removes it."""
        self._removed_listeners.append(listener)

    def subscribe_state(self, listener: StateListener) -> None:
        """Called with the new QueueState whenever any of its fields changes: pause and resume, hold
        and release, a throttle starting and ending, the limit changing (set_max_concurrent() or the
        end of the reduced period after a throttle), jobs being queued, starting and ending. Only
        changes are reported; `state` is the current one.

        Delivered in order with the job notifications (see subscribe()); a listener that raises is
        logged and skipped."""
        self._state_listeners.append(listener)

    def unsubscribe(self, listener: Callable[..., None]) -> None:
        """Stop calling a listener given to subscribe(), subscribe_removed() or subscribe_state()."""
        for listeners in (self._listeners, self._removed_listeners, self._state_listeners):
            while listener in listeners:
                listeners.remove(listener)

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    @property
    def jobs(self) -> list[Job]:
        return sorted(self._jobs.values(), key=lambda j: j.created_at)

    @property
    def current_limit(self) -> int:
        """1 while recovering from throttling, else the configured limit."""
        return 1 if self._clock() < self._reduced_until else self._max

    @property
    def throttled(self) -> bool:
        return self._clock() < self._throttled_until

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def held(self) -> bool:
        return self._holds > 0

    @property
    def state(self) -> QueueState:
        throttled = self.throttled
        return QueueState(
            paused=self._paused,
            held=self.held,
            throttled=throttled,
            throttled_until=self._throttled_until_wall if throttled else None,
            current_limit=self.current_limit,
            running=len(self._running),
            waiting=sum(1 for job_id in self._waiting if self._is_waiting(job_id)) + len(self._moving),
        )

    def _is_waiting(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        return job is not None and job.state is JobState.WAITING

    def batch_summary(self, batch_id: str) -> BatchSummary:
        """The batch's jobs in the queue, plus the ones it no longer holds as History records them: done
        or failed in an earlier session, or cleared. A job the queue holds is counted as it is now.

        A cleared failed job still counts as failed (History says so) although retry_failed() can no
        longer retry it; a cancelled job the queue no longer holds isn't counted (History doesn't record
        cancelled downloads), and neither is a job whose History entry was deleted."""
        states = [j.state for j in self._jobs.values() if j.spec.batch_id == batch_id]
        try:
            recorded = self._history.batch_statuses(batch_id)
        except Exception:  # the summary of the jobs in memory is still worth showing
            log.exception("Couldn't read batch %s from History", batch_id)
            recorded = {}
        states += [JobState(status) for job_id, status in recorded.items() if job_id not in self._jobs]
        return BatchSummary(
            total=len(states),
            done=states.count(JobState.DONE),
            failed=states.count(JobState.FAILED),
            cancelled=states.count(JobState.CANCELLED),
            pending=sum(1 for s in states if s not in END_STATES),
        )

    async def wait_idle(self) -> None:
        """Wait until start() is done and no job is waiting, running, being cancelled, cleared or moved.

        Mainly for tests: it never returns while the queue is paused or shut down with jobs waiting."""
        await self._wait_until(self._is_idle)

    def _is_idle(self) -> bool:
        return not (
            self._starting or self._waiting or self._running or self._cancelling or self._clearing or self._moving
        )

    async def wait_running_done(self) -> None:
        """Wait until no job is running, whatever is waiting and whether or not the queue is paused or
        held. With hold(), this is how the updater waits for yt-dlp to be free to replace."""
        await self._wait_until(lambda: not self._running)

    # ----- changing state -----

    @staticmethod
    def new_batch_id() -> str:
        return new_id()

    def add(self, spec: JobSpec) -> Job:
        """Queue a download. If it can't be saved (say the disk is full) it is still queued and runs; the
        failure is logged, and the job is saved again once the database works again."""
        job = Job(spec=spec)
        self._jobs[job.id] = job
        self._enqueue(job)
        return job

    def add_batch(self, specs: Iterable[JobSpec]) -> list[Job]:
        return [self.add(spec) for spec in specs]

    async def cancel(self, job_id: str) -> None:
        """Stop a job and delete its partial files. Raises KeyError for an unknown job; a finished job, or
        one already being cancelled, is left alone."""
        job = self._jobs[job_id]
        if job.state in END_STATES or job_id in self._cancelling:
            return
        if job_id in self._moving:  # retried in another folder: the move can't be interrupted
            self._cancel_requested.add(job_id)
            await asyncio.wait((self._moving[job_id],))  # unlike awaiting the task, this never cancels it
            return
        if job_id in self._running:
            self._cancel_requested.add(job_id)
            self._stop_events[job_id].set()
            await self._runner.cancel(job_id)
            return
        if job_id in self._waiting:
            self._waiting.remove(job_id)
        self._cancelling.add(job_id)
        try:
            await asyncio.to_thread(remove_work_dir, self._work_of(job))
        finally:
            # Out of the queue now, so cancelled even if this call is cut short (the folder is then
            # deleted in the background, or swept at the next start).
            self._cancelling.discard(job_id)
            self._finish(job, JobState.CANCELLED)
            self._pump()

    def retry(self, job_id: str, dest_dir: Path | None = None) -> Job:
        """Queue a failed or cancelled job again; a kept partial download is resumed. Raises KeyError for
        an unknown job and ValueError for one that isn't failed or cancelled, or that retrying can't help.

        dest_dir: save it in this folder instead ("Change folder"). A playlist job (one with a
        playlist_index) keeps its playlist subfolder: its download goes to dest_dir\\<that subfolder>,
        unless dest_dir already ends in a folder of that name (the playlist's folder itself was picked).

        A job changing folder shows WAITING with MOVING_STAGE while its partial is got ready for the new
        folder in a worker thread (see _move_partial: moved, or deleted if it can't be, so the job
        starts over), and is queued after that; its saved row is only changed then, so a stop meanwhile
        leaves it as it was. If the new folder can't be used the job fails again with a FOLDER error and
        keeps its old folder, and its partial. cancel() waits for a move under way."""
        job = self._jobs[job_id]
        if job.state not in (JobState.FAILED, JobState.CANCELLED):
            raise ValueError("only failed or cancelled downloads can be retried")
        if job.error is not None and not job.error.can_retry:
            raise ValueError("retrying won't help this download")
        if dest_dir is not None and not Path(dest_dir).is_absolute():
            raise ValueError("the new folder must be a full path like C:\\Users\\me\\Videos")
        new_dest = _retry_dest(job.spec, Path(dest_dir)) if dest_dir is not None else job.spec.dest_dir
        if _same_place(new_dest, job.spec.dest_dir):
            self._requeue(job)
            return job
        job.state, job.error, job.finished_at, job.file_path = JobState.WAITING, None, None, None
        job.percent, job.speed, job.eta, job.stage = None, None, None, MOVING_STAGE
        self._moving[job_id] = asyncio.create_task(self._change_folder(job, new_dest), name=f"snag-move-{job_id}")
        self._emit(job)
        self._report_state()
        return job

    async def _change_folder(self, job: Job, new_dest: Path) -> None:
        """Get the job's partial ready in new_dest (in a thread), then queue it there, or fail it with a
        FOLDER error if new_dest can't be used. A cancel() meanwhile cancels it once the move is done."""
        try:
            problem = await asyncio.to_thread(_move_partial, job.id, job.spec.dest_dir, new_dest, self._paths.work_dir)
            if problem is None:
                job.spec = replace(job.spec, dest_dir=new_dest)
            if job.id in self._cancel_requested:
                await asyncio.to_thread(remove_work_dir, self._work_of(job))
                self._finish(job, JobState.CANCELLED)
            elif problem is not None:
                self._finish(job, JobState.FAILED, error=folder_error(replace(job.spec, dest_dir=new_dest), problem))
            else:
                self._requeue(job)
        except asyncio.CancelledError:
            # As for a job's run (see _run_job): only the event loop ending cancels this, so start
            # nothing more. The job's row is as it was before the retry.
            self._close()
            raise
        except Exception:  # never leave the job stuck "moving"
            details = traceback.format_exc()
            log.error("Moving job %s to %s failed unexpectedly:\n%s", job.id, new_dest, details)
            self._finish(job, JobState.FAILED, error=make_error(ErrorKind.UNKNOWN, job.spec.site, details=details))
        finally:
            self._moving.pop(job.id, None)
            self._cancel_requested.discard(job.id)
            self._pump()

    def _requeue(self, job: Job, *, with_cookies: bool = False) -> None:
        """Queue a failed or cancelled job again as if new, keeping its working folder (and spec).
        with_cookies: its first attempt already uses the site's cookies."""
        job.error, job.finished_at, job.file_path = None, None, None
        job.refetched = job.subtitles_skipped = False
        job.used_cookies = with_cookies
        self._no_subtitles.discard(job.id)
        self._enqueue(job)

    def retry_failed(self, batch_id: str) -> list[Job]:
        """Retry every failed job of a batch that retrying could help."""
        failed = [
            j for j in self.jobs
            if j.spec.batch_id == batch_id and j.state is JobState.FAILED and (j.error is None or j.error.can_retry)
        ]
        return [self.retry(j.id) for j in failed]

    def retry_signin_failures(self, host: str | None = None) -> list[Job]:
        """Retry every failed job that needed signing in, now with cookies from its first attempt; for
        when fresh cookies arrive from the browser. Returns the jobs retried, oldest first.

        host: only jobs whose URL is on that host or below it ("youtube.com" covers www.youtube.com and
        music.youtube.com, not notyoutube.com), compared without regard to case; None retries them all.
        Only the job's URL is compared, so jobs should be queued with the site's page URL (VideoInfo.url,
        yt-dlp's webpage_url): a job queued with a youtu.be link isn't on youtube.com."""
        wanted = host.casefold().strip(".") if host else None
        retried = []
        for job in self.jobs:
            if job.state is not JobState.FAILED or job.error is None or job.error.kind is not ErrorKind.SIGN_IN:
                continue
            if wanted is not None and not _on_host(job.spec.url, wanted):
                continue
            self._requeue(job, with_cookies=True)
            retried.append(job)
        return retried

    async def clear(self, job_id: str) -> None:
        """Remove a done, failed or cancelled job for good: its saved row, then the job (telling the
        removal listeners), then, unless it is done, its working folder. A done job's downloaded file is
        never touched. Raises KeyError for an unknown job and ValueError for one still waiting or running.

        If the row can't be deleted that error is raised and the job stays as it was. Once the row is
        gone the job is gone, even if deleting its folder is cut short; a folder left behind is an
        orphan for the next start's sweep."""
        job = self._jobs[job_id]
        if job.state not in (JobState.DONE, JobState.FAILED, JobState.CANCELLED) or job_id in self._running:
            raise ValueError("only finished, failed or cancelled downloads can be cleared")
        self._job_store.delete(job_id)
        del self._jobs[job_id]
        self._saved_state.pop(job_id, None)
        self._unsaved.discard(job_id)
        if job.state is JobState.DONE:  # its working folder went when the file was moved out of it
            self._emit_removed(job_id)
            self._activity.set()
            return
        self._clearing.add(job_id)
        try:
            self._emit_removed(job_id)
            await asyncio.to_thread(remove_work_dir, self._work_of(job))
        finally:
            self._clearing.discard(job_id)
            self._activity.set()

    def clear_finished(self) -> None:
        """Remove done and cancelled jobs from the queue (their rows go at the next start).

        A job whose last save failed is saved once more first: a row left saying the job is still going
        would make the next start download it again. If that save fails too it is logged."""
        finished = [
            job for job in self.jobs
            if job.state in (JobState.DONE, JobState.CANCELLED) and job.id not in self._running
        ]
        self._save_unsaved(job.id for job in finished)
        for job in finished:
            if job.id in self._unsaved:
                log.warning("Job %s is cleared unsaved; the next start may restore it", job.id)
            del self._jobs[job.id]
            self._saved_state.pop(job.id, None)
            self._unsaved.discard(job.id)
            self._emit_removed(job.id)

    def set_max_concurrent(self, limit: int) -> None:
        _check_limit(limit)
        self._max = limit
        self._pump()

    def pause(self) -> None:
        """Start no new jobs; running ones carry on. For the user (the tray's pause and resume)."""
        self._paused = True
        self._report_state()

    def resume(self) -> None:
        self._paused = False
        self._pump()

    def hold(self) -> None:
        """Start no new jobs until release(); running ones carry on. For the updater, which holds the
        queue, waits with wait_running_done(), replaces yt-dlp, then releases it.

        Separate from pause() so neither undoes the other: release() never resumes a queue the user
        paused, and resume() never starts jobs while the updater holds the queue. Holds nest: each
        hold() needs its own release()."""
        self._holds += 1
        self._report_state()

    def release(self) -> None:
        """Undo one hold(). Raises RuntimeError when the queue isn't held."""
        if self._holds == 0:
            raise RuntimeError("release() without a matching hold()")
        self._holds -= 1
        self._pump()

    async def start(self, extra_roots: Iterable[Path] = (), deferred_roots: Iterable[Path] = ()) -> None:
        """Restore the queue when the app starts; call once, before any download begins.

        Unfinished jobs are marked interrupted and queued again (yt-dlp resumes their partial files),
        failed jobs come back as failed, done and cancelled rows are dropped. Leftover cookie files
        and working folders that belong to no job are deleted. Deleting leftovers is best effort: a
        failure is logged and the queue is restored all the same.

        Leftover working folders are looked for in Snag's own work folder and in the .snag-tmp of each
        saved job's folder, of extra_roots (e.g. the video and audio folders) and of deferred_roots
        (e.g. remembered destinations). Before start() returns it sweeps the work folder and the
        extra_roots and job folders on a local drive (is_local); jobs added meanwhile wait until that
        is done. The rest (network shares, every deferred root) are swept afterwards in the background,
        one at a time, so a slow or unreachable share never holds up the start. Each sweep is waited for
        sweep_timeout seconds; one that takes longer is logged and left to finish on its own thread.
        However late it runs, a sweep never deletes the working folder of a job the queue holds.
        """
        if self._started or self._running:
            raise RuntimeError("start() must run once, before any download starts")
        self._started = self._starting = True
        later: list[Path] = []
        try:
            try:
                if not await asyncio.to_thread(clear_cookies_dir, self._paths.cookies_dir):
                    log.warning("Couldn't delete every leftover cookies file in %s", self._paths.cookies_dir)
            except Exception:
                log.exception("Couldn't clear the cookies folder %s", self._paths.cookies_dir)
            pruned = self._job_store.prune_finished()
            unfinished = [j for j in self._job_store.load_unfinished() if j.id not in self._jobs]
            failed = [j for j in self._job_store.load_failed() if j.id not in self._jobs]
            self._job_store.prune_corrupt()  # after loading, which marks unreadable rows corrupt
            for job in failed:
                self._jobs[job.id] = job
                self._saved_state[job.id] = job.state
                self._emit(job)
            for job in unfinished:
                self._jobs[job.id] = job
                job.state, job.speed, job.eta, job.stage = JobState.INTERRUPTED, None, None, None
                job.refetched = False  # formats can change between sessions; each may replace one once
                self._persist(job)
                self._emit(job)
            now = [self._paths.work_dir]
            for root in dict.fromkeys([
                *(job.spec.dest_dir / TMP_FOLDER for job in (*pruned, *unfinished, *failed)),
                *(Path(root) / TMP_FOLDER for root in extra_roots),
            ]):
                if root not in now:
                    (now if self._is_local(root) else later).append(root)
            later += (Path(root) / TMP_FOLDER for root in deferred_roots)
            later = [root for root in dict.fromkeys(later) if root not in now]
            await self._sweep(now)
            for job in unfinished:
                if job.state is JobState.INTERRUPTED:  # not cancelled or cleared meanwhile
                    self._enqueue(job)
        finally:
            self._starting = False
            self._activity.set()
        self._pump()
        if later and not self._closing:
            self._sweeper = asyncio.create_task(self._sweep_each(later), name="snag-sweep")

    async def wait_swept(self) -> None:
        """Wait for start()'s background sweep, if any, to finish (or be given up on)."""
        if self._sweeper is not None:
            await asyncio.wait((self._sweeper,))

    async def _sweep(self, roots: list[Path]) -> None:
        """Delete the orphan working folders under roots, waiting sweep_timeout seconds at most."""
        try:
            keep = _KeepIds(self._jobs, self._job_store.all_ids())
            await asyncio.wait_for(_in_daemon_thread(sweep_orphans, roots, keep), self._sweep_timeout)
        except TimeoutError:
            log.warning("Sweeping leftover working folders in %s took over %.0f s; it carries on alone",
                        ", ".join(map(str, roots)), self._sweep_timeout)
        except Exception:
            log.exception("Couldn't sweep leftover working folders in %s", ", ".join(map(str, roots)))

    async def _sweep_each(self, roots: list[Path]) -> None:
        for root in roots:
            if self._closing:
                return
            await self._sweep([root])

    async def shutdown(self) -> None:
        """Stop running downloads but keep their partial files, so they resume at the next start.

        Returns once the running jobs have stopped, every cancel or clear under way has finished
        deleting its working folder, and every job changing folder has its partial moved (the move
        can't be interrupted). Waiting jobs stay waiting, for the next start. Last, every job
        whose last save failed is saved once more, so the next start restores it as it is now (a done
        download isn't downloaded again); if that fails too it is logged."""
        self._close()
        running = list(self._running.items())
        for job_id, _ in running:
            self._stop_events[job_id].set()
        await asyncio.gather(*(self._runner.cancel(job_id, keep_partial=True) for job_id, _ in running))
        await asyncio.gather(*(task for _, task in running), return_exceptions=True)
        await self._wait_until(lambda: not (self._cancelling or self._clearing or self._moving))
        self._save_unsaved()
        if self._unsaved:
            log.warning(
                "Couldn't save %d job(s) before shutting down; the next start restores them as last saved",
                len(self._unsaved),
            )

    def _close(self) -> None:
        """Start no more jobs or sweeps."""
        self._closing = True
        if self._sweeper is not None:
            self._sweeper.cancel()  # a sweep already on its thread ends there, keeping every job's folder
        for timer in (self._wake, self._state_timer):
            if timer is not None:
                timer.cancel()
        self._wake = self._state_timer = None

    async def _wait_until(self, condition: Callable[[], bool]) -> None:
        while not condition():
            self._activity.clear()
            await self._activity.wait()

    # ----- scheduling -----

    def _enqueue(self, job: Job) -> None:
        job.state, job.percent, job.speed, job.eta, job.stage = JobState.WAITING, None, None, None, None
        self._waiting.append(job.id)
        self._persist(job)
        self._emit(job)
        self._pump()

    def _pump(self) -> None:
        try:
            self._start_waiting()
        finally:
            self._activity.set()
            self._report_state()

    def _start_waiting(self) -> None:
        if self._paused or self._holds or self._closing or self._starting:
            return
        now = self._clock()
        if now < self._throttled_until:
            self._wake_in(self._throttled_until - now)
            return
        if now < self._reduced_until and self._max > 1 and self._waiting:
            self._wake_in(self._reduced_until - now)  # so the normal limit returns on time
        while len(self._running) < self.current_limit:
            # A job requeued by its own run (throttling, or a listener's retry) waits for that run to end.
            job_id = next((i for i in self._waiting if i not in self._running), None)
            if job_id is None:
                break
            self._waiting.remove(job_id)
            job = self._jobs.get(job_id)
            if job is None or job.state is not JobState.WAITING:
                continue
            self._stop_events[job_id] = asyncio.Event()
            self._running[job_id] = asyncio.create_task(self._run_job(job), name=f"snag-job-{job_id}")

    def _wake_in(self, delay: float) -> None:
        if self._wake is not None:
            self._wake.cancel()
        self._wake = asyncio.get_running_loop().call_later(max(delay, 0.0), self._pump)

    # ----- one job's run -----

    async def _run_job(self, job: Job) -> None:
        run = JobRun(
            job, self._runner, cookies=self._cookies, lookup=self._lookup, stop=self._stop_events[job.id],
            save=self._persist, without_subtitles=job.id in self._no_subtitles,
        )
        try:
            error = await asyncio.to_thread(check_destination, job, self._paths.work_dir)
            result = AttemptResult(Outcome.FAILED, error=error) if error is not None else await run.execute()
            await self._settle(job, run, result)
        except asyncio.CancelledError:
            # Only the event loop ending without shutdown() cancels a job's task: start nothing more
            # (a new task would never run to its end), and leave this job to resume at the next start,
            # unless the user cancelled it (its folder is then swept at the next start).
            self._close()
            if job.state not in END_STATES:
                try:
                    if job.id in self._cancel_requested:
                        self._finish(job, JobState.CANCELLED)
                    else:
                        self._interrupt(job)
                except Exception:
                    log.exception("Couldn't record how job %s was stopped", job.id)
            raise
        except Exception:  # one broken job must never stop the rest of the queue
            details = traceback.format_exc()
            log.error("Job %s failed unexpectedly:\n%s", job.id, details)
            try:
                self._finish(job, JobState.FAILED, error=make_error(ErrorKind.UNKNOWN, job.spec.site, details=details))
            except Exception:
                log.exception("Couldn't record the failure of job %s", job.id)
        finally:
            self._running.pop(job.id, None)
            self._stop_events.pop(job.id, None)
            self._cancel_requested.discard(job.id)
            self._pump()

    async def _settle(self, job: Job, run: JobRun, result: AttemptResult) -> None:
        throttled = (
            result.outcome is Outcome.FAILED and result.error is not None
            and result.error.kind is ErrorKind.THROTTLED and not result.subtitle_failure
        )
        if job.id in self._cancel_requested and result.outcome is not Outcome.DONE:
            await asyncio.to_thread(remove_work_dir, self._work_of(job))
            self._finish(job, JobState.CANCELLED)
        elif result.outcome is Outcome.DONE:
            job.subtitles_skipped = run.without_subtitles
            self._finish(job, JobState.DONE, file_path=result.file_path)
        elif result.outcome is Outcome.CANCELLED:
            self._finish(job, JobState.CANCELLED)
        elif result.outcome is Outcome.INTERRUPTED or (self._closing and (throttled or run.recovery_left)):
            self._interrupt(job)  # the next start resumes it, recoveries included
        elif throttled:
            if run.without_subtitles:
                self._no_subtitles.add(job.id)  # the requeued run starts without them
            self._throttle(job)
        else:
            if not result.error.keep_partial:
                await asyncio.to_thread(remove_work_dir, self._work_of(job))
            if job.id in self._cancel_requested:  # cancelled while the folder was being deleted
                self._finish(job, JobState.CANCELLED)
            else:
                self._finish(job, JobState.FAILED, error=result.error)

    def _throttle(self, job: Job) -> None:
        """Back to the front of the queue; pause, then run one at a time until it's been calm a while.
        The job's stage says how long the pause is."""
        self._throttled_until = self._clock() + self._throttle_pause
        self._reduced_until = self._throttled_until + self._throttle_reduced
        job.state, job.percent, job.speed, job.eta = JobState.WAITING, None, None, None
        job.stage = throttle_message(job.spec.site, self._throttle_pause)
        self._throttled_until_wall = time.time() + self._throttle_pause
        self._waiting.appendleft(job.id)
        self._persist(job)
        self._emit(job)
        self._report_state()
        self._watch_throttle()

    def _watch_throttle(self) -> None:
        """Report the state again when the pause and then the reduced period end, even if nothing else
        happens then (the queue may be paused, or have nothing waiting)."""
        if self._state_timer is not None:
            self._state_timer.cancel()
            self._state_timer = None
        if self._closing:
            return
        now = self._clock()
        ends = [end for end in (self._throttled_until, self._reduced_until) if end > now]
        if ends:
            self._state_timer = asyncio.get_running_loop().call_later(min(ends) - now, self._throttle_tick)

    def _throttle_tick(self) -> None:
        self._state_timer = None
        self._report_state()
        self._watch_throttle()  # a timer can fire a moment early; it then waits for the rest

    def _interrupt(self, job: Job) -> None:
        job.state, job.speed, job.eta, job.stage = JobState.INTERRUPTED, None, None, None
        self._persist(job)
        self._emit(job)

    def _finish(self, job: Job, state: JobState, *, file_path: Path | None = None, error: ErrorInfo | None = None) -> None:
        job.state, job.file_path, job.error = state, file_path, error
        job.finished_at = time.time()
        job.speed, job.eta, job.stage = None, None, None
        if state is JobState.DONE:
            job.percent = 100.0
        self._no_subtitles.discard(job.id)
        self._persist(job)
        if state in (JobState.DONE, JobState.FAILED):
            try:
                self._history.add(job)
            except Exception:  # losing the History line mustn't turn a finished download into a failure
                log.exception("Couldn't add job %s to History", job.id)
        self._emit(job)

    # ----- saving and telling listeners -----

    def _work_of(self, job: Job) -> Path:
        return work_dir_path(job.spec.dest_dir, job.id, self._paths.work_dir)

    def _on_runner_update(self, job: Job) -> None:
        """The runner's progress callback. It must never raise, or the runner would end the attempt:
        _persist and _notify handle their own failures."""
        if self._saved_state.get(job.id) is not job.state:
            self._persist(job)
        self._emit(job)

    def _persist(self, job: Job) -> None:
        """Save the job, best effort: a failure is logged (once until a save works again). A save that
        works also retries every job whose last save failed, so a job that finished while the disk was
        full doesn't keep a row saying it's still going."""
        if self._save(job) and self._unsaved:
            self._save_unsaved()

    def _save(self, job: Job) -> bool:
        try:
            self._job_store.save(job)
        except Exception:
            self._saved_state.pop(job.id, None)
            if job.id not in self._unsaved:
                self._unsaved.add(job.id)
                log.exception("Couldn't save job %s; it carries on unsaved", job.id)
            return False
        self._saved_state[job.id] = job.state
        if job.id in self._unsaved:
            self._unsaved.discard(job.id)
            log.info("Job %s is saved again", job.id)
        return True

    def _save_unsaved(self, job_ids: Iterable[str] | None = None) -> None:
        """Try again to save the jobs (by default every one) whose last save failed."""
        for job_id in list(self._unsaved if job_ids is None else job_ids):
            job = self._jobs.get(job_id)
            if job is not None and job_id in self._unsaved:
                self._save(job)

    def _emit(self, job: Job) -> None:
        self._notify(self._listeners, copy.copy(job))

    def _emit_removed(self, job_id: str) -> None:
        self._notify(self._removed_listeners, job_id)

    def _report_state(self) -> None:
        """Tell the state listeners if the queue's state changed since they were last told."""
        state = self.state
        if state != self._last_state:
            self._last_state = state
            self._notify(self._state_listeners, state)

    def _notify(self, listeners: list, subject: Job | str | QueueState) -> None:
        """Deliver notifications one at a time, in order. One raised while another is being delivered (a
        listener calling retry(), say) waits behind it, so every listener sees every change in order."""
        self._notifications.append((listeners, subject))
        if self._notifying:
            return
        self._notifying = True
        try:
            while self._notifications:
                listeners, subject = self._notifications.popleft()
                for listener in list(listeners):
                    try:
                        listener(subject)
                    except Exception:
                        if isinstance(subject, Job):
                            log.exception("A queue listener failed on job %s", subject.id)
                        elif isinstance(subject, QueueState):
                            log.exception("A queue state listener failed")
                        else:
                            log.exception("A queue listener failed on removed job %s", subject)
        finally:
            self._notifying = False


class _KeepIds:
    """The jobs whose working folders a sweep keeps: every saved job, and every job the queue holds at the
    moment the sweep looks (it runs on another thread while jobs come and go)."""

    def __init__(self, jobs: dict[str, Job], saved: set[str]):
        self._jobs = jobs
        self._saved = saved

    def __contains__(self, job_id: object) -> bool:
        return job_id in self._saved or job_id in self._jobs


_LOCAL_DRIVE_TYPES = {2, 3, 5, 6}  # GetDriveTypeW: removable, fixed, CD-ROM, RAM disk


def local_drive(folder: Path) -> bool:
    """Whether folder is on a local drive letter: False for a network share (a UNC path or a mapped
    drive), a drive letter that isn't there, or a path without a drive letter."""
    drive = PureWindowsPath(folder).drive
    if len(drive) != 2 or drive[1] != ":":
        return False
    if sys.platform != "win32":
        return True
    import ctypes

    return ctypes.windll.kernel32.GetDriveTypeW(f"{drive}\\") in _LOCAL_DRIVE_TYPES


async def _in_daemon_thread(work: Callable[..., Any], *args: Any) -> Any:
    """work(*args) on a daemon thread of its own, so a call stuck on an unreachable network share can be
    given up on: asyncio.to_thread's pool would hold up Snag's exit until the call returned."""
    loop = asyncio.get_running_loop()
    future: asyncio.Future[Any] = loop.create_future()

    def settle(result: Any, error: BaseException | None) -> None:
        if future.done():  # given up on
            return
        if error is not None:
            future.set_exception(error)
        else:
            future.set_result(result)

    def run() -> None:
        result, error = None, None
        try:
            result = work(*args)
        except BaseException as caught:  # handed to the coroutine waiting for it
            error = caught
        try:
            loop.call_soon_threadsafe(settle, result, error)
        except RuntimeError:  # the loop has closed: nobody is waiting
            pass

    threading.Thread(target=run, name="snag-sweep", daemon=True).start()
    return await future


def _on_host(url: str, host: str) -> bool:
    """Whether url's host is host (already casefolded) or a subdomain of it."""
    try:
        url_host = (urlsplit(url).hostname or "").rstrip(".")
    except ValueError:  # not a URL urlsplit can read
        return False
    return url_host == host or url_host.endswith(f".{host}")


def throttle_message(site: str, pause: float) -> str:
    """What a throttled job shows while the queue pauses for `pause` seconds: the THROTTLED message
    naming the pause in whole minutes, rounded up so the queue never starts again later than it says."""
    return make_error(ErrorKind.THROTTLED, site, throttle_minutes=math.ceil(pause / 60)).message


def check_destination(job: Job, local_work: Path) -> ErrorInfo | None:
    """Check the destination is writable and roomy enough before downloading (blocking: run in a thread).

    A destination that can't be used is a FOLDER error that keeps the job's working folder, like the
    runner's: a partial download is never lost because the folder was briefly unusable (a drive not
    plugged in yet, say). Not enough space is DISK_FULL, which deletes it (spec 5.2).

    The space needed is what is left to download: the estimated size less what the working folder
    already holds. The working folder is always on the destination's drive (see workdir), so what a
    resumed download already has is already taken from that drive's free space."""
    dest, site = job.spec.dest_dir, job.spec.site
    problem = _folder_problem(dest, job.id)
    if problem is not None:
        return folder_error(job.spec, problem)
    try:
        free = shutil.disk_usage(dest).free
    except OSError:
        return folder_error(job.spec, traceback.format_exc())
    estimated = job.spec.estimated_size
    if not estimated:
        return None
    try:
        have = _bytes_in(work_dir_path(dest, job.id, local_work))
    except (OSError, RuntimeError):  # the working folder's place can't be worked out: assume nothing yet
        have = 0
    needed = max(estimated - have, 0)
    if free < needed * DISK_MARGIN:
        details = f"needs about {needed} more bytes ({have} already downloaded), {free} free"
        return make_error(ErrorKind.DISK_FULL, site, details=details)
    return None


def _retry_dest(spec: JobSpec, dest_dir: Path) -> Path:
    """Where retry(dest_dir=...) saves the job: dest_dir, or for a playlist job (saved in a subfolder
    named after its playlist) that subfolder inside dest_dir, unless dest_dir is already named so."""
    subfolder = spec.dest_dir.name
    if spec.playlist_index is None or not subfolder or dest_dir.name.casefold() == subfolder.casefold():
        return dest_dir
    return dest_dir / subfolder


def _folder_problem(dest: Path, job_id: str) -> str | None:
    """Blocking. Create dest if needed and check a file can be written there; the error's details
    (a traceback) when not, else None."""
    try:
        dest.mkdir(parents=True, exist_ok=True)
        probe = dest / f".snag-write-test-{job_id}"
        try:
            probe.write_bytes(b"")
        finally:
            probe.unlink(missing_ok=True)
    except OSError:
        return traceback.format_exc()
    return None


def _rename_dir(source: Path, target: Path) -> None:
    os.rename(source, target)


def _copy_dir(source: Path, target: Path) -> None:
    shutil.copytree(source, target)


def _is_cross_device(error: OSError) -> bool:
    return error.errno == errno.EXDEV or getattr(error, "winerror", None) == _ERROR_NOT_SAME_DEVICE


def _same_place(first: Path, second: Path) -> bool:
    return os.path.normcase(os.path.abspath(first)) == os.path.normcase(os.path.abspath(second))


def _move_partial(job_id: str, old_dest: Path, new_dest: Path, local_work: Path) -> str | None:
    """Blocking. Get a job that is changing folder from old_dest to new_dest ready to run in new_dest.

    When new_dest (or the working folder that goes with it) can't be used, nothing is changed and the
    problem's details are returned, so the partial stays where the job's spec still says it is.

    Otherwise returns None, and the job's partial download, if any, is in the working folder for
    new_dest: nothing moves when both destinations share a working root (Snag's own work folder, the
    usual case); across roots on one drive it is renamed, across drives copied (through a staging
    folder the startup sweep deletes should Snag stop mid-copy) and then deleted. A partial that can't
    be moved is deleted, logged, and the job starts over."""
    problem = _folder_problem(new_dest, job_id)
    if problem is not None:
        return problem
    try:
        source = work_dir_path(old_dest, job_id, local_work)
        target = work_dir_path(new_dest, job_id, local_work)
        if _same_place(source, target) or not source.is_dir():
            return None
        if target.exists():  # left by an earlier attempt in that folder: the newer partial wins
            remove_work_dir(target)
        make_work_dir(new_dest, job_id, local_work).rmdir()  # creates (and hides) a .snag-tmp root
    except (OSError, RuntimeError):
        return traceback.format_exc()
    staging: Path | None = None
    try:
        try:
            _rename_dir(source, target)
            return None
        except OSError as error:
            if not _is_cross_device(error):
                raise
        staging = target.with_name(new_id())
        _copy_dir(source, staging)
        _rename_dir(staging, target)
        staging = None
    except OSError:
        log.warning(
            "Couldn't move the partial download of job %s from %s to %s; it starts over",
            job_id, source, target, exc_info=True,
        )
        if staging is not None:
            remove_work_dir(staging)
    if not remove_work_dir(source):
        log.warning("Couldn't delete the old partial download of job %s in %s", job_id, source)
    return None


def _bytes_in(folder: Path) -> int:
    """The total size of the files under folder; 0 when it doesn't exist. Files that vanish or can't
    be read meanwhile count as nothing."""
    total = 0
    for root, _, names in os.walk(folder):
        for name in names:
            try:
                total += os.stat(os.path.join(root, name), follow_symlinks=False).st_size
            except OSError:
                pass
    return total


def _check_limit(limit: int) -> None:
    """The limits allowed are the ones the max_concurrent setting allows."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit not in MAX_CONCURRENT_RANGE:
        low, high = MAX_CONCURRENT_RANGE[0], MAX_CONCURRENT_RANGE[-1]
        raise ValueError(f"limit must be a whole number from {low} to {high}")
