"""Every API payload as plain JSON (dicts, lists, str, numbers, bools, None), and the download request
parsed back into the engine's types. Pure functions: no I/O."""
from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import fields, is_dataclass, replace
from enum import Enum
from pathlib import Path, PurePath
from typing import Any

from snag.checks import ToolStatus
from snag.errors import ErrorInfo
from snag.info.models import FormatRow, PlaylistInfo, VideoInfo
from snag.jobs.models import (
    Extras, FormatChoice, Job, JobState, choice_for_row, extras_from_dict, job_to_dict, validate_extras,
)
from snag.jobs.queue import BatchSummary, QueueState
from snag.store.history import HistoryEntry, format_summary
from snag.store.settings import validate_folder

_RETRYABLE_STATES = (JobState.FAILED, JobState.CANCELLED)  # what JobQueue.retry accepts
_CLEARABLE_STATES = (JobState.DONE, JobState.FAILED, JobState.CANCELLED)  # what JobQueue.clear accepts


def _plain(value: Any) -> Any:
    """Dataclasses as dicts, tuples as lists, enums as their values, paths as text, and NaN and the
    infinities (yt-dlp's JSON can carry them; strict JSON can't) as None."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _plain(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, PurePath):
        return str(value)
    return value


# --- lookups ---

def format_row_json(row: FormatRow) -> dict[str, Any]:
    return _plain(row)


def history_entry_json(entry: HistoryEntry) -> dict[str, Any]:
    return _plain(entry)


def _duplicate_json(entry: HistoryEntry | None) -> dict[str, Any] | None:
    return history_entry_json(entry) if entry is not None else None


def video_info_json(info: VideoInfo, duplicate: HistoryEntry | None) -> dict[str, Any]:
    """duplicate: the latest successful download of this video, for "already downloaded" (or None)."""
    formats = info.formats
    return _plain({
        "type": "video",
        "site": info.site,
        "video_id": info.video_id,
        "url": info.url,
        "title": info.title,
        "channel": info.channel,
        "duration": info.duration,
        "thumbnail": info.thumbnail,
        "upload_date": info.upload_date,
        "formats": {
            name: [format_row_json(row) for row in getattr(formats, name)]
            for name in ("video_simple", "video_all", "audio", "audio_tracks")
        },
        "subtitle_langs": list(info.subtitle_langs),
        "auto_subtitle_langs": list(info.auto_subtitle_langs),
        "has_chapters": info.has_chapters,
        "playlist": _plain(info.playlist) if info.playlist is not None else None,
        "live_status": info.live_status,
        "js_runtime_missing": info.js_runtime_missing,
        "duplicate": _duplicate_json(duplicate),
    })


def playlist_info_json(info: PlaylistInfo, duplicates: Mapping[str, HistoryEntry] | None = None) -> dict[str, Any]:
    """duplicates: video id -> its latest successful download, for the picker's "already downloaded"
    marks; entries not in it (or all, without it) have none."""
    duplicates = duplicates or {}
    return _plain({
        "type": "playlist",
        "site": info.site,
        "playlist_id": info.playlist_id,
        "url": info.url,
        "title": info.title,
        "entries": [
            {**_plain(entry), "duplicate": _duplicate_json(duplicates.get(entry.video_id))}
            for entry in info.entries
        ],
    })


# --- jobs, queue, tools, errors ---

def job_json(job: Job) -> dict[str, Any]:
    """job_to_dict plus what the UI shows and offers: kind ("video" or "audio"), summary (as History will
    word it), and whether Retry (can_retry) and the per-job Clear (can_clear) apply. Done jobs can be
    cleared one by one too, or with the others by clear-finished."""
    data = _plain(job_to_dict(job))
    retryable = job.state in _RETRYABLE_STATES
    data.update(
        kind="audio" if job.spec.choice.audio_only else "video",
        summary=format_summary(job.spec, job.file_path, subtitles_skipped=job.subtitles_skipped),
        can_retry=retryable and (job.error is None or job.error.can_retry),
        can_clear=job.state in _CLEARABLE_STATES,
    )
    return data


def queue_state_json(state: QueueState) -> dict[str, Any]:
    return _plain(state)


def batch_summary_json(summary: BatchSummary) -> dict[str, Any]:
    return _plain(summary)


def tool_status_json(status: ToolStatus) -> dict[str, Any]:
    """Each tool's {present, version, message}, plus ready: downloads can run (Deno is optional)."""
    return {**_plain(status), "ready": status.ready}


