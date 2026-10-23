"""JobQueue recovery: subtitles, sign-in, stale formats and throttling (plan Task 18, revised)."""
import asyncio
import time
import uuid

import pytest

from snag.errors import ErrorKind
from snag.info.lookup import LookupFailed
from snag.jobs.models import ChoiceKind, Extras, FormatChoice, JobState
from tests.helpers import Recorder, make_spec, wait_for
from tests.queue_env import COOKIES, GONE, M4A, video, video_info_with


def attempts(recorder: Recorder, job) -> int:
    """Each attempt reports FETCHING once when it starts."""
    return sum(1 for job_id, state, _ in recorder.events if job_id == job.id and state is JobState.FETCHING)


# --- subtitles ---

async def test_subtitle_failure_retries_without_subtitles(queue_env):
    queue = queue_env.make_queue()
    recorder = Recorder()
    queue.subscribe(recorder)
    job = queue.add(make_spec("fake://subs-fail", queue_env.dest, extras=Extras(subtitles=True)))
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert job.state is JobState.DONE and job.error is None
    assert job.subtitles_skipped is True
    assert attempts(recorder, job) == 2
    assert queue_env.store.load(job.id).subtitles_skipped is True
    assert queue_env.history.list()[0].format_summary == "1080p MP4 + saved without subtitles"


async def test_subtitle_failure_is_not_treated_as_throttling(queue_env):
    queue = queue_env.make_queue(max_concurrent=3, throttle_pause=60.0)
    throttled_seen = []
    queue.subscribe(lambda job: throttled_seen.append(queue.throttled))
    job = queue.add(make_spec("fake://subs-fail", queue_env.dest, extras=Extras(subtitles=True)))
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert job.state is JobState.DONE
    assert not any(throttled_seen) and not queue.throttled
    assert queue.current_limit == 3


async def test_downloads_with_subtitles_that_work_are_not_marked(queue_env):
    queue = queue_env.make_queue()
    job = queue.add(make_spec("fake://success", queue_env.dest, extras=Extras(subtitles=True)))
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert job.state is JobState.DONE and job.subtitles_skipped is False


# --- sign-in ---

async def test_sign_in_retries_with_cookies(queue_env):
    asked = []
    queue = queue_env.make_queue(cookies=lambda url: asked.append(url) or COOKIES)
    job = queue.add(make_spec("fake://signin", queue_env.dest))
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert job.state is JobState.DONE
    assert job.used_cookies is True
    assert asked == ["fake://signin"]
    assert not queue_env.work_of(job).exists()
    assert not any(queue_env.paths.cookies_dir.glob("*"))  # the runner deletes the cookies file


async def test_sign_in_without_cookies_fails_with_message(queue_env):
    queue = queue_env.make_queue()
    job = queue.add(make_spec("fake://signin", queue_env.dest))
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert job.state is JobState.FAILED
    assert job.error.kind is ErrorKind.SIGN_IN
    assert job.error.message == "Sign in to YouTube in Chrome or Edge, then retry"
    assert job.used_cookies is False


async def test_fresh_cookies_retry_the_jobs_that_needed_signing_in(queue_env, monkeypatch):
    commands = spy_commands(monkeypatch)
    cookies: list[str] = []  # none until the extension pushes some
    queue = queue_env.make_queue(cookies=lambda url: cookies[0] if cookies else None)
    signin = queue.add(make_spec("fake://signin", queue_env.dest))
    network = queue.add(make_spec("fake://fail-network", queue_env.dest))
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert signin.error.kind is ErrorKind.SIGN_IN and network.error.kind is ErrorKind.NETWORK
    commands.clear()
    cookies.append(COOKIES)
    assert queue.retry_signin_failures() == [signin]
    assert signin.state is JobState.WAITING and network.state is JobState.FAILED
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert signin.state is JobState.DONE and signin.used_cookies
    assert len(commands) == 1 and "--cookies" in commands[0]  # straight to the cookies, no failing first
    assert network.state is JobState.FAILED
    assert queue.retry_signin_failures() == []


def failed_sign_in(env, url: str):
    from snag.errors import make_error
    from snag.jobs.models import Job

    job = Job(spec=make_spec(url, env.dest))
    job.state, job.error, job.finished_at = JobState.FAILED, make_error(ErrorKind.SIGN_IN, "youtube"), 1.0
    env.store.save(job)
    return job


