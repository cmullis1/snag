import asyncio
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import pytest

import snag.jobs.runner as runner_module
from snag import proc as proc_module
from snag.errors import ErrorKind
from snag.jobs.models import ChoiceKind, Extras, FormatChoice, Job, JobState
from snag.jobs.runner import JobRunner, Outcome
from snag.jobs.workdir import TMP_FOLDER, cookies_file_path, work_dir_path
from snag.paths import AppPaths
from tests.helpers import make_spec, wait_for

COOKIES = "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tsecret\n"


@pytest.fixture
def paths(tmp_path) -> AppPaths:
    return AppPaths(tmp_path / "Snag")


@pytest.fixture
def dest(tmp_path) -> Path:
    return tmp_path / "Videos"


def runner_with_log(fake_tools, paths):
    seen: list[tuple[JobState, float | None, str | None]] = []
    runner = JobRunner(fake_tools, paths, lambda job: seen.append((job.state, job.percent, job.stage)))
    return runner, seen


def spy_spawn(monkeypatch, before_return=None) -> list[asyncio.subprocess.Process]:
    """Record every process the runner starts so tests can check none is left running."""
    procs: list[asyncio.subprocess.Process] = []
    real_spawn = runner_module.spawn

    async def spawn(args, cwd=None, **kwargs):
        proc = await real_spawn(args, cwd=cwd, **kwargs)
        procs.append(proc)
        if before_return is not None:
            await before_return()
        return proc

    monkeypatch.setattr(runner_module, "spawn", spawn)
    return procs


def work_of(job: Job, paths: AppPaths) -> Path:
    return work_dir_path(job.spec.dest_dir, job.id, paths.work_dir)


# --- success ---

async def test_success_moves_file_and_reports_progress(fake_tools, paths, dest):
    runner, seen = runner_with_log(fake_tools, paths)
    job = Job(spec=make_spec("fake://success", dest))
    result = await runner.run(job)
    assert result.outcome is Outcome.DONE
    assert result.file_path == dest / "Fake Video.mp4"
    assert result.file_path.exists()
    assert result.error is None and result.subtitle_failure is False
    assert not work_of(job, paths).exists()
    states = [state for state, _, _ in seen]
    assert states[0] is JobState.FETCHING
    assert JobState.DOWNLOADING in states and states[-1] is JobState.PROCESSING
    percents = [p for state, p, _ in seen if state is JobState.DOWNLOADING and p is not None]
    assert percents == sorted(percents) and percents[-1] == 100.0
    assert "Joining video and audio…" in [stage for _, _, stage in seen]


async def test_working_folder_is_under_snags_work_folder_on_the_same_drive(fake_tools, paths, dest):
    runner = JobRunner(fake_tools, paths, lambda job: None)
    job = Job(spec=make_spec("fake://fail-network", dest))
    await runner.run(job)
    assert work_of(job, paths) == paths.work_dir / job.id
    assert (paths.work_dir / job.id).is_dir()
    assert not (dest / TMP_FOLDER).exists()


async def test_quick_single_stream_progress_reaches_100(fake_tools, paths, dest):
    # QUICK expects two streams until yt-dlp's SNAGFMT line says only one was picked.
    runner, seen = runner_with_log(fake_tools, paths)
    spec = make_spec("fake://single", dest, choice=FormatChoice(ChoiceKind.QUICK, height=1080))
    result = await runner.run(Job(spec=spec))
    assert result.outcome is Outcome.DONE
    percents = [p for state, p, _ in seen if state is JobState.DOWNLOADING and p is not None]
    assert percents == sorted(percents) and percents[-1] == 100.0


async def test_unicode_title_survives(fake_tools, paths, dest):
    runner, _ = runner_with_log(fake_tools, paths)
    result = await runner.run(Job(spec=make_spec("fake://unicode", dest)))
    assert result.outcome is Outcome.DONE
    assert result.file_path.name == "Café ünïcode ✓.mp4"


async def test_playlist_prefix_and_subtitle_file(fake_tools, paths, dest):
    runner, _ = runner_with_log(fake_tools, paths)
    spec = make_spec("fake://success", dest, playlist_index=3, playlist_count=12,
                     extras=Extras(subtitles=True, subtitle_mode="file"))
    result = await runner.run(Job(spec=spec))
    assert result.file_path.name == "03 - Fake Video.mp4"
    assert (dest / "03 - Fake Video.en.srt").exists()


