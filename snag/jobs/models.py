"""A download job: what to fetch, how, where, and how it's going.

Decoding (the *_from_dict functions) is tolerant of schema drift: unknown keys are ignored and missing
optional keys take their defaults. Missing or mistyped required fields raise ValueError.
"""
from __future__ import annotations

import math
import re
import time
import uuid
from dataclasses import dataclass, field, fields
from enum import StrEnum
from pathlib import Path
from typing import Any

from snag.errors import ErrorInfo
from snag.info.models import FormatRow


class JobState(StrEnum):
    WAITING = "waiting"
    FETCHING = "fetching"
    DOWNLOADING = "downloading"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


ACTIVE_STATES = frozenset({JobState.FETCHING, JobState.DOWNLOADING, JobState.PROCESSING})
UNFINISHED_STATES = ACTIVE_STATES | {JobState.WAITING, JobState.INTERRUPTED}
END_STATES = frozenset({JobState.DONE, JobState.FAILED, JobState.CANCELLED})


class ChoiceKind(StrEnum):
    VIDEO = "video"  # a specific video format (+ audio track)
    AUDIO = "audio"  # a specific original audio format
    MP3 = "mp3"      # best audio converted to MP3
    QUICK = "quick"  # quick-download default: best video, up to a height when one is given
    BEST_AUDIO = "best_audio"  # best original-language audio as it is, no conversion (playlists)


@dataclass(frozen=True)
class FormatChoice:
    kind: ChoiceKind
    format_id: str | None = None
    audio_id: str | None = None
    height: int | None = None  # VIDEO: chosen height (for closest-match recovery); QUICK: maximum height (None: no limit)
    ext: str | None = None     # VIDEO/AUDIO: chosen container
    vcodec: str | None = None  # VIDEO: the format's codec (e.g. "avc1.64002A"), so recovery can keep its family
    # The separately downloaded audio's container and codec (VIDEO: the audio track; AUDIO: the format
    # itself), which decide the finished file's container. None when not known (older saved jobs).
    audio_ext: str | None = None
    audio_codec: str | None = None
    # VIDEO/AUDIO: the language of the audio used (the paired, chosen or combined track, or the audio
    # format itself), so recovery can keep a deliberately chosen dub. None when not known.
    audio_language: str | None = None

    @property
    def audio_only(self) -> bool:
        return self.kind in (ChoiceKind.AUDIO, ChoiceKind.MP3, ChoiceKind.BEST_AUDIO)


def choice_for_row(row: FormatRow, audio_row: FormatRow | None = None) -> FormatChoice:
    """The choice for downloading a row of the format list. `audio_row` replaces a video row's paired
    audio track (the expanded view's Audio track dropdown); it must be an original audio row, and a
    format that already includes audio can't take one. Raises ValueError otherwise."""
    if audio_row is not None and (row.kind != "video" or row.has_audio or audio_row.kind != "audio"):
        raise ValueError("only a video format without its own audio can take another audio track")
    match row.kind:
        case "mp3":
            return FormatChoice(ChoiceKind.MP3)
        case "audio":
            return FormatChoice(
                ChoiceKind.AUDIO, row.format_id, ext=row.ext, audio_ext=row.ext, audio_codec=row.acodec,
                audio_language=row.language,
            )
        case "video":
            sound = audio_row or row  # a video row's language is its paired (or own) audio's
            if audio_row is not None:
                audio_id, audio_ext, audio_codec = audio_row.format_id, audio_row.ext, audio_row.acodec
            else:
                audio_id, audio_ext, audio_codec = row.audio_id, row.audio_ext, row.audio_codec
            return FormatChoice(
                ChoiceKind.VIDEO, row.format_id, audio_id, row.height, row.ext, row.vcodec, audio_ext, audio_codec,
                sound.language,
            )
    raise ValueError(f"unknown format row kind: {row.kind!r}")


class SubtitleMode(StrEnum):
    EMBEDDED = "embed"
    FILE = "file"


