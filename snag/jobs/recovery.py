"""How a job recovers from a failed attempt: retrying without subtitles, with cookies, with the
closest replacement for a format that is no longer offered, or afresh after a stale link's 403."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from typing import TypeVar

from snag.errors import ErrorKind, http_forbidden
from snag.info.formats import best_quality, codec_family
from snag.info.lookup import LookupFailed
from snag.info.models import FormatRow, PlaylistInfo, VideoInfo

from .models import ChoiceKind, FormatChoice, Job, choice_for_row
from .runner import AttemptResult, JobRunner, Outcome

log = logging.getLogger(__name__)

# Must look the URL up afresh (not from a cache), e.g. lambda url: lookup_service.get(url, fresh=True).
LookupFn = Callable[[str], Awaitable[VideoInfo | PlaylistInfo]]
CookieProvider = Callable[[str], str | None]
T = TypeVar("T")


def _tiers(old: FormatChoice, rows: list[FormatRow]) -> list[list[FormatRow]]:
    """Candidates from closest to loosest: same height + container + codec family, then same height +
    container, then same height."""
    ext = (old.ext or "").lower()
    family = codec_family(old.vcodec)
    same_ext = [r for r in rows if ext and r.ext.lower() == ext]
    same_family = [r for r in same_ext if family and codec_family(r.vcodec) == family]
    return [same_family, same_ext, rows]


def closest_choice(old: FormatChoice, info: VideoInfo | PlaylistInfo) -> FormatChoice | None:
    """The closest format to `old` (a VIDEO or AUDIO choice) in a fresh lookup, or None when there is
    none worth trying. The choice is built like the format list's own (choice_for_row), so it records
    the audio's container and codec.

    VIDEO: the best-quality format of the closest tier (see _tiers) wins. A chosen audio track that is
    still offered is kept (it may be a deliberately picked dub). Otherwise the format list's automatic
    pairing is used, unless the choice recorded an audio language that pairing isn't in: then the
    closest track in that language (see _closest_audio), when one is offered. AUDIO: see _closest_audio.

    None is also returned when the match is the same format(s) as `old`, since retrying would fail the
    same way. Other kinds (MP3, QUICK, BEST_AUDIO) let yt-dlp pick the formats, so they have no match.
    """
    if not isinstance(info, VideoInfo):
        return None
    if old.kind is ChoiceKind.AUDIO:
        row = _closest_audio(
            info.formats.audio_tracks, old.audio_ext or old.ext, old.audio_codec, old.audio_language,
        )
        choice = choice_for_row(row) if row is not None else None
    elif old.kind is ChoiceKind.VIDEO and old.height is not None:
        choice = _closest_video(old, info)
    else:
        return None
    if choice is None or (choice.format_id, choice.audio_id) == (old.format_id, old.audio_id):
        return None
    return choice


def _closest_video(old: FormatChoice, info: VideoInfo) -> FormatChoice | None:
    rows = [r for r in info.formats.video_all if r.height == old.height]
    tier = next((t for t in _tiers(old, rows) if t), None)
    if tier is None:
        return None
    row = best_quality(tier)
    if row.has_audio:
        return choice_for_row(row)  # a combined format has its own sound
    tracks = info.formats.audio_tracks
    audio = next((a for a in tracks if a.format_id == old.audio_id), None)
    if audio is None and old.audio_language and not _same_language(row.language, old.audio_language):
        in_language = [a for a in tracks if _same_language(a.language, old.audio_language)]
        audio = _closest_audio(in_language, old.audio_ext, old.audio_codec, old.audio_language)
    if audio is None:
        return choice_for_row(row)  # the automatic pairing
    return choice_for_row(row, audio)


def _same_language(language: str | None, wanted: str) -> bool:
    return language is not None and language.lower() == wanted.lower()


def _audio_codec(acodec: str | None) -> str | None:
    """"mp4a.40.2" -> "mp4a", "opus" -> "opus"; None when unknown."""
    if not acodec or acodec == "none":
        return None
    return acodec.split(".")[0].lower()


def _closest_audio(
    tracks: Sequence[FormatRow], ext: str | None, acodec: str | None, language: str | None,
) -> FormatRow | None:
    """The closest audio format to one in container `ext`, codec `acodec` and `language`: the same
    container and codec, else the same codec, else the best one; the highest bitrate within the first
    of those that offers any.

    Only tracks in `language` are considered when it's known and offered (a deliberately chosen dub
    stays a dub); otherwise only original-language tracks (all of them when the site doesn't say which
    is original), since the original is what the format list offers first, so a dub or described audio
    never replaces it. A choice saved before codecs were recorded is matched by container alone."""
    in_language = [t for t in tracks if language and _same_language(t.language, language)]
    pool = in_language or [t for t in tracks if t.is_original] or list(tracks)
    ext, codec = (ext or "").lower(), _audio_codec(acodec)
    same_codec = [t for t in pool if codec and _audio_codec(t.acodec) == codec]
    same_both = [t for t in same_codec if t.ext.lower() == ext]
    same_ext = [t for t in pool if ext and t.ext.lower() == ext] if codec is None else []
    tier = next((t for t in (same_both, same_codec, same_ext, pool) if t), None)
    if tier is None:
        return None
    return max(tier, key=lambda t: (t.abr or t.tbr or 0, t.size or 0))


class _Stopped(Exception):
    """The job was stopped (cancelled, or the queue began closing) while waiting on something."""


class JobRun:
    """One run of a job: its first attempt, then each recovery that applies, tried once and in order
    (spec 4.4 "Subtitle failures", 5.2), until an attempt no longer fails or the job is stopped.

    Throttling isn't recovered here: pausing affects the whole queue, so the queue handles it.
    """

    def __init__(
        self,
        job: Job,
        runner: JobRunner,
        *,
        cookies: CookieProvider,
        lookup: LookupFn,
        stop: asyncio.Event,
        save: Callable[[Job], None],
        without_subtitles: bool = False,
    ):
        self._job = job
        self._runner = runner
        self._cookies = cookies
        self._lookup = lookup
        self._stop = stop
        self._save = save
        self._cookies_text: str | None = None
        self.without_subtitles = without_subtitles
        self._forbidden_retried = False  # the one fresh attempt after a 403 this run may make
        # Stopped before a recovery could run: the next start should resume the job, not fail it.
        self.recovery_left = False

    async def execute(self) -> AttemptResult:
        job = self._job
        if job.used_cookies:  # resumed after an attempt that needed them
            self._cookies_text = self._cookies(job.spec.url)
        result = await self._attempt()
        while result.outcome is Outcome.FAILED:
            step = self._recovery_for(result)
            if step is None:
                return result
            if self._stop.is_set():
                self.recovery_left = True
                return result
            try:
                retried = await step()
            except _Stopped:
                self.recovery_left = True
                return result
            if retried is None:  # nothing to retry with after all; keep the error
                return result
            result = retried
        return result

    async def _attempt(self) -> AttemptResult:
        if self._stop.is_set():
            return AttemptResult(Outcome.INTERRUPTED)  # the queue turns a user's cancel into CANCELLED
        return await self._runner.run(self._job, self._cookies_text, without_subtitles=self.without_subtitles)

    def _recovery_for(self, result: AttemptResult) -> Callable[[], Awaitable[AttemptResult | None]] | None:
        job = self._job
        kind = result.error.kind if result.error else None
        # Checked first: yt-dlp's subtitle error text can contain "HTTP Error 429".
        if result.subtitle_failure and not self.without_subtitles:
            return self._without_subtitles
        if kind is ErrorKind.SIGN_IN and not job.used_cookies:
            return self._with_cookies
        if kind is ErrorKind.FORMAT_GONE and not job.refetched:
            return self._with_closest_format
        if http_forbidden(result.error) and not self._forbidden_retried:
            return self._afresh
        return None

    async def _without_subtitles(self) -> AttemptResult:
        self.without_subtitles = True
        return await self._attempt()

    async def _afresh(self) -> AttemptResult:
        """Once per run: yt-dlp runs again, extracting fresh download links (spec 5.2). The partial
        download is kept, so the new attempt resumes it."""
        self._forbidden_retried = True
        log.info("Job %s: the site answered 403 (a stale download link); trying once more afresh", self._job.id)
        return await self._attempt()

    async def _with_cookies(self) -> AttemptResult | None:
        cookies = self._cookies(self._job.spec.url)
        if cookies is None:
            return None
        self._job.used_cookies, self._cookies_text = True, cookies
        return await self._attempt()

    async def _with_closest_format(self) -> AttemptResult | None:
        job = self._job
        job.refetched = True
        try:
            info = await self._unless_stopped(self._lookup(job.spec.url))
        except LookupFailed as error:
            log.info("Couldn't look up %s again for a replacement format: %s", job.spec.url, error)
            return None
        choice = closest_choice(job.spec.choice, info)
        if choice is None:
            return None
        job.spec = replace(job.spec, choice=choice)
        self._save(job)
        return await self._attempt()

    async def _unless_stopped(self, awaitable: Awaitable[T]) -> T:
        """Await something that isn't a download (a lookup), giving up as soon as the job is stopped."""
        work = asyncio.ensure_future(awaitable)
        stopper = asyncio.ensure_future(self._stop.wait())
        try:
            await asyncio.wait({work, stopper}, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            work.cancel()
            raise
        finally:
            stopper.cancel()
        if work.done():
            return work.result()
        work.cancel()
        work.add_done_callback(lambda done: done.cancelled() or done.exception())  # never "unretrieved"
        raise _Stopped