async def test_work_folder_named_like_an_environment_variable_is_used_as_is(fake_tools, tmp_path, dest, monkeypatch):
    monkeypatch.setenv("SNAGTESTVAR", "expanded")
    paths = AppPaths(tmp_path / "%SNAGTESTVAR%" / "Snag")
    runner, _ = runner_with_log(fake_tools, paths)
    result = await runner.run(Job(spec=make_spec("fake://success", dest)))
    assert result.outcome is Outcome.DONE
    assert result.file_path == dest / "Fake Video.mp4"
    assert not (tmp_path / "expanded").exists()


# --- failures ---

async def test_failure_keeps_partial_when_resumable(fake_tools, paths, dest):
    runner, _ = runner_with_log(fake_tools, paths)
    job = Job(spec=make_spec("fake://fail-network", dest))
    result = await runner.run(job)
    assert result.outcome is Outcome.FAILED
    assert result.error.kind is ErrorKind.NETWORK
    assert "getaddrinfo failed" in result.error.details
    assert work_of(job, paths).exists()


async def test_failure_removes_partial_when_pointless(fake_tools, paths, dest):
    runner, _ = runner_with_log(fake_tools, paths)
    job = Job(spec=make_spec("fake://unavailable", dest))
    result = await runner.run(job)
    assert result.error.kind is ErrorKind.UNAVAILABLE
    assert not work_of(job, paths).exists()


async def test_finishing_without_a_file_is_a_failure(fake_tools, paths, dest):
    runner, _ = runner_with_log(fake_tools, paths)
    job = Job(spec=make_spec("fake://no-file", dest))
    result = await runner.run(job)
    assert result.outcome is Outcome.FAILED
    assert result.error.kind is ErrorKind.UNKNOWN
    assert result.error.message == "yt-dlp finished without producing a file"
    assert not dest.exists() or list(dest.iterdir()) == []


async def test_subtitle_failure_is_flagged_and_keeps_the_partial(fake_tools, paths, dest):
    runner, _ = runner_with_log(fake_tools, paths)
    job = Job(spec=make_spec("fake://subs-fail", dest, extras=Extras(subtitles=True)))
    result = await runner.run(job)
    assert result.outcome is Outcome.FAILED
    assert result.subtitle_failure is True
    # yt-dlp's message contains "HTTP Error 429", so classify alone says THROTTLED; the flag is what
    # lets the queue treat it as a subtitle failure first.
    assert result.error.kind is ErrorKind.THROTTLED
    assert work_of(job, paths).is_dir()
    retry = await runner.run(job, without_subtitles=True)
    assert retry.outcome is Outcome.DONE and retry.file_path == dest / "Fake Video.mp4"


async def test_ordinary_failures_are_not_subtitle_failures(fake_tools, paths, dest):
    runner, _ = runner_with_log(fake_tools, paths)
    result = await runner.run(Job(spec=make_spec("fake://throttle-once/t", dest, extras=Extras(subtitles=True))))
    assert result.error.kind is ErrorKind.THROTTLED
    assert result.subtitle_failure is False


async def test_finalize_failure_is_a_folder_error(fake_tools, paths, tmp_path):
    dest = tmp_path / "not-a-folder"
    dest.write_text("a file where the destination folder should be")
    runner, _ = runner_with_log(fake_tools, paths)
    job = Job(spec=make_spec("fake://success", dest))
    result = await runner.run(job)
    assert result.outcome is Outcome.FAILED
    assert result.error.kind is ErrorKind.FOLDER
    assert str(dest) in result.error.message
    # The download itself finished, so it's kept: a retry only has to move it.
    assert (work_of(job, paths) / "Fake Video.mp4").is_file()


# --- cookies ---

async def test_cookies_live_in_the_cookies_folder_only_during_the_attempt(fake_tools, paths, dest):
    job = Job(spec=make_spec("fake://signin", dest))
    cookies_file = cookies_file_path(paths.cookies_dir, job.id)
    during: list[str] = []

    def on_progress(job):
        if job.state is JobState.DOWNLOADING and not during:
            during.append(cookies_file.read_text(encoding="utf-8"))

    runner = JobRunner(fake_tools, paths, on_progress)
    assert (await runner.run(job)).error.kind is ErrorKind.SIGN_IN
    result = await runner.run(job, cookies_text=COOKIES)
    assert result.outcome is Outcome.DONE
    assert during == [COOKIES]
    assert not cookies_file.exists()
    assert not any(work_of(job, paths).parent.glob("*/*.txt"))  # never beside the download