class SponsorBlockMode(StrEnum):
    REMOVE = "remove"
    MARK = "mark"


# yt-dlp's SponsorBlockPP.CATEGORIES (Snag can't import yt-dlp at runtime; a test checks they match).
SPONSORBLOCK_CATEGORIES = (
    "sponsor", "intro", "outro", "selfpromo", "preview", "filler", "interaction", "music_offtopic", "hook",
    "poi_highlight", "chapter",
)
# --sponsorblock-remove refuses the non-skippable ones (a highlight point and chapter names).
SPONSORBLOCK_REMOVABLE = tuple(c for c in SPONSORBLOCK_CATEGORIES if c not in ("poi_highlight", "chapter"))

# seconds, M:SS or H:MM:SS with an optional fraction; the first field is unbounded ("90", "125:00"),
# the minutes and seconds after it are 0-59.
_TRIM_TIME = re.compile(r"[0-9]+(:[0-5]?[0-9]){0,2}(\.[0-9]+)?")


@dataclass(frozen=True)
class Extras:
    subtitles: bool = False
    subtitle_lang: str = "en"
    subtitle_mode: SubtitleMode = SubtitleMode.EMBEDDED
    thumbnail_tags: bool = False
    chapters: bool = False
    sponsorblock: bool = False
    sponsorblock_mode: SponsorBlockMode = SponsorBlockMode.REMOVE
    sponsorblock_categories: tuple[str, ...] = ("sponsor", "selfpromo", "interaction")
    trim_start: str | None = None  # "90", "1:30", "1:02:03.5"
    trim_end: str | None = None

    def __post_init__(self) -> None:
        # Accept the plain strings JSON and the UI produce; an unknown mode raises ValueError.
        object.__setattr__(self, "subtitle_mode", SubtitleMode(self.subtitle_mode))
        object.__setattr__(self, "sponsorblock_mode", SponsorBlockMode(self.sponsorblock_mode))
        object.__setattr__(self, "sponsorblock_categories", tuple(self.sponsorblock_categories))


def trim_seconds(text: str) -> float:
    whole, _, fraction = text.partition(".")
    seconds = 0.0
    for part in whole.split(":"):
        seconds = seconds * 60 + int(part)
    return seconds + (float(f"0.{fraction}") if fraction else 0.0)


# What a download really gets of its extras. snag.jobs.command builds yt-dlp's arguments from these and
# History's summary lists what they say, so the two can't disagree.

def is_trimmed(extras: Extras) -> bool:
    return bool(extras.trim_start or extras.trim_end)


def gets_subtitles(extras: Extras, choice: FormatChoice) -> bool:
    """Whether yt-dlp is asked for subtitles: not for audio, and not for a trim that starts late
    (yt-dlp doesn't shift subtitle timings for a clip, so they'd be out of sync)."""
    starts_late = bool(extras.trim_start) and trim_seconds(extras.trim_start) > 0
    return extras.subtitles and not choice.audio_only and not starts_late


def gets_sponsorblock(extras: Extras) -> bool:
    """Whether SponsorBlock is applied: it needs categories, and is skipped for any trim (its segments
    use the full video's timestamps)."""
    return extras.sponsorblock and bool(extras.sponsorblock_categories) and not is_trimmed(extras)


def gets_chapters(extras: Extras) -> bool:
    """Whether chapters are embedded: skipped for any trim (they use the full video's timestamps)."""
    return extras.chapters and not is_trimmed(extras)


def summary_base(choice: FormatChoice, ext: str) -> str:
    """What was downloaded, as History's summary starts: "1080p MP4", "Original audio OPUS", "MP3".
    ext is the container as shown to people ("MP4"), or "" when it isn't known."""
    match choice.kind:
        case ChoiceKind.MP3:
            base = "MP3"
        case ChoiceKind.AUDIO:
            base = f"Audio {ext}"
        case ChoiceKind.BEST_AUDIO:
            base = f"Original audio {ext}"
        case ChoiceKind.QUICK:
            base = f"Up to {choice.height}p {ext}" if choice.height else f"Best quality {ext}"
        case _:
            base = f"{choice.height}p {ext}" if choice.height else ext or "Video"
    return base.strip()


