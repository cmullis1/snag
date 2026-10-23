"""Turns yt-dlp's raw format list into the rows the UI shows."""
from __future__ import annotations

import math
import re

from .containers import extracted_audio_ext, merged_ext
from .models import FormatLists, FormatRow

MP3_KBPS = 192
_DIRECT_PROTOCOLS = {"https", "http"}
_DRC_ID = re.compile(r"-drc(-\d+)?$")
_UPSCALED_ID = re.compile(r"-sr(-\d+)?$")
_SDR = (None, "SDR")
_CODEC_FAMILY = {
    "vp09": "vp9", "avc3": "avc1", "h264": "avc1", "hev1": "hevc", "hvc1": "hevc", "h265": "hevc", "av1": "av01",
}
# Best first, as yt-dlp ranks them; any other codec comes after these.
_CODEC_RANK = {"av01": 0, "vp9": 1, "hevc": 2, "avc1": 3}
# yt-dlp's YouTube extractor sets language_preference to 10 for the original audio language,
# 5 for the default track, -1 for other (dubbed) tracks, and -10 for descriptive audio.
ORIGINAL_LANG_PREFERENCE = 10
DESCRIPTIVE_LANG_PREFERENCE = -10


def _usable(fmt: dict) -> bool:
    if fmt.get("ext") == "mhtml" or str(fmt.get("protocol", "")).startswith("mhtml"):
        return False
    return not _is_drc(fmt)


def _is_drc(fmt: dict) -> bool:
    # yt-dlp renames clashing ids to "<id>-<n>", so a DRC id can look like "251-drc-0".
    if _DRC_ID.search(str(fmt.get("format_id", ""))):
        return True
    return "DRC" in (part.strip() for part in str(fmt.get("format_note") or "").split(","))


def _kind(fmt: dict) -> str:
    if fmt.get("vcodec") == "none":
        return "audio"
    if fmt.get("acodec") == "none":
        return "video"
    return "combined"


def codec_family(vcodec: str | None) -> str | None:
    """"avc1.64002A" -> "avc1", "vp09.00.40.08" -> "vp9"; None for no video."""
    if not vcodec or vcodec == "none":
        return None
    head = vcodec.split(".")[0].lower()
    return _CODEC_FAMILY.get(head, head)


def _size(fmt: dict) -> tuple[int | None, bool]:
    if fmt.get("filesize"):
        return int(fmt["filesize"]), False
    if fmt.get("filesize_approx"):
        return int(fmt["filesize_approx"]), True
    return None, False


def _bitrate(fmt: dict) -> float:
    return fmt.get("abr") or fmt.get("tbr") or 0.0


def _video_key(fmt: dict) -> tuple:
    # HLS video often omits acodec, so it is matched on resolution and codec family only.
    return (fmt.get("height"), codec_family(fmt.get("vcodec")))


def _audio_key(fmt: dict) -> tuple:
    bitrate = fmt.get("abr") or fmt.get("tbr")
    return (_audio_name(fmt), round(bitrate) if bitrate else None)


def _drop_hls_duplicates(formats: list[dict]) -> list[dict]:
    direct = [f for f in formats if f.get("protocol") in _DIRECT_PROTOCOLS]
    direct_video = {_video_key(f) for f in direct if _kind(f) != "audio"}
    direct_audio = {_audio_key(f) for f in direct if _kind(f) == "audio"}

    def duplicate(fmt: dict) -> bool:
        if not str(fmt.get("protocol", "")).startswith("m3u8"):
            return False
        if _kind(fmt) != "audio":
            return _video_key(fmt) in direct_video
        if fmt.get("acodec") in (None, "", "none") and not (fmt.get("abr") or fmt.get("tbr")):
            # Nothing to compare (YouTube's HLS audio): assume it repeats the direct audio.
            return bool(direct_audio)
        return _audio_key(fmt) in direct_audio

    return [f for f in formats if not duplicate(f)]


def _language_preference(fmt: dict) -> int:
    return fmt.get("language_preference") or 0


def _best_audio(audios: list[dict], ext: str | None = None) -> dict | None:
    candidates = [a for a in audios if ext is None or a.get("ext") == ext]
    if not candidates:
        return None
    return max(candidates, key=lambda a: (_language_preference(a), _bitrate(a), _size(a)[0] or 0))


def _pair_audio(video: dict, audios: list[dict]) -> dict | None:
    # Language outranks container: an original-language Opus track beats a dubbed M4A one.
    # Descriptive audio is used only when it's the only audio there is: better than a silent video.
    pairable = [a for a in audios if _language_preference(a) != DESCRIPTIVE_LANG_PREFERENCE] or audios
    if not pairable:
        return None
    top = max(_language_preference(a) for a in pairable)
    in_language = [a for a in pairable if _language_preference(a) == top]
    preferred = {"mp4": "m4a", "webm": "webm"}.get(video.get("ext"))
    return _best_audio(in_language, preferred) or _best_audio(in_language)


def _video_label(fmt: dict) -> str:
    height = fmt.get("height")
    if not height:
        return fmt.get("format_note") or str(fmt["format_id"])
    fps = fmt.get("fps")
    return f"{height}p{round(fps)}" if fps and fps > 30 else f"{height}p"