async def test_cookies_are_removed_after_a_failed_attempt(fake_tools, paths, dest):
    runner, _ = runner_with_log(fake_tools, paths)
    job = Job(spec=make_spec("fake://fail-network", dest))
    result = await runner.run(job, cookies_text=COOKIES)
    assert result.outcome is Outcome.FAILED
    assert not cookies_file_path(paths.cookies_dir, job.id).exists()


# --- stopping ---

async def test_cancel_kills_and_cleans_up(fake_tools, paths, dest, monkeypatch):
    procs = spy_spawn(monkeypatch)
    runner, _ = runner_with_log(fake_tools, paths)
    job = Job(spec=make_spec("fake://slow", dest))
    task = asyncio.create_task(runner.run(job, cookies_text=COOKIES))
    await wait_for(lambda: job.state is JobState.DOWNLOADING)
    assert runner.is_active(job.id)
    await runner.cancel(job.id)
    result = await asyncio.wait_for(task, 10)
    assert result.outcome is Outcome.CANCELLED
    assert not work_of(job, paths).exists()
    assert not cookies_file_path(paths.cookies_dir, job.id).exists()
    assert not runner.is_active(job.id)
    assert all(proc.returncode is not None for proc in procs)


async def test_interrupt_keeps_partial(fake_tools, paths, dest, monkeypatch):
    procs = spy_spawn(monkeypatch)
    runner, _ = runner_with_log(fake_tools, paths)
    job = Job(spec=make_spec("fake://slow", dest))
    task = asyncio.create_task(runner.run(job))
    await wait_for(lambda: job.state is JobState.DOWNLOADING)
    await runner.cancel(job.id, keep_partial=True)
    result = await asyncio.wait_for(task, 10)
    assert result.outcome is Outcome.INTERRUPTED
    assert any(path.name.endswith(".part") for path in work_of(job, paths).iterdir())
    assert all(proc.returncode is not None for proc in procs)


async def test_user_cancel_wins_over_an_interrupt(fake_tools, paths, dest):
    runner, _ = runner_with_log(fake_tools, paths)
    job = Job(spec=make_spec("fake://slow", dest))
    task = asyncio.create_task(runner.run(job))
    await wait_for(lambda: job.state is JobState.DOWNLOADING)
    await runner.cancel(job.id)
    await runner.cancel(job.id, keep_partial=True)
    assert (await asyncio.wait_for(task, 10)).outcome is Outcome.CANCELLED


async def test_stop_requested_while_yt_dlp_starts_is_honoured(fake_tools, paths, dest, monkeypatch):
    job = Job(spec=make_spec("fake://slow", dest))
    runner, _ = runner_with_log(fake_tools, paths)

    async def cancel_now():  # yt-dlp exists, but the runner hasn't been handed it yet
        await runner.cancel(job.id)

    procs = spy_spawn(monkeypatch, before_return=cancel_now)
    result = await asyncio.wait_for(runner.run(job), 15)  # the slow download would take ~40 s
    assert result.outcome is Outcome.CANCELLED
    assert len(procs) == 1 and procs[0].returncode is not None
    assert not work_of(job, paths).exists()


async def test_stop_requested_before_yt_dlp_starts_skips_it(fake_tools, paths, dest, monkeypatch):
    procs = spy_spawn(monkeypatch)
    runner, _ = runner_with_log(fake_tools, paths)
    job = Job(spec=make_spec("fake://slow", dest))
    task = asyncio.create_task(runner.run(job))
    await asyncio.sleep(0)  # run() is now preparing the working folder
    assert runner.is_active(job.id)
    await runner.cancel(job.id, keep_partial=True)
    assert (await asyncio.wait_for(task, 10)).outcome is Outcome.INTERRUPTED
    assert procs == []