def validate_extras(extras: Extras) -> None:
    """Raise ValueError unless every field has the right type and a value yt-dlp accepts."""
    if not isinstance(extras, Extras):
        raise ValueError(f"expected Extras, not {type(extras).__name__}")
    for name in ("subtitles", "thumbnail_tags", "chapters", "sponsorblock"):
        if not isinstance(getattr(extras, name), bool):
            raise ValueError(f"{name} must be true or false")
    if not (isinstance(extras.subtitle_lang, str) and extras.subtitle_lang.strip()):
        raise ValueError("subtitle_lang must be a language code")
    removing = extras.sponsorblock_mode is SponsorBlockMode.REMOVE
    allowed = SPONSORBLOCK_REMOVABLE if removing else SPONSORBLOCK_CATEGORIES
    for category in extras.sponsorblock_categories:
        if category not in allowed:
            raise ValueError(f"SponsorBlock can't {extras.sponsorblock_mode.value} {category!r}")
    if extras.sponsorblock and not extras.sponsorblock_categories:
        raise ValueError("pick at least one SponsorBlock category")
    for name in ("trim_start", "trim_end"):
        value = getattr(extras, name)
        if value is not None and not (isinstance(value, str) and _TRIM_TIME.fullmatch(value)):
            raise ValueError(f"{name} must look like 90, 1:30 or 1:02:03.5")
    if extras.trim_start and extras.trim_end and trim_seconds(extras.trim_start) >= trim_seconds(extras.trim_end):
        raise ValueError("the trim end must come after the start")


@dataclass(frozen=True)
class JobSpec:
    url: str
    title: str
    site: str
    video_id: str
    choice: FormatChoice
    extras: Extras
    dest_dir: Path
    channel: str | None = None
    estimated_size: int | None = None
    batch_id: str | None = None
    playlist_index: int | None = None
    playlist_count: int | None = None
    # A part of a multi-part page that has no URL of its own: downloaded with --playlist-items (1-based).
    playlist_item: int | None = None


def new_id() -> str:
    return uuid.uuid4().hex


_JOB_ID = re.compile(r"[0-9a-f]{32}")


def is_job_id(text: str) -> bool:
    """True for strings shaped like new_id()'s output."""
    return _JOB_ID.fullmatch(text) is not None


@dataclass
class Job:
    spec: JobSpec
    id: str = field(default_factory=new_id)
    state: JobState = JobState.WAITING
    percent: float | None = None
    speed: float | None = None
    eta: int | None = None
    stage: str | None = None
    error: ErrorInfo | None = None
    file_path: Path | None = None
    used_cookies: bool = False
    refetched: bool = False
    subtitles_skipped: bool = False  # the subtitle download failed, so it was saved without them
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None


# --- dict (JSON) conversion ---

def _known(cls: type, data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError(f"expected an object for {cls.__name__}, not {type(data).__name__}")
    names = {f.name for f in fields(cls)}
    return {key: value for key, value in data.items() if key in names}


def _required_str(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be text, not {type(value).__name__}")
    return value


def _check_number(data: dict[str, Any], key: str, *, required: bool = False) -> None:
    """Raise ValueError unless data[key] is a finite int or float (or absent/None when optional)."""
    value = data.get(key)
    if value is None and not required:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} must be a number, not {type(value).__name__}")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{key} must be a finite number, not {value}")


def choice_to_dict(choice: FormatChoice) -> dict[str, Any]:
    return {
        "kind": choice.kind.value, "format_id": choice.format_id, "audio_id": choice.audio_id,
        "height": choice.height, "ext": choice.ext, "vcodec": choice.vcodec,
        "audio_ext": choice.audio_ext, "audio_codec": choice.audio_codec,
        "audio_language": choice.audio_language,
    }


