"""Builds the yt-dlp command line for one job."""
from __future__ import annotations

import re
from pathlib import Path

from snag.info.containers import extracted_audio_ext, merged_ext, possible_merged_exts
from snag.paths import Tools

from .models import (
    ChoiceKind, Extras, FormatChoice, JobSpec, SponsorBlockMode, SubtitleMode, gets_chapters, gets_sponsorblock,
    gets_subtitles, is_trimmed,
)
from .progress import DOWNLOAD_TEMPLATE, FILE_PRINT, FORMAT_PRINT, POSTPROCESS_TEMPLATE
from .workdir import MAX_NAME_UNITS, MAX_PATH_UNITS, MIN_STEM_UNITS, utf16_units

# yt-dlp runs with the job's working folder as its current folder and a relative output template, so
# the folder never passes through yt-dlp's %VAR%/~ expansion or its template fields.
# The longest temporary name yt-dlp makes is "<title>.f<format id>.<ext>.part-Frag<n>"; this leaves
# room for everything after the title.
TEMP_SUFFIX_UNITS = 30

# Containers yt-dlp's EmbedThumbnailPP can write cover art into; anything else fails the whole download.
COVER_ART_EXTS = frozenset({"mp4", "m4a", "m4v", "mov", "mkv", "mka", "mp3", "opus", "ogg", "flac"})

# Quick downloads of video always end up as MP4 or MKV: merges via --merge-output-format, and a
# single-file fallback in any other container (WebM, FLV, 3GP...) is remuxed into MKV, which holds any
# codec. Audio-only results (sites that only offer audio) keep their own container.
QUICK_MERGE_FORMAT = "mp4/mkv"
_QUICK_KEPT = ("opus", "ogg", "m4a", "mp3", "flac", "mka", "mp4")
QUICK_REMUX_RULE = "/".join(f"{ext}>{ext}" for ext in _QUICK_KEPT) + "/mkv"

# Original audio for playlists keeps the best audio as it is (-x --audio-format best copies the stream).
# The few containers that leaves which can't hold cover art (WAV, AIFF, ALAC, WMA) are copied into MKA,
# which holds any codec, so the result always takes cover art.
BEST_AUDIO_REMUX_RULE = "aiff>mka/alac>mka/wav>mka/wma>mka"
_BEST_AUDIO_RESULTS = frozenset({"flac", "m4a", "mka", "mp3", "ogg", "opus"})

# yt-dlp reports a failed subtitle download as "ERROR: Unable to download video subtitles for 'en': ..."
# (YoutubeDL._write_subtitles); some extractors fetch subtitles themselves and fail with
# "ERROR: [site] id: Unable to download subtitles...". Warnings don't stop the download, so only errors count.
_SUBTITLE_FAILURE = re.compile(r"^ERROR:.*\bUnable to download (?:video )?subtitles\b", re.MULTILINE)


def _required_format_id(choice: FormatChoice) -> str:
    if not choice.format_id:
        raise ValueError(f"a {choice.kind.value} choice needs a format id")
    return str(choice.format_id)


def format_args(choice: FormatChoice) -> list[str]:
    match choice.kind:
        case ChoiceKind.VIDEO:
            format_id = _required_format_id(choice)
            return ["-f", f"{format_id}+{choice.audio_id}" if choice.audio_id else format_id]
        case ChoiceKind.AUDIO:
            args = ["-f", _required_format_id(choice)]
            if choice.ext == "webm":
                # Lossless remux to .opus/.ogg; WebM can't hold cover art.
                args += ["-x", "--audio-format", "best"]
            return args
        case ChoiceKind.MP3:
            # "b" covers sites that only offer formats with video and audio together.
            return ["-f", "ba/b", "-x", "--audio-format", "mp3", "--audio-quality", "192K"]
        case ChoiceKind.BEST_AUDIO:
            # yt-dlp's "ba" ranks the original language first; "b" as for MP3.
            return ["-f", "ba/b", "-x", "--audio-format", "best", "--remux-video", BEST_AUDIO_REMUX_RULE]
        case ChoiceKind.QUICK:
            # yt-dlp's own best-quality ranking. With a height, it prefers formats whose shorter side (res)
            # is within that cap, so a vertical 1080x1920 video counts as 1080; higher resolutions are only
            # a fallback. Without one (None, the default) there is no cap: the best available.
            # "b" also picks the best audio-only format on sites that have no video.
            cap = ["-S", f"res:{choice.height}"] if choice.height else []
            return [
                "-f", "bv*+ba/b",
                *cap,
                "--merge-output-format", QUICK_MERGE_FORMAT,
                "--remux-video", QUICK_REMUX_RULE,
            ]
    raise ValueError(f"unknown choice kind: {choice.kind}")


def result_exts(choice: FormatChoice) -> frozenset[str]:
    """The containers the finished file can have; empty when it can't be known in advance."""
    ext = (choice.ext or "").lower()
    match choice.kind:
        case ChoiceKind.VIDEO:
            if not ext:
                return frozenset()
            if not choice.audio_id:
                return frozenset({ext})
            if choice.audio_ext is None:
                # Saved before the audio's container was recorded: anything the merge could give.
                return possible_merged_exts(choice.vcodec, ext)
            return frozenset({merged_ext(choice.vcodec, ext, choice.audio_codec, choice.audio_ext.lower())})
        case ChoiceKind.AUDIO:
            if ext == "webm":  # -x --audio-format best: Opus -> .opus, Vorbis -> .ogg
                known = extracted_audio_ext(ext, choice.audio_codec)
                return frozenset({known}) if known else frozenset({"opus", "ogg"})
            return frozenset({ext}) if ext else frozenset()
        case ChoiceKind.MP3:
            return frozenset({"mp3"})
        case ChoiceKind.QUICK:
            return frozenset({*_QUICK_KEPT, "mkv"})
        case ChoiceKind.BEST_AUDIO:
            return _BEST_AUDIO_RESULTS
    return frozenset()