async def test_stops_are_forgotten_when_an_attempt_ends(fake_tools, paths, dest):
    runner, _ = runner_with_log(fake_tools, paths)
    job = Job(spec=make_spec("fake://slow", dest))
    task = asyncio.create_task(runner.run(job))
    await wait_for(lambda: job.state is JobState.DOWNLOADING)
    await runner.cancel(job.id)
    assert (await asyncio.wait_for(task, 10)).outcome is Outcome.CANCELLED
    job.spec = make_spec("fake://success", dest)
    assert (await runner.run(job)).outcome is Outcome.DONE


async def test_cancel_unknown_job_is_harmless(fake_tools, paths):
    runner, _ = runner_with_log(fake_tools, paths)
    await runner.cancel("nope")
    assert not runner.is_active("nope")


async def test_cancelling_the_run_task_stops_yt_dlp(fake_tools, paths, dest, monkeypatch):
    procs = spy_spawn(monkeypatch)
    runner, _ = runner_with_log(fake_tools, paths)
    job = Job(spec=make_spec("fake://slow", dest))
    task = asyncio.create_task(runner.run(job, cookies_text=COOKIES))
    await wait_for(lambda: job.state is JobState.DOWNLOADING)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert procs[0].returncode is not None
    assert not cookies_file_path(paths.cookies_dir, job.id).exists()
    assert not runner.is_active(job.id)


# --- never raising ---

async def test_progress_callback_exception_fails_the_attempt_and_stops_yt_dlp(fake_tools, paths, dest, monkeypatch):
    procs = spy_spawn(monkeypatch)

    def broken_listener(job):
        if job.state is JobState.DOWNLOADING:
            raise RuntimeError("listener broke")

    runner = JobRunner(fake_tools, paths, broken_listener)
    job = Job(spec=make_spec("fake://slow", dest))
    result = await asyncio.wait_for(runner.run(job, cookies_text=COOKIES), 15)  # stopped, not left to finish
    assert result.outcome is Outcome.FAILED
    assert result.error.kind is ErrorKind.UNKNOWN
    assert "listener broke" in result.error.details and "Traceback" in result.error.details
    assert all(proc.returncode is not None for proc in procs)
    # Whatever was downloaded is kept, as the error says, so a retry can resume it.
    assert result.error.keep_partial and work_of(job, paths).is_dir()
    assert not cookies_file_path(paths.cookies_dir, job.id).exists()
    assert not runner.is_active(job.id)


async def test_unexpected_error_before_starting_yt_dlp_is_a_failure(fake_tools, paths, dest, monkeypatch):
    def broken_build(*args, **kwargs):
        raise ValueError("bad spec")

    monkeypatch.setattr(runner_module, "build_command", broken_build)
    runner, _ = runner_with_log(fake_tools, paths)
    job = Job(spec=make_spec("fake://success", dest))
    result = await runner.run(job, cookies_text=COOKIES)
    assert result.outcome is Outcome.FAILED
    assert result.error.kind is ErrorKind.UNKNOWN and "bad spec" in result.error.details
    assert result.error.keep_partial and work_of(job, paths).is_dir()
    assert not cookies_file_path(paths.cookies_dir, job.id).exists()


async def test_listener_failing_on_the_first_update_is_a_failure(fake_tools, paths, dest):
    def broken_listener(job):
        raise RuntimeError("no listeners today")

    runner = JobRunner(fake_tools, paths, broken_listener)
    result = await runner.run(Job(spec=make_spec("fake://success", dest)))
    assert result.outcome is Outcome.FAILED and "no listeners today" in result.error.details


# --- blocking file work stays off the event loop ---

async def test_file_work_runs_in_worker_threads(fake_tools, paths, dest, monkeypatch):
    calls: list[tuple[str, bool]] = []

    def watch(name):
        real = getattr(runner_module, name)

        def wrapper(*args, **kwargs):
            calls.append((name, threading.current_thread() is threading.main_thread()))
            return real(*args, **kwargs)

        monkeypatch.setattr(runner_module, name, wrapper)

    for name in ("make_work_dir", "find_main_file", "finalize", "remove_work_dir"):
        watch(name)
    runner, _ = runner_with_log(fake_tools, paths)
    await runner.run(Job(spec=make_spec("fake://success", dest)))
    await runner.run(Job(spec=make_spec("fake://unavailable", dest)))
    assert {name for name, _ in calls} == {"make_work_dir", "find_main_file", "finalize", "remove_work_dir"}
    assert not any(on_loop_thread for _, on_loop_thread in calls)