async def test_fresh_cookies_for_one_site_retry_only_that_sites_jobs(queue_env):
    on_site = [
        failed_sign_in(queue_env, "https://www.youtube.com/watch?v=a"),
        failed_sign_in(queue_env, "https://music.YouTube.com/watch?v=b"),
        failed_sign_in(queue_env, "https://youtube.com/watch?v=c"),
    ]
    elsewhere = [
        failed_sign_in(queue_env, "https://vimeo.com/1"),
        failed_sign_in(queue_env, "https://notyoutube.com/watch?v=d"),
    ]
    queue = queue_env.make_queue()
    await queue.start()
    queue.pause()  # only which jobs are retried matters here
    retried = queue.retry_signin_failures("youtube.com")
    assert sorted(job.id for job in retried) == sorted(job.id for job in on_site)
    assert all(queue.get(job.id).state is JobState.WAITING for job in on_site)
    assert all(queue.get(job.id).state is JobState.FAILED for job in elsewhere)


# --- stale formats ---

async def test_format_gone_switches_to_closest_match(queue_env):
    formats = [M4A, video("298", 720, "avc1.4D4020"), video("299", 1080, "avc1.64002A")]
    looked_up = []

    async def lookup(url):
        looked_up.append(url)
        return video_info_with(formats)

    queue = queue_env.make_queue(lookup=lookup)
    job = queue.add(make_spec("fake://format-gone", queue_env.dest, choice=GONE))
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert job.state is JobState.DONE
    assert job.spec.choice.format_id == "299" and job.refetched is True
    assert looked_up == ["fake://format-gone"]
    assert queue_env.store.load(job.id).spec.choice.format_id == "299"


async def test_closest_match_keeps_the_codec_family(queue_env):
    formats = [M4A, video("399", 1080, "av01.0.08M.08"), video("299", 1080, "avc1.64002A"), video("303", 1080, "vp09.00.41.08", "webm")]

    async def lookup(url):
        return video_info_with(formats)

    queue = queue_env.make_queue(lookup=lookup)
    job = queue.add(make_spec("fake://format-gone", queue_env.dest, choice=GONE))
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert job.state is JobState.DONE
    assert (job.spec.choice.format_id, job.spec.choice.vcodec) == ("299", "avc1.64002A")


async def test_format_gone_switches_an_audio_download_to_the_closest_audio(queue_env, monkeypatch):
    import snag.jobs.runner as runner_module

    def track(format_id, ext, acodec, abr, language="en", preference=10):
        return {"format_id": format_id, "ext": ext, "protocol": "https", "vcodec": "none", "acodec": acodec,
                "abr": abr, "filesize": 1, "language": language, "language_preference": preference}

    formats = [
        track("140", "m4a", "mp4a.40.2", 128), track("250", "webm", "opus", 70), track("251", "webm", "opus", 160),
        track("251-1", "webm", "opus", 200, language="de", preference=-1),
    ]

    async def lookup(url):
        return video_info_with(formats)

    commands: list[list[str]] = []
    spawn = runner_module.spawn

    async def gone_until_replaced(args, cwd=None, **kwargs):
        commands.append(list(args))
        if args[args.index("-f") + 1] == "gone":  # the fake yt-dlp's stale-format error
            args = [*args[:-1], "fake://format-gone"]
            args[args.index("-f") + 1] = "gone+140"
        else:
            args = [*args[:-1], "fake://success"]
        return await spawn(args, cwd=cwd, **kwargs)

    monkeypatch.setattr(runner_module, "spawn", gone_until_replaced)
    queue = queue_env.make_queue(lookup=lookup)
    old = FormatChoice(ChoiceKind.AUDIO, "gone", ext="webm", audio_ext="webm", audio_codec="opus")
    job = queue.add(make_spec("fake://audio", queue_env.dest, choice=old))
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert job.state is JobState.DONE and job.refetched
    assert job.spec.choice == FormatChoice(
        ChoiceKind.AUDIO, "251", ext="webm", audio_ext="webm", audio_codec="opus", audio_language="en",
    )
    assert [args[args.index("-f") + 1] for args in commands] == ["gone", "251"]
    assert queue_env.store.load(job.id).spec.choice.format_id == "251"


async def test_format_gone_without_match_fails(queue_env):
    async def lookup(url):
        return video_info_with([])

    queue = queue_env.make_queue(lookup=lookup)
    job = queue.add(make_spec("fake://format-gone", queue_env.dest, choice=GONE))
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert job.state is JobState.FAILED
    assert job.error.kind is ErrorKind.FORMAT_GONE
    assert job.spec.choice == GONE


async def test_format_gone_when_the_lookup_fails(queue_env):
    from snag.errors import make_error

    async def lookup(url):
        raise LookupFailed(make_error(ErrorKind.NETWORK, "youtube"))

    queue = queue_env.make_queue(lookup=lookup)
    job = queue.add(make_spec("fake://format-gone", queue_env.dest, choice=GONE))
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert job.state is JobState.FAILED and job.error.kind is ErrorKind.FORMAT_GONE


