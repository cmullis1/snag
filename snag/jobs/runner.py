"""Runs one yt-dlp attempt for a job: progress updates, stopping, and moving the result into place.

An attempt never raises: whatever goes wrong becomes an AttemptResult. Deciding what to do next
(retrying with cookies or without subtitles, pausing for throttling) is the queue's job.
"""
from __future__ import annotations

import asyncio
import logging
import traceback
from asyncio.subprocess import Process
from collections import deque
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Awaitable, Callable

from snag.errors import ErrorInfo, ErrorKind, classify, make_error
from snag.paths import AppPaths, Tools
from snag.proc import kill_tree, pump_lines, spawn

from .command import build_command, expected_streams, is_subtitle_failure
from .models import Job, JobSpec, JobState
from .naming import playlist_prefix
from .progress import DownloadEvent, FileEvent, FormatEvent, PostprocessEvent, ProgressTracker, parse_line
from .workdir import cookies_file_path, finalize, find_main_file, make_work_dir, remove_work_dir, work_root_for

log = logging.getLogger(__name__)

TAIL_LINES = 200  # output lines kept for error classification and the "Details" text
NO_FILE_MESSAGE = "yt-dlp finished without producing a file"
# Seconds to wait, once yt-dlp has exited, for its output pipe to close before giving up on the rest of it.
PIPE_GRACE = 2.0
_EXIT_POLL = 0.25  # seconds between checks for yt-dlp having exited while its output is still open
# Windows errors from starting yt-dlp that mean its working folder can't be used: 267 "The directory
# name is invalid" (also a folder past MAX_PATH), 3 "The system cannot find the path specified".
_BAD_FOLDER_ERRORS = frozenset({3, 267})


class Outcome(StrEnum):
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"      # stopped by the user; partial files deleted
    INTERRUPTED = "interrupted"  # stopped by app shutdown; partial files kept for resume


@dataclass(frozen=True)
class AttemptResult:
    outcome: Outcome
    file_path: Path | None = None
    error: ErrorInfo | None = None
    # The attempt failed because subtitles couldn't be downloaded (yt-dlp's message may also look like
    # throttling); the partial is kept so a retry without subtitles can reuse it.
    subtitle_failure: bool = False


class _Output:
    """Turns yt-dlp's output lines into progress on the job and keeps what the end of the attempt
    needs: the final file's path and the last other lines, for error messages."""

    def __init__(self, job: Job, report: Callable[[Job], None]):
        self._job = job
        self._report = report
        self._tracker = ProgressTracker(expected_streams(job.spec.choice))
        self._tail: deque[str] = deque(maxlen=TAIL_LINES)
        self._format_seen = False
        self.file_hint: str | None = None

    @property
    def text(self) -> str:
        return "\n".join(self._tail)

    def feed(self, line: str) -> None:
        job, event = self._job, parse_line(line)
        if isinstance(event, DownloadEvent):
            if not self._format_seen:
                # yt-dlp downloads subtitles before it prints the format and starts on the video.
                return
            self._tracker.update(event)
            job.state, job.stage = JobState.DOWNLOADING, None
            job.percent, job.speed, job.eta = self._tracker.percent, self._tracker.speed, self._tracker.eta
            self._report(job)
        elif isinstance(event, PostprocessEvent):
            if event.status == "started":
                job.state, job.stage, job.speed, job.eta = JobState.PROCESSING, event.label, None, None
                self._report(job)
        elif isinstance(event, FormatEvent):
            self._format_seen = True
            self._tracker.set_streams(event.format_id)
        elif isinstance(event, FileEvent):
            self.file_hint = event.path
        elif line.strip():
            self._tail.append(line)