# --- destination on another drive ---

def _drive(path: Path | str) -> str:
    return os.path.splitdrive(str(Path(path).resolve()))[0].casefold()


def _folder_on_another_drive(than: Path) -> Path | None:
    """A folder this test suite owns on a different drive: the repository's (git-ignored) .pytest_cache,
    when the repository and the temp folder are on different drives."""
    cache = Path(__file__).resolve().parent.parent / ".pytest_cache"
    return cache if _drive(cache) != _drive(than) else None


async def test_destination_on_another_drive_uses_a_hidden_folder_beside_it(fake_tools, paths, tmp_path):
    base = _folder_on_another_drive(than=tmp_path)
    if base is None:
        pytest.skip("needs the repository and the temp folder on different drives to test a cross-drive destination")
    base.mkdir(exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="snag-cross-drive-", dir=base))
    try:
        dest = root / "Videos"
        runner, _ = runner_with_log(fake_tools, paths)
        failing = Job(spec=make_spec("fake://fail-network", dest))
        await runner.run(failing)
        assert (dest / TMP_FOLDER / failing.id).is_dir()
        assert not (paths.work_dir / failing.id).exists()
        result = await runner.run(Job(spec=make_spec("fake://success", root / "Music")))
        assert result.outcome is Outcome.DONE and result.file_path == root / "Music" / "Fake Video.mp4"
        assert not (root / "Music" / TMP_FOLDER).exists()
    finally:
        shutil.rmtree(root, ignore_errors=True)


# --- progress details ---

def downloading_percents(seen) -> list[float | None]:
    return [percent for state, percent, _ in seen if state is JobState.DOWNLOADING]


@pytest.mark.parametrize("url, choice", [
    ("fake://success", FormatChoice(ChoiceKind.VIDEO, "299", "140", 1080, "mp4")),
    ("fake://single", FormatChoice(ChoiceKind.QUICK, height=1080)),
])
async def test_subtitle_downloads_dont_move_progress(fake_tools, paths, dest, url, choice):
    # yt-dlp downloads the subtitles (format id NA) before printing SNAGFMT and starting the video.
    runner, seen = runner_with_log(fake_tools, paths)
    spec = make_spec(url, dest, choice=choice, extras=Extras(subtitles=True, subtitle_mode="file"))
    result = await runner.run(Job(spec=spec))
    assert result.outcome is Outcome.DONE
    percents = downloading_percents(seen)
    assert None not in percents, "the subtitle download showed up as downloading the video"
    assert percents == sorted(percents) and percents[0] <= 25.0 and percents[-1] == 100.0


async def test_already_downloaded_file_shows_100(fake_tools, paths, dest):
    runner, seen = runner_with_log(fake_tools, paths)
    result = await runner.run(Job(spec=make_spec("fake://resume-complete", dest)))
    assert result.outcome is Outcome.DONE
    assert downloading_percents(seen) == [100.0]


async def test_yt_dlps_output_is_read_as_one_ordered_stream(fake_tools, paths, dest, monkeypatch):
    procs = spy_spawn(monkeypatch)
    runner, _ = runner_with_log(fake_tools, paths)
    result = await runner.run(Job(spec=make_spec("fake://unavailable", dest)))
    assert procs[0].stderr is None  # stderr goes into stdout
    # yt-dlp prints errors on stderr; they are still classified.
    assert result.error.kind is ErrorKind.UNAVAILABLE
    assert "ERROR: [youtube] abc123: Video unavailable" in result.error.details


# --- bad destinations ---

def _missing_drive() -> str | None:
    for letter in "QRSTUVWXYZ":
        if not os.path.exists(f"{letter}:\\"):
            return f"{letter}:\\"
    return None


async def test_destination_on_a_missing_drive_is_a_folder_error(fake_tools, paths):
    drive = _missing_drive()
    if drive is None:
        pytest.skip("every drive letter from Q to Z is in use")
    dest = Path(drive) / "Videos"
    runner, _ = runner_with_log(fake_tools, paths)
    result = await runner.run(Job(spec=make_spec("fake://success", dest)))
    assert result.outcome is Outcome.FAILED
    assert result.error.kind is ErrorKind.FOLDER
    assert result.error.message == f"Can't save to {dest}"
    assert "Traceback" in result.error.details