async def test_cancel_during_the_lookup_stops_the_job(queue_env):
    started, never = asyncio.Event(), asyncio.Event()

    async def lookup(url):
        started.set()
        await never.wait()

    queue = queue_env.make_queue(lookup=lookup)
    job = queue.add(make_spec("fake://format-gone", queue_env.dest, choice=GONE))
    await asyncio.wait_for(started.wait(), 15)
    await queue.cancel(job.id)
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert job.state is JobState.CANCELLED
    assert not queue_env.work_of(job).exists()


async def test_shutdown_during_the_lookup_interrupts_the_job(queue_env):
    started, never = asyncio.Event(), asyncio.Event()

    async def lookup(url):
        started.set()
        await never.wait()

    queue = queue_env.make_queue(lookup=lookup)
    job = queue.add(make_spec("fake://format-gone", queue_env.dest, choice=GONE))
    await asyncio.wait_for(started.wait(), 15)
    await asyncio.wait_for(queue.shutdown(), 15)
    assert job.state is JobState.INTERRUPTED
    assert queue_env.store.load(job.id).state is JobState.INTERRUPTED


def spy_commands(monkeypatch) -> list[list[str]]:
    import snag.jobs.runner as runner_module

    commands: list[list[str]] = []
    spawn = runner_module.spawn

    async def spying(args, cwd=None, **kwargs):
        commands.append(list(args))
        return await spawn(args, cwd=cwd, **kwargs)

    monkeypatch.setattr(runner_module, "spawn", spying)
    return commands


async def test_a_job_that_needed_cookies_keeps_them_for_the_closest_match(queue_env, monkeypatch):
    commands = spy_commands(monkeypatch)
    asked = []

    async def lookup(url):
        return video_info_with([M4A, video("299", 1080, "avc1.64002A")])

    queue = queue_env.make_queue(lookup=lookup, cookies=lambda url: asked.append(url) or COOKIES)
    queue.pause()
    job = queue.add(make_spec("fake://format-gone", queue_env.dest, choice=GONE))
    job.used_cookies = True  # e.g. resumed after a restart: an earlier attempt needed sign-in
    queue.resume()
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert job.state is JobState.DONE and job.spec.choice.format_id == "299"
    assert len(commands) == 2 and all("--cookies" in args for args in commands)
    assert asked == ["fake://format-gone"]  # asked once per run, reused for the second attempt


# --- stale download links (HTTP 403) ---

async def test_a_403_is_retried_once_with_a_fresh_run(queue_env):
    """YouTube answers 403 when the links yt-dlp extracted have gone stale; a fresh run extracts new ones."""
    queue = queue_env.make_queue()
    recorder = Recorder()
    queue.subscribe(recorder)
    job = queue.add(make_spec(f"fake://forbidden-once/{uuid.uuid4().hex}", queue_env.dest))
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert job.state is JobState.DONE and job.error is None
    assert attempts(recorder, job) == 2


async def test_a_403_that_happens_again_fails_after_one_retry(queue_env):
    queue = queue_env.make_queue()
    recorder = Recorder()
    queue.subscribe(recorder)
    job = queue.add(make_spec("fake://forbidden", queue_env.dest))
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert job.state is JobState.FAILED
    assert job.error.kind is ErrorKind.SITE_CHANGED and "HTTP Error 403" in job.error.details
    assert attempts(recorder, job) == 2


async def test_a_403_is_retried_again_in_the_jobs_next_run(queue_env):
    """Once per run of the job: the user's Retry starts a run that gets its own retry."""
    queue = queue_env.make_queue()
    recorder = Recorder()
    queue.subscribe(recorder)
    job = queue.add(make_spec("fake://forbidden", queue_env.dest))
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert attempts(recorder, job) == 2
    queue.retry(job.id)
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert job.state is JobState.FAILED and attempts(recorder, job) == 4


async def test_other_site_changes_are_not_retried(queue_env):
    queue = queue_env.make_queue()
    recorder = Recorder()
    queue.subscribe(recorder)
    job = queue.add(make_spec("fake://nsig", queue_env.dest))
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert job.state is JobState.FAILED and job.error.kind is ErrorKind.SITE_CHANGED
    assert attempts(recorder, job) == 1


# --- throttling ---