def _write_cookies(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


def folder_error(spec: JobSpec, details: str, message: str | None = None) -> ErrorInfo:
    """The destination (or the working folder that goes with it) can't be used. Nothing is deleted,
    so whatever an earlier attempt downloaded is still there for a retry once the folder is fixed.
    message: what to show instead of blaming the destination."""
    message = message or f"Can't save to {spec.dest_dir}"
    error = make_error(ErrorKind.FOLDER, spec.site, message=message, details=details)
    return replace(error, keep_partial=True)


def _work_folder_message(spec: JobSpec, local_work: Path, problem: str) -> str | None:
    """Blocking. For a job's working folder that can't be created or used: when it would be in Snag's
    own work folder (the destination shares its drive) the destination isn't to blame, so the message
    names that folder; None (blame the destination) when it would be in the destination's .snag-tmp."""
    try:
        in_local_work = work_root_for(spec.dest_dir, local_work) == local_work
    except (OSError, RuntimeError):  # the paths can't even be resolved: the destination is suspect
        return None
    return f"Can't {problem} Snag's working folder in {local_work}" if in_local_work else None


async def _run_to_end(step: Awaitable[None]) -> None:
    """Await step to the end even if this task is cancelled meanwhile; such a cancellation is raised
    once the step is done, so it is never lost."""
    task = asyncio.ensure_future(step)
    cancelled = False
    while not task.done():
        try:
            await asyncio.wait((task,))  # unlike awaiting the task, this never cancels it
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError


def _remove_cookies(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        # Startup clears the cookies folder, so this one goes then at the latest.
        log.warning("Couldn't delete the cookies file %s", path, exc_info=True)


class JobRunner:
    def __init__(self, tools: Tools, paths: AppPaths, on_progress: Callable[[Job], None]):
        self._tools = tools
        self._paths = paths
        self._on_progress = on_progress
        self._active: set[str] = set()
        self._procs: dict[str, Process] = {}
        self._stops: dict[str, bool] = {}  # job id -> keep partial files

    def is_active(self, job_id: str) -> bool:
        return job_id in self._active

    async def cancel(self, job_id: str, *, keep_partial: bool = False) -> None:
        """Stop a running attempt: CANCELLED deletes its partial files, keep_partial=True (shutdown)
        makes it INTERRUPTED and keeps them. A user cancel isn't undone by a later keep_partial stop.

        Only an attempt whose run() has started executing can be stopped: a cancel for a job that
        isn't active (unknown, finished, or whose run() task hasn't started executing yet) is ignored
        and not remembered, so callers must keep track of a user's cancel themselves and not start
        (or not keep) that attempt."""
        if job_id not in self._active:
            return
        self._stops[job_id] = self._stops.get(job_id, True) and keep_partial
        proc = self._procs.get(job_id)
        if proc is not None:
            await self._kill(proc)

    async def run(self, job: Job, cookies_text: str | None = None, *, without_subtitles: bool = False) -> AttemptResult:
        """Run one yt-dlp attempt for job, reporting progress through on_progress. Never raises for
        anything that goes wrong in the attempt; that becomes the result.

        Raises RuntimeError if an attempt for the same job id is already running.

        Stop an attempt with cancel(). Cancelling the task that runs this also stops yt-dlp, but a
        task cancelled while the finished download is being moved into the destination may still
        complete the move (it runs in a worker thread, which can't be interrupted), leaving the file
        in place although the task ends cancelled."""
        if job.id in self._active:
            raise RuntimeError(f"an attempt for job {job.id} is already running")
        self._active.add(job.id)
        try:
            return await self._attempt(job, cookies_text, without_subtitles)
        finally:
            self._active.discard(job.id)
            self._stops.pop(job.id, None)

    async def _attempt(self, job: Job, cookies_text: str | None, without_subtitles: bool) -> AttemptResult:
        spec = job.spec
        work: Path | None = None
        cookies: Path | None = None
        proc: Process | None = None
        try:
            job.state, job.percent, job.speed, job.eta, job.stage = JobState.FETCHING, None, None, None, None
            self._on_progress(job)
            try:
                work = await asyncio.to_thread(make_work_dir, spec.dest_dir, job.id, self._paths.work_dir)
            except OSError:
                return await self._work_folder_failed(spec, "create", traceback.format_exc())
            if cookies_text is not None:
                cookies = cookies_file_path(self._paths.cookies_dir, job.id)
                await asyncio.to_thread(_write_cookies, cookies, cookies_text)
            output = _Output(job, self._on_progress)
            args = build_command(spec, self._tools, work, cookies, without_subtitles=without_subtitles)
            if job.id in self._stops:
                return await self._stopped(job, work)
            try:
                # One pipe for both streams, so lines (and the error tail) keep their true order.
                proc = await spawn(args, cwd=work, merge_stderr=True)
            except OSError as error:
                if getattr(error, "winerror", None) not in _BAD_FOLDER_ERRORS:
                    raise
                return await self._work_folder_failed(spec, "use", traceback.format_exc())
            self._procs[job.id] = proc
            if job.id in self._stops:  # requested while yt-dlp was starting
                await self._kill(proc)
            code = await self._read_until_exit(proc, output)
            if job.id in self._stops:
                return await self._stopped(job, work)
            return await self._conclude(job, work, code, output)
        except asyncio.CancelledError:
            if proc is not None:
                await _run_to_end(self._kill(proc))
            raise
        except Exception:
            return await self._crashed(job, proc, traceback.format_exc())
        finally:
            self._procs.pop(job.id, None)
            try:
                if proc is not None and proc.returncode is None:
                    # A cancellation cut a kill short (or the kill failed): yt-dlp must be gone
                    # before its cookies are.
                    await _run_to_end(self._kill(proc))
            finally:
                if cookies is not None:
                    _remove_cookies(cookies)

    async def _work_folder_failed(self, spec: JobSpec, problem: str, details: str) -> AttemptResult:
        message = await asyncio.to_thread(_work_folder_message, spec, self._paths.work_dir, problem)
        return AttemptResult(Outcome.FAILED, error=folder_error(spec, details, message))

    async def _read_until_exit(self, proc: Process, output: _Output) -> int:
        """Feed yt-dlp's output to output until it ends; returns yt-dlp's exit code.

        snag.proc ends whatever yt-dlp leaves running when it exits, so its output pipe closes with
        it. Should something still hold the pipe PIPE_GRACE seconds after yt-dlp has exited, the
        rest of the output is abandoned so the attempt can finish (see _let_output_close)."""
        reader = asyncio.ensure_future(self._pump(proc, output))
        try:
            while not (await asyncio.wait((reader,), timeout=_EXIT_POLL))[0]:
                if proc.returncode is not None:
                    await self._let_output_close(proc, reader)
                    break
            if not reader.cancelled():
                reader.result()  # re-raise what went wrong while reading
            return await proc.wait()
        finally:
            if not reader.done():
                reader.cancel()
                await asyncio.wait((reader,))

    async def _let_output_close(self, proc: Process, reader: asyncio.Future[None]) -> None:
        """yt-dlp has exited: give its output pipe PIPE_GRACE seconds to close, then stop reading
        (cancelling reader). By then everything yt-dlp itself wrote has been read.

        Normally the pipe closes with yt-dlp, as snag.proc closes its job when it exits. Whatever
        still holds the pipe after the grace either escaped that job, and nothing here can end it,
        or is in a job snag.proc couldn't close (its wait for the exit failed): ending the tree
        ends that one, and costs nothing otherwise."""
        if (await asyncio.wait((reader,), timeout=PIPE_GRACE))[0]:
            return
        log.warning(
            "yt-dlp (pid %s) exited but something it started still holds its output; ending what it "
            "left running and ignoring the rest of the output", proc.pid,
        )
        await self._kill(proc)
        reader.cancel()
        await asyncio.wait((reader,))

    async def _pump(self, proc: Process, output: _Output) -> None:
        """Feed yt-dlp's output to output until the pipe closes. If output raises (a broken progress
        listener) or the pipe can't be read, yt-dlp is stopped at once instead of being left to finish,
        and the first error is re-raised."""
        kills: list[asyncio.Future[None]] = []

        def on_line(line: str) -> None:
            try:
                output.feed(line)
            except Exception:
                if not kills:
                    kills.append(asyncio.ensure_future(self._kill(proc)))
                raise  # pump_lines keeps draining, then re-raises at the end of the stream

        async def drain(stream: asyncio.StreamReader) -> None:
            try:
                await pump_lines(stream, on_line)
            except Exception:
                await self._kill(proc)
                raise

        streams = [stream for stream in (proc.stdout, proc.stderr) if stream is not None]
        results = await asyncio.gather(*(drain(stream) for stream in streams), return_exceptions=True)
        await asyncio.gather(*kills)
        for result in results:
            if isinstance(result, BaseException):
                raise result

    async def _kill(self, proc: Process) -> None:
        try:
            await kill_tree(proc)
        except Exception:
            log.exception("Couldn't stop yt-dlp (pid %s)", proc.pid)

    async def _stopped(self, job: Job, work: Path) -> AttemptResult:
        if self._stops[job.id]:
            return AttemptResult(Outcome.INTERRUPTED)
        await asyncio.to_thread(remove_work_dir, work)
        return AttemptResult(Outcome.CANCELLED)

    async def _conclude(self, job: Job, work: Path, code: int, output: _Output) -> AttemptResult:
        spec = job.spec
        if code != 0:
            error = classify(output.text, spec.site)
            subtitle_failure = is_subtitle_failure(output.text)
            if not (error.keep_partial or subtitle_failure):
                await asyncio.to_thread(remove_work_dir, work)
            return AttemptResult(Outcome.FAILED, error=error, subtitle_failure=subtitle_failure)
        main = await asyncio.to_thread(find_main_file, work, output.file_hint)
        if main is None:
            # UNKNOWN keeps the working folder, so whatever yt-dlp left there is reused by a retry.
            error = make_error(ErrorKind.UNKNOWN, spec.site, message=NO_FILE_MESSAGE, details=output.text)
            return AttemptResult(Outcome.FAILED, error=error)
        prefix = playlist_prefix(spec.playlist_index, spec.playlist_count) if spec.playlist_index else None
        try:
            final = await asyncio.to_thread(finalize, work, spec.dest_dir, main, prefix)
        except OSError:
            # The download itself is complete, so it stays in the working folder: once the folder is
            # fixed, a retry finds the finished file and only has to move it.
            return AttemptResult(Outcome.FAILED, error=folder_error(spec, traceback.format_exc()))
        return AttemptResult(Outcome.DONE, file_path=final)

    async def _crashed(self, job: Job, proc: Process | None, details: str) -> AttemptResult:
        """After an unexpected exception: stop yt-dlp but keep the working folder, so whatever was
        downloaded is still there for a retry (an UNKNOWN error says the partial is kept)."""
        log.error("Download attempt for job %s failed unexpectedly:\n%s", job.id, details)
        if proc is not None:
            await self._kill(proc)
        return AttemptResult(Outcome.FAILED, error=make_error(ErrorKind.UNKNOWN, job.spec.site, details=details))