async def test_working_folder_too_long_to_run_in_is_a_folder_error(fake_tools, tmp_path, dest, monkeypatch):
    # Windows can't start a process in a folder past MAX_PATH, even with long paths enabled.
    procs = spy_spawn(monkeypatch)
    paths = AppPaths(tmp_path.joinpath(*["d" * 60] * 5) / "Snag")
    runner, _ = runner_with_log(fake_tools, paths)
    result = await runner.run(Job(spec=make_spec("fake://success", dest)))
    assert result.outcome is Outcome.FAILED
    assert result.error.kind is ErrorKind.FOLDER
    # The destination shares a drive with Snag's own work folder, so that's where yt-dlp would run.
    assert result.error.message == f"Can't use Snag's working folder in {paths.work_dir}"
    assert procs == []


async def test_a_working_folder_that_cant_be_created_in_snags_folder_says_so(fake_tools, tmp_path, dest):
    paths = AppPaths(tmp_path / "Snag")
    paths.work_dir.parent.mkdir(parents=True)
    paths.work_dir.write_text("a file where the folder should be")
    runner, _ = runner_with_log(fake_tools, paths)
    result = await runner.run(Job(spec=make_spec("fake://success", dest)))
    assert result.outcome is Outcome.FAILED
    assert result.error.kind is ErrorKind.FOLDER
    assert result.error.message == f"Can't create Snag's working folder in {paths.work_dir}"
    assert "Traceback" in result.error.details


async def test_a_working_folder_that_cant_be_created_in_the_destination_blames_it(fake_tools, paths, dest, monkeypatch):
    # On another drive the working folder is <destination>\.snag-tmp, so the destination is the problem.
    monkeypatch.setattr(runner_module, "work_root_for", lambda dest_dir, local: dest_dir / TMP_FOLDER)
    monkeypatch.setattr(runner_module, "make_work_dir", lambda dest_dir, job_id, local: _raise(PermissionError(5, "denied")))
    runner, _ = runner_with_log(fake_tools, paths)
    result = await runner.run(Job(spec=make_spec("fake://success", dest)))
    assert result.error.kind is ErrorKind.FOLDER
    assert result.error.message == f"Can't save to {dest}"


def _raise(error: Exception):
    raise error


async def test_yt_dlp_failing_to_start_for_another_reason_is_not_a_folder_error(fake_tools, paths, dest, monkeypatch):
    async def missing_program(args, cwd=None, **kwargs):
        raise FileNotFoundError(2, "The system cannot find the file specified", None, 2)

    monkeypatch.setattr(runner_module, "spawn", missing_program)
    runner, _ = runner_with_log(fake_tools, paths)
    result = await runner.run(Job(spec=make_spec("fake://success", dest)))
    assert result.outcome is Outcome.FAILED
    assert result.error.kind is ErrorKind.UNKNOWN


# --- one attempt per job ---

async def test_a_second_run_of_a_running_job_is_refused(fake_tools, paths, dest):
    runner, _ = runner_with_log(fake_tools, paths)
    job = Job(spec=make_spec("fake://slow", dest))
    task = asyncio.create_task(runner.run(job))
    await wait_for(lambda: job.state is JobState.DOWNLOADING)
    with pytest.raises(RuntimeError):
        await runner.run(job)
    assert runner.is_active(job.id)  # the first attempt is untouched
    await runner.cancel(job.id)
    assert (await asyncio.wait_for(task, 10)).outcome is Outcome.CANCELLED


# --- processes yt-dlp leaves behind ---

def _alive(pid: int) -> bool:
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
    return str(pid) in out


def _force_kill(pid: int) -> None:
    subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)