def _audio_name(fmt: dict) -> str:
    acodec = str(fmt.get("acodec") or "")
    if acodec.startswith("opus"):
        return "opus"
    if acodec.startswith("mp4a"):
        return "m4a"
    return fmt.get("ext") or "audio"


def _is_original(fmt: dict) -> bool:
    return _language_preference(fmt) >= ORIGINAL_LANG_PREFERENCE


def _is_upscaled(fmt: dict) -> bool:
    return bool(_UPSCALED_ID.search(str(fmt["format_id"]))) or "AI-upscaled" in str(fmt.get("format_note") or "")


def _video_row(fmt: dict, audios: list[dict]) -> FormatRow:
    size, estimate = _size(fmt)
    combined = _kind(fmt) == "combined"
    audio_id = None
    sound = fmt if combined else None
    ext = fmt.get("ext") or ""
    audio_ext = audio_codec = None
    result_ext = ext or None
    if not combined:
        audio = _pair_audio(fmt, audios)
        if audio is not None:
            sound = audio
            audio_id = str(audio["format_id"])
            audio_ext, audio_codec = audio.get("ext") or "", audio.get("acodec")
            result_ext = merged_ext(fmt.get("vcodec"), ext, audio_codec, audio_ext)
            audio_size, audio_estimate = _size(audio)
            size = size + audio_size if size is not None and audio_size is not None else None
            estimate = estimate or audio_estimate
    return FormatRow(
        format_id=str(fmt["format_id"]),
        label=_video_label(fmt),
        ext=ext,
        vcodec=fmt.get("vcodec"),
        acodec=fmt.get("acodec"),
        height=fmt.get("height"),
        fps=fmt.get("fps"),
        size=size,
        size_is_estimate=estimate if size is not None else False,
        audio_id=audio_id,
        has_audio=combined,
        language=sound.get("language") if sound else None,
        is_original=_is_original(sound) if sound else False,
        tbr=fmt.get("tbr"),
        dynamic_range=fmt.get("dynamic_range"),
        upscaled=_is_upscaled(fmt),
        audio_ext=audio_ext,
        audio_codec=audio_codec,
        result_ext=result_ext,
    )


def _audio_row(fmt: dict) -> FormatRow:
    size, estimate = _size(fmt)
    abr = fmt.get("abr") or fmt.get("tbr")
    name = _audio_name(fmt)
    ext = fmt.get("ext") or ""
    return FormatRow(
        format_id=str(fmt["format_id"]),
        label=f"{name} {round(abr)}k" if abr else name,
        ext=ext,
        acodec=fmt.get("acodec"),
        abr=abr,
        size=size,
        size_is_estimate=estimate,
        has_audio=True,
        language=fmt.get("language"),
        is_original=_is_original(fmt),
        tbr=fmt.get("tbr"),
        kind="audio",
        result_ext=extracted_audio_ext(ext, fmt.get("acodec")) if ext else None,
    )


def _sort_size(row: FormatRow) -> float:
    return row.size if row.size is not None else math.inf


def _simple_rank(row: FormatRow) -> tuple:
    """Smaller is better: higher frame rate, then the better codec, then higher bitrate, then larger size."""
    codec = _CODEC_RANK.get(codec_family(row.vcodec) or "", len(_CODEC_RANK))
    return (-(row.fps or 0), codec, -(row.tbr or 0), -(row.size or 0))


def best_quality(rows: list[FormatRow]) -> FormatRow:
    """The best-quality row of a non-empty group, ranked like yt-dlp. HDR and AI-upscaled formats are
    picked only when the group offers nothing else, so the result looks like the source."""
    plain = [r for r in rows if r.dynamic_range in _SDR and not r.upscaled]
    return min(plain or rows, key=_simple_rank)


def _simple_view(rows: tuple[FormatRow, ...]) -> tuple[FormatRow, ...]:
    """One row per height with the best-quality format, ranked like yt-dlp (user decision: quality over
    compatibility)."""
    by_height: dict[int, list[FormatRow]] = {}
    for row in rows:
        if row.height:
            by_height.setdefault(row.height, []).append(row)
    picked = []
    for height in sorted(by_height, reverse=True):
        picked.append(best_quality(by_height[height]))
    return (*picked, *(row for row in rows if not row.height))


def build_format_lists(formats: list[dict], duration: float | None) -> FormatLists:
    usable = _drop_hls_duplicates([f for f in formats if _usable(f)])
    audios = [f for f in usable if _kind(f) == "audio"]
    videos = [f for f in usable if _kind(f) != "audio"]

    video_all = tuple(sorted(
        (_video_row(f, audios) for f in videos),
        key=lambda r: (-(r.height or 0), -(r.fps or 0), _sort_size(r)),
    ))
    audio_tracks = tuple(sorted((_audio_row(f) for f in audios), key=lambda r: (not r.is_original, -(r.abr or 0))))
    mp3 = FormatRow(
        format_id="mp3",
        label="MP3",
        ext="mp3",
        abr=float(MP3_KBPS),
        size=int(duration * MP3_KBPS * 1000 / 8) if duration else None,
        size_is_estimate=bool(duration),
        has_audio=True,
        kind="mp3",
        result_ext="mp3",
    )
    return FormatLists(
        video_simple=_simple_view(video_all),
        video_all=video_all,
        audio=(mp3, *audio_tracks),
        audio_tracks=audio_tracks,
    )