async def test_throttle_pauses_then_runs_one_at_a_time(queue_env):
    queue = queue_env.make_queue(throttle_pause=0.3, throttle_reduced=60.0)
    recorder = Recorder()
    queue.subscribe(recorder)
    job = queue.add(make_spec(f"fake://throttle-once/{uuid.uuid4().hex}", queue_env.dest))
    await wait_for(lambda: queue.throttled)
    throttled_at = time.monotonic()
    assert job.state is JobState.WAITING
    assert job.stage == "YouTube is slowing you down. Paused for 1 minute."  # 0.3 s, rounded up
    assert queue_env.store.load(job.id).state is JobState.WAITING
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert job.state is JobState.DONE
    assert time.monotonic() - throttled_at >= 0.25
    assert queue.current_limit == 1
    states = recorder.states(job.id)
    assert states.index(JobState.WAITING, 1) > states.index(JobState.FETCHING)
    assert queue_env.history.list()[0].status == "done"  # the throttled attempt isn't a failure


@pytest.mark.parametrize(("pause", "said"), [
    (300.0, "5 minutes"), (600.0, "10 minutes"), (90.0, "2 minutes"), (60.0, "1 minute"),
])
async def test_the_throttle_message_says_how_long_the_queue_pauses(queue_env, pause, said):
    queue = queue_env.make_queue(throttle_pause=pause)
    job = queue.add(make_spec(f"fake://throttle-once/{uuid.uuid4().hex}", queue_env.dest))
    await wait_for(lambda: queue.throttled)
    assert job.stage == f"YouTube is slowing you down. Paused for {said}."
    await asyncio.wait_for(queue.shutdown(), 5)


async def test_throttled_job_goes_back_to_the_front(queue_env):
    queue = queue_env.make_queue(max_concurrent=1, throttle_pause=0.3, throttle_reduced=60.0)
    order: list[str] = []
    queue.subscribe(lambda job: job.state is JobState.FETCHING and order.append(job.id))
    throttled = queue.add(make_spec(f"fake://throttle-once/{uuid.uuid4().hex}", queue_env.dest))
    other = queue.add(make_spec("fake://success", queue_env.dest))
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert throttled.state is JobState.DONE and other.state is JobState.DONE
    assert order == [throttled.id, throttled.id, other.id]


async def test_normal_limit_returns_on_time_after_the_reduced_period(queue_env):
    loop = asyncio.get_running_loop()
    skew = [0.0]  # a clock must track loop.time(); this one may also be moved ahead to skip a wait
    queue = queue_env.make_queue(
        max_concurrent=2, throttle_pause=0.2, throttle_reduced=2.0, clock=lambda: loop.time() + skew[0],
    )
    throttled = queue.add(make_spec(f"fake://throttle-once/{uuid.uuid4().hex}", queue_env.dest))
    await wait_for(lambda: queue.throttled)
    first = queue.add(make_spec("fake://slow", queue_env.dest))
    second = queue.add(make_spec("fake://slow", queue_env.dest))
    await wait_for(lambda: first.state is JobState.DOWNLOADING)
    assert throttled.state is JobState.DONE and not queue.throttled
    assert queue.current_limit == 1 and second.state is JobState.WAITING
    # Nothing finishes from here on: only the queue's timer can start the second job.
    await wait_for(lambda: second.state is JobState.DOWNLOADING, timeout=5)
    assert first.state is JobState.DOWNLOADING and queue.current_limit == 2
    for job in (first, second):
        await queue.cancel(job.id)
    await asyncio.wait_for(queue.wait_idle(), 15)


async def test_a_throttled_job_that_dropped_subtitles_skips_them_when_it_runs_again(queue_env, monkeypatch):
    import snag.jobs.runner as runner_module

    token = uuid.uuid4().hex
    script = [
        "fake://subs-fail",  # the subtitles fail, so the job tries again without them...
        f"fake://throttle-once/{token}",  # ...and is throttled, so it goes back into the queue...
        f"fake://throttle-once/{token}",  # ...and its next run works
    ]
    commands: list[list[str]] = []
    spawn = runner_module.spawn

    async def scripted(args, cwd=None, **kwargs):
        commands.append(list(args))
        return await spawn([*args[:-1], script[len(commands) - 1]], cwd=cwd, **kwargs)

    monkeypatch.setattr(runner_module, "spawn", scripted)
    queue = queue_env.make_queue(throttle_pause=0.2, throttle_reduced=0.2)
    job = queue.add(make_spec("fake://scripted", queue_env.dest, extras=Extras(subtitles=True)))
    await asyncio.wait_for(queue.wait_idle(), 15)
    assert job.state is JobState.DONE
    assert ["--write-subs" in args for args in commands] == [True, False, False]
    assert job.subtitles_skipped is True
    assert queue_env.history.list()[0].format_summary == "1080p MP4 + saved without subtitles"
