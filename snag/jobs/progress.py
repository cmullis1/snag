"""yt-dlp progress templates and the parser for the lines they produce."""
from __future__ import annotations

import math
from dataclasses import dataclass

DOWNLOAD_TEMPLATE = (
    "download:SNAGDL %(progress.status)s|%(progress.downloaded_bytes)s|%(progress.total_bytes)s"
    "|%(progress.total_bytes_estimate)s|%(progress.speed)s|%(progress.eta)s|%(info.format_id)s"
)
POSTPROCESS_TEMPLATE = "postprocess:SNAGPP %(progress.status)s|%(progress.postprocessor)s"
FILE_PRINT = "after_move:SNAGFILE %(filepath)s"
# The chosen format, e.g. "299+140" (two streams) or "18" (one); printed once, just before downloading.
FORMAT_PRINT = "before_dl:SNAGFMT %(format_id)s"

_PP_LABELS = {
    "Merger": "Joining video and audio…",
    "ExtractAudio": "Converting audio…",
    "EmbedSubtitle": "Embedding subtitles…",
    "SubtitlesConvertor": "Converting subtitles…",
    "EmbedThumbnail": "Adding thumbnail…",
    "ThumbnailsConvertor": "Adding thumbnail…",
    "Metadata": "Adding tags and chapters…",
    "SponsorBlock": "Checking SponsorBlock…",
    "ModifyChapters": "Removing sponsor segments…",
    "VideoRemuxer": "Changing container…",
    "MoveFiles": "Finishing…",
}


@dataclass(frozen=True)
class DownloadEvent:
    status: str
    done: int | None
    total: int | None
    total_estimate: int | None
    speed: float | None
    eta: int | None
    format_id: str


@dataclass(frozen=True)
class PostprocessEvent:
    status: str
    name: str

    @property
    def label(self) -> str:
        return _PP_LABELS.get(self.name, "Processing…")


@dataclass(frozen=True)
class FileEvent:
    path: str


@dataclass(frozen=True)
class FormatEvent:
    format_id: str


Event = DownloadEvent | PostprocessEvent | FileEvent | FormatEvent


def _float(text: str) -> float | None:
    """None for missing or non-finite values; ValueError for text that isn't a number."""
    if text in ("NA", "None", ""):
        return None
    try:
        value = float(text)
    except OverflowError:
        return None
    return value if math.isfinite(value) else None


def _int(text: str) -> int | None:
    value = _float(text)
    try:
        return None if value is None else int(value)
    except OverflowError:
        return None


def parse_line(line: str) -> Event | None:
    if line.startswith("SNAGDL "):
        parts = line[len("SNAGDL "):].split("|")
        if len(parts) != 7:
            return None
        status, done, total, estimate, speed, eta, format_id = parts
        try:
            return DownloadEvent(status, _int(done), _int(total), _int(estimate), _float(speed), _int(eta), format_id)
        except (ValueError, OverflowError):
            return None
    if line.startswith("SNAGPP "):
        status, _, name = line[len("SNAGPP "):].partition("|")
        return PostprocessEvent(status, name)
    if line.startswith("SNAGFILE "):
        return FileEvent(line[len("SNAGFILE "):])
    if line.startswith("SNAGFMT "):
        return FormatEvent(line[len("SNAGFMT "):].strip())
    return None


def _parts(format_id: str) -> int:
    return format_id.count("+") + 1


class ProgressTracker:
    """Combines per-stream progress (video and audio download separately) into one percentage.

    expected_streams is a guess from the job's choice; set_streams corrects it from yt-dlp's
    SNAGFMT line. A stream whose id has "+" (one ffmpeg run, as for trims) covers all its parts.
    """

    def __init__(self, expected_streams: int = 1):
        self._expected = max(1, expected_streams)
        self._streams: dict[str, tuple[int, int | None]] = {}
        self._best: float | None = None
        self.speed: float | None = None
        self.eta: int | None = None

    def set_streams(self, format_id: str) -> None:
        if format_id and format_id != "NA":
            self._expected = _parts(format_id)
            self._advance()

    def update(self, event: DownloadEvent) -> None:
        if event.format_id == "NA":
            # Not part of the video: with --write-subs yt-dlp downloads the subtitle files first, and
            # their events have no format id.
            return
        total = event.total or event.total_estimate
        done = event.done
        if done is None and event.status == "finished":
            done = total  # a file that was already complete is reported finished with no byte count
        self._streams[event.format_id] = (done or 0, total)
        if event.status == "downloading":
            self.speed, self.eta = event.speed, event.eta
        self._advance()

    def _advance(self) -> None:
        current = self._compute()
        if current is not None and (self._best is None or current > self._best):
            self._best = current

    def _compute(self) -> float | None:
        totals = [total for _, total in self._streams.values()]
        if not totals or any(total is None for total in totals):
            return None
        total = sum(totals)
        if total == 0:
            return None
        done = sum(min(d, t) for d, t in self._streams.values())
        coverage = min(1.0, sum(_parts(format_id) for format_id in self._streams) / self._expected)
        return round(100 * done / total * coverage, 1)

    @property
    def percent(self) -> float | None:
        return self._best