async def _wait_dead(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while _alive(pid):
        if time.monotonic() > deadline:
            return False
        await asyncio.sleep(0.1)
    return True


def stray_pid(tmp_path: Path) -> int:
    return int((tmp_path / "fake-state" / "stray-pid").read_text())


async def test_a_child_left_holding_the_output_doesnt_hold_up_the_attempt(fake_tools, paths, dest, tmp_path):
    runner, _ = runner_with_log(fake_tools, paths)
    try:
        result = await asyncio.wait_for(runner.run(Job(spec=make_spec("fake://stray-child", dest))), 10)
        assert result.outcome is Outcome.DONE
        assert await _wait_dead(stray_pid(tmp_path))
    finally:
        if (tmp_path / "fake-state" / "stray-pid").exists():
            _force_kill(stray_pid(tmp_path))


async def test_without_a_job_a_stray_child_only_delays_the_attempt_briefly(fake_tools, paths, dest, tmp_path, monkeypatch):
    monkeypatch.setattr(proc_module, "_open_child_job", lambda process: None)
    monkeypatch.setattr(runner_module, "PIPE_GRACE", 0.3)
    runner, _ = runner_with_log(fake_tools, paths)
    try:
        started = time.monotonic()
        result = await asyncio.wait_for(runner.run(Job(spec=make_spec("fake://stray-child", dest))), 30)
        assert result.outcome is Outcome.DONE
        assert time.monotonic() - started < 20  # the stray sleeps for 60 s
    finally:
        if (tmp_path / "fake-state" / "stray-pid").exists():
            _force_kill(stray_pid(tmp_path))


async def test_a_stray_child_whose_job_was_left_open_is_ended_after_the_grace(
    fake_tools, paths, dest, tmp_path, monkeypatch, caplog,
):
    # The watcher's wait failed, so yt-dlp's job wasn't closed when it exited: the runner ends the stray.
    monkeypatch.setattr(proc_module, "_WaitForSingleObject", lambda handle, milliseconds: proc_module._WAIT_FAILED)
    monkeypatch.setattr(runner_module, "PIPE_GRACE", 0.3)
    procs = spy_spawn(monkeypatch)
    runner, _ = runner_with_log(fake_tools, paths)
    try:
        with caplog.at_level(logging.WARNING, logger="snag.jobs.runner"):
            started = time.monotonic()
            result = await asyncio.wait_for(runner.run(Job(spec=make_spec("fake://stray-child", dest))), 30)
        assert result.outcome is Outcome.DONE
        assert time.monotonic() - started < 20  # the stray sleeps for 60 s
        assert await _wait_dead(stray_pid(tmp_path))
        assert "still holds its output" in caplog.text and "ignoring the rest" in caplog.text
    finally:
        if (tmp_path / "fake-state" / "stray-pid").exists():
            _force_kill(stray_pid(tmp_path))
        for proc in procs:
            if (job := proc_module._child_job_of(proc)) is not None:
                job.close()


async def test_cancel_ends_children_yt_dlp_lost_track_of(fake_tools, paths, dest, tmp_path):
    runner, _ = runner_with_log(fake_tools, paths)
    job = Job(spec=make_spec("fake://slow-stray", dest))
    task = asyncio.create_task(runner.run(job))
    try:
        await wait_for(lambda: job.state is JobState.DOWNLOADING)
        pid = stray_pid(tmp_path)
        assert _alive(pid)
        await runner.cancel(job.id)
        assert (await asyncio.wait_for(task, 10)).outcome is Outcome.CANCELLED
        assert await _wait_dead(pid)
    finally:
        if (tmp_path / "fake-state" / "stray-pid").exists():
            _force_kill(stray_pid(tmp_path))


# --- cancelling the task ---

async def test_cookies_outlast_yt_dlp_even_when_the_task_is_cancelled_twice(fake_tools, paths, dest, monkeypatch):
    procs = spy_spawn(monkeypatch)
    job = Job(spec=make_spec("fake://slow", dest))
    cookies_file = cookies_file_path(paths.cookies_dir, job.id)
    kill_started = asyncio.Event()
    cookies_when_killed: list[bool] = []
    real_kill_tree = runner_module.kill_tree

    async def slow_kill_tree(proc):
        kill_started.set()
        await asyncio.sleep(0.5)
        await real_kill_tree(proc)
        cookies_when_killed.append(cookies_file.exists())

    monkeypatch.setattr(runner_module, "kill_tree", slow_kill_tree)
    runner, _ = runner_with_log(fake_tools, paths)
    task = asyncio.create_task(runner.run(job, cookies_text=COOKIES))
    await wait_for(lambda: job.state is JobState.DOWNLOADING)
    task.cancel()
    await asyncio.wait_for(kill_started.wait(), 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cookies_when_killed == [True], "the cookies were deleted while yt-dlp was still running"
    assert procs[0].returncode is not None
    assert not cookies_file.exists()
    assert not runner.is_active(job.id)