def error_json(error: ErrorInfo) -> dict[str, Any]:
    return _plain(error.to_dict())


# --- requests ---

def _rows_by_id(rows: tuple[FormatRow, ...]) -> dict[str, FormatRow]:
    return {row.format_id: row for row in rows}


def _dest_dir(value: Any) -> Path | None:
    return None if value is None else validate_folder(value)


class InvalidExtras(ValueError):
    """Extras a request can't have: message is a sentence for the user saying what to fix, details the
    engine's own reason (for the log and the error's details)."""

    def __init__(self, message: str, details: str = ""):
        super().__init__(message)
        self.message = message
        self.details = details


EXTRAS_KEYS = frozenset(f.name for f in fields(Extras))
MAX_TRIM = 32  # characters in a trim time
TRIM_FORMAT = "Trim times look like 1:30 or 1:02:03."
TRIM_ORDER = "The trim end must come after the start."
PICK_CATEGORY = "Pick at least one SponsorBlock category."
NOT_VALID = "These extras aren't valid. Open the extras and choose them again."


def _broken(extras: Extras) -> str | None:
    """validate_extras' reason extras are bad, or None."""
    try:
        validate_extras(extras)
    except (ValueError, OverflowError) as error:
        return str(error)
    return None


def parse_extras(value: Any) -> Extras:
    """Extras as the UI sends them (an object; missing fields take their defaults; None is no extras)
    -> Extras. Raises InvalidExtras with a plain message: the same for a download, a playlist and the
    settings, so the UI can show it by the extras (error kind invalid_extras)."""
    if value is None:
        return Extras()
    if not isinstance(value, dict):
        raise InvalidExtras(NOT_VALID, f"extras must be an object, not {type(value).__name__}")
    unknown = sorted(str(key) for key in set(value) - EXTRAS_KEYS)
    if unknown:
        raise InvalidExtras(f"Snag doesn't know these extras: {', '.join(unknown)}.", f"unknown extras: {unknown}")
    try:
        extras = extras_from_dict(value)
    except ValueError as error:
        raise InvalidExtras(NOT_VALID, str(error)) from None
    for name in ("trim_start", "trim_end"):
        time = getattr(extras, name)
        if time is not None and (not isinstance(time, str) or len(time) > MAX_TRIM
                                 or _broken(replace(Extras(), **{name: time})) is not None):
            raise InvalidExtras(TRIM_FORMAT, f"{name}: {time!r}")
    reason = _broken(replace(Extras(), trim_start=extras.trim_start, trim_end=extras.trim_end))
    if reason is not None:
        raise InvalidExtras(TRIM_ORDER, reason)
    if extras.sponsorblock and not extras.sponsorblock_categories:
        raise InvalidExtras(PICK_CATEGORY, "sponsorblock_categories is empty")
    reason = _broken(extras)
    if reason is not None:
        raise InvalidExtras(NOT_VALID, reason)
    return extras


def parse_job_request(body: Any, info: VideoInfo) -> tuple[FormatChoice, Extras, Path | None]:
    """A download request -> (choice, extras, destination folder or None for the default).

    body: {"format_id": a row of the lookup's video_all, audio or audio_tracks, or "mp3";
    "audio_id": optional, an audio track to use instead of a video row's paired one;
    "extras": optional, extras as JSON (see parse_extras); "dest_dir": optional, a full folder
    path}. Raises ValueError with a plain message: InvalidExtras for the extras."""
    if not isinstance(body, dict):
        raise ValueError("The download request must be a JSON object")
    format_id = body.get("format_id")
    formats = info.formats
    row = _rows_by_id((*formats.video_all, *formats.audio)).get(format_id) if isinstance(format_id, str) else None
    if row is None:
        raise ValueError("That format isn't offered for this video. Look the video up again and pick another.")
    audio_id = body.get("audio_id")
    audio_row = None
    if audio_id is not None:
        audio_row = _rows_by_id(formats.audio_tracks).get(audio_id) if isinstance(audio_id, str) else None
        if audio_row is None:
            raise ValueError("That audio track isn't offered for this video. Look the video up again.")
    try:
        choice = choice_for_row(row, audio_row)
    except ValueError as error:
        raise ValueError("Only a video format without its own audio can take another audio track") from error
    return choice, parse_extras(body.get("extras")), _dest_dir(body.get("dest_dir"))