def embeds_thumbnail(choice: FormatChoice) -> bool:
    """True when the finished file is known to be a container that takes cover art."""
    exts = result_exts(choice)
    return bool(exts) and exts <= COVER_ART_EXTS


def extras_args(extras: Extras, choice: FormatChoice, without_subtitles: bool = False) -> list[str]:
    """yt-dlp's arguments for the extras; which ones a download gets is decided by the gets_* rules in
    snag.jobs.models, which History's summary uses too."""
    args: list[str] = []
    if gets_subtitles(extras, choice) and not without_subtitles:
        args += ["--write-subs", "--write-auto-subs", "--sub-langs", extras.subtitle_lang]
        if extras.subtitle_mode is SubtitleMode.EMBEDDED:
            # no-keep-subs: delete the subtitle file once it's inside the video.
            args += ["--embed-subs", "--compat-options", "no-keep-subs"]
        else:
            args += ["--convert-subs", "srt"]
    trimmed = is_trimmed(extras)
    sponsorblock = gets_sponsorblock(extras)
    chapters = gets_chapters(extras)
    marking = sponsorblock and extras.sponsorblock_mode is SponsorBlockMode.MARK
    if extras.thumbnail_tags:
        if embeds_thumbnail(choice):
            args.append("--embed-thumbnail")
        args.append("--embed-metadata")
        if not (chapters or marking):
            # yt-dlp's --embed-metadata also embeds chapters unless told not to.
            args.append("--no-embed-chapters")
    if chapters:
        args.append("--embed-chapters")
    if sponsorblock:
        # Marking writes the segments as chapters, so it embeds chapters even without --embed-chapters.
        flag = "--sponsorblock-mark" if marking else "--sponsorblock-remove"
        args += [flag, ",".join(extras.sponsorblock_categories)]
    if trimmed:
        section = f"*{extras.trim_start or '0'}-{extras.trim_end or 'inf'}"
        args += ["--download-sections", section, "--force-keyframes-at-cuts"]
    return args


def is_subtitle_failure(output: str) -> bool:
    """True when yt-dlp's output shows the download failed because subtitles couldn't be downloaded,
    so the job can be retried with `without_subtitles=True`."""
    return _SUBTITLE_FAILURE.search(output) is not None


def expected_streams(choice: FormatChoice) -> int:
    two = (choice.kind is ChoiceKind.VIDEO and choice.audio_id) or choice.kind is ChoiceKind.QUICK
    return 2 if two else 1


def title_limit(title: str, work_dir: Path) -> int:
    """How many characters of the title fit in yt-dlp's names inside work_dir.

    The longest temporary name ("<title>" + TEMP_SUFFIX_UNITS) must stay within a Windows name and, with
    the folder, within MAX_PATH. yt-dlp's %(title).<n>s counts characters while Windows counts UTF-16
    units, so characters such as emoji in the (expected) title count twice. Very deep folders still
    keep MIN_STEM_UNITS of the title.
    """
    room = min(MAX_NAME_UNITS, MAX_PATH_UNITS - utf16_units(str(work_dir)) - 1) - TEMP_SUFFIX_UNITS
    budget = max(room, MIN_STEM_UNITS)
    used = 0
    for count, char in enumerate(title):
        used += utf16_units(char)
        if used > budget:
            return count
    return budget - (used - len(title))


def output_template(title: str, work_dir: Path) -> str:
    """Relative to the working folder yt-dlp runs in (see the note on TEMP_SUFFIX_UNITS)."""
    return f"%(title).{title_limit(title, work_dir)}s.%(ext)s"


def build_command(
    spec: JobSpec, tools: Tools, work_dir: Path, cookies_file: Path | None, *, without_subtitles: bool = False,
) -> list[str]:
    """yt-dlp's arguments for one attempt; it must run with work_dir as its current folder."""
    # A part of a multi-part page is picked by its position; anything else is a single video.
    playlist = ["--playlist-items", str(spec.playlist_item)] if spec.playlist_item else ["--no-playlist"]
    args = [
        *tools.ytdlp,
        "--encoding", "utf-8",
        "--color", "no_color",
        "--js-runtimes", f"deno:{tools.deno}",
        "--ffmpeg-location", str(tools.ffmpeg_dir),
        "--progress", "--newline",
        "--progress-template", DOWNLOAD_TEMPLATE,
        "--progress-template", POSTPROCESS_TEMPLATE,
        "--print", FORMAT_PRINT,
        "--print", FILE_PRINT,
        "--windows-filenames", "--no-mtime", "--continue",
        *playlist,
        "--retries", "10", "--fragment-retries", "10",
        "-o", output_template(spec.title, work_dir),
        *format_args(spec.choice),
        *extras_args(spec.extras, spec.choice, without_subtitles),
    ]
    if cookies_file is not None:
        args += ["--cookies", str(cookies_file)]
    return [*args, "--", spec.url]
