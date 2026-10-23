"""What an info lookup returns to the UI. Lookups are cached and shared between callers,
so every collection is a tuple."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FormatRow:
    """One choosable format. For video rows, `language`/`is_original` describe the audio that
    comes with the row (the paired track, or the format's own audio when combined); `tbr` and
    `dynamic_range` always describe the format itself. `ext` is the format's own container;
    `result_ext` is the one the finished file gets (a merge or an audio extraction can change it)."""

    format_id: str
    label: str
    ext: str
    vcodec: str | None = None
    acodec: str | None = None
    height: int | None = None
    fps: float | None = None
    abr: float | None = None
    size: int | None = None
    size_is_estimate: bool = False
    audio_id: str | None = None
    has_audio: bool = False
    language: str | None = None
    is_original: bool = False
    tbr: float | None = None
    dynamic_range: str | None = None
    upscaled: bool = False
    kind: str = "video"  # "video", "audio" (an original audio format) or "mp3" (best audio converted to MP3)
    audio_ext: str | None = None  # video rows: the paired audio track's container and codec
    audio_codec: str | None = None
    result_ext: str | None = None  # None when it can't be known in advance


@dataclass(frozen=True)
class FormatLists:
    video_simple: tuple[FormatRow, ...]
    video_all: tuple[FormatRow, ...]
    audio: tuple[FormatRow, ...]
    audio_tracks: tuple[FormatRow, ...]


@dataclass(frozen=True)
class PlaylistRef:
    playlist_id: str
    url: str


@dataclass(frozen=True)
class VideoInfo:
    site: str
    video_id: str
    url: str
    title: str
    channel: str | None
    duration: float | None
    thumbnail: str | None
    upload_date: str | None
    formats: FormatLists
    subtitle_langs: tuple[str, ...]
    auto_subtitle_langs: tuple[str, ...]
    has_chapters: bool
    playlist: PlaylistRef | None = None
    live_status: str | None = None  # yt-dlp's value: "not_live", "is_live", "is_upcoming", "was_live", "post_live"
    # yt-dlp warned it had no JavaScript runtime (Deno missing or broken), so some formats may be missing.
    js_runtime_missing: bool = False


@dataclass(frozen=True)
class PlaylistEntry:
    index: int
    video_id: str
    url: str
    title: str
    duration: float | None
    thumbnail: str | None
    # Set only for an entry without a URL of its own (a part of a "multi_video" page): `url` is then the
    # page, and the entry is downloaded with --playlist-items <item_index> (1-based).
    item_index: int | None = None


@dataclass(frozen=True)
class PlaylistInfo:
    site: str
    playlist_id: str
    url: str
    title: str
    entries: tuple[PlaylistEntry, ...]