def choice_from_dict(data: Any) -> FormatChoice:
    values = _known(FormatChoice, data)
    values["kind"] = ChoiceKind(_required_str(values, "kind"))
    return FormatChoice(**values)


def extras_to_dict(extras: Extras) -> dict[str, Any]:
    return {
        "subtitles": extras.subtitles,
        "subtitle_lang": extras.subtitle_lang,
        "subtitle_mode": extras.subtitle_mode.value,
        "thumbnail_tags": extras.thumbnail_tags,
        "chapters": extras.chapters,
        "sponsorblock": extras.sponsorblock,
        "sponsorblock_mode": extras.sponsorblock_mode.value,
        "sponsorblock_categories": list(extras.sponsorblock_categories),
        "trim_start": extras.trim_start,
        "trim_end": extras.trim_end,
    }


def extras_from_dict(data: Any) -> Extras:
    try:
        return Extras(**_known(Extras, data))
    except TypeError as error:  # e.g. categories that aren't a list
        raise ValueError(f"not valid extras: {error}") from error


def job_to_dict(job: Job) -> dict[str, Any]:
    spec = job.spec
    return {
        "id": job.id,
        "state": job.state.value,
        "percent": job.percent,
        "speed": job.speed,
        "eta": job.eta,
        "stage": job.stage,
        "error": job.error.to_dict() if job.error else None,
        "file_path": str(job.file_path) if job.file_path else None,
        "used_cookies": job.used_cookies,
        "refetched": job.refetched,
        "subtitles_skipped": job.subtitles_skipped,
        "created_at": job.created_at,
        "finished_at": job.finished_at,
        "spec": {
            "url": spec.url,
            "title": spec.title,
            "site": spec.site,
            "video_id": spec.video_id,
            "choice": choice_to_dict(spec.choice),
            "extras": extras_to_dict(spec.extras),
            "dest_dir": str(spec.dest_dir),
            "channel": spec.channel,
            "estimated_size": spec.estimated_size,
            "batch_id": spec.batch_id,
            "playlist_index": spec.playlist_index,
            "playlist_count": spec.playlist_count,
            "playlist_item": spec.playlist_item,
        },
    }


def _spec_from_dict(data: Any) -> JobSpec:
    values = _known(JobSpec, data)
    for key in ("url", "title", "site", "video_id"):
        _required_str(values, key)
    values["choice"] = choice_from_dict(values.get("choice"))
    values["extras"] = extras_from_dict(values.get("extras"))
    values["dest_dir"] = Path(_required_str(values, "dest_dir"))
    item = values.get("playlist_item")
    # It goes straight onto yt-dlp's command line, where "1-100" would mean a hundred downloads.
    if item is not None and (isinstance(item, bool) or not isinstance(item, int) or item < 1):
        raise ValueError(f"playlist_item must be a whole number from 1, not {item!r}")
    return JobSpec(**values)


def job_from_dict(data: Any) -> Job:
    values = _known(Job, data)
    values["id"] = _required_str(values, "id")
    values["spec"] = _spec_from_dict(values.get("spec"))
    _check_number(values, "created_at", required=True)
    for key in ("finished_at", "percent", "speed", "eta"):
        _check_number(values, key)
    if not isinstance(values.get("subtitles_skipped", False), bool):
        raise ValueError(f"subtitles_skipped must be true or false, not {values['subtitles_skipped']!r}")
    if "state" in values:
        values["state"] = JobState(values["state"])
    if values.get("error") is not None:
        try:
            values["error"] = ErrorInfo.from_dict(values["error"])
        except (KeyError, TypeError, AttributeError) as error:
            raise ValueError(f"not a valid error record: {error!r}") from error
    if values.get("file_path") is not None:
        values["file_path"] = Path(_required_str(values, "file_path"))
    return Job(**values)
