"""Which container yt-dlp gives a finished download, so cover art is only asked for where it fits and
the UI shows the container the file will really have.

Snag can't import yt-dlp at runtime (it runs yt-dlp.exe), so these mirror yt-dlp's own rules; tests
compare them with the installed yt-dlp."""
from __future__ import annotations

from collections.abc import Sequence

# yt-dlp utils.get_compatible_ext: COMPATIBLE_CODECS and COMPATIBLE_EXTS.
_COMPATIBLE_CODECS = {
    "mp4": frozenset({"av1", "hevc", "avc1", "mp4a", "ac-4", "h264", "aacl", "ec-3"}),
    "webm": frozenset({"av1", "vp9", "vp8", "opus", "vrbs", "vp9x", "vp8x"}),
}
_COMPATIBLE_EXTS = (
    frozenset({"mp3", "mp4", "m4a", "m4p", "m4b", "m4r", "m4v", "ismv", "isma", "mov"}),
    frozenset({"webm", "weba"}),
)

# What `-x --audio-format best` (FFmpegExtractAudioPP) turns a WebM audio download into: it copies the
# stream into the container named after the codec (ACODECS).
_WEBM_AUDIO_RESULT = {"opus": "opus", "vorbis": "ogg"}


def _sanitize_codec(codecs: Sequence[str | None]) -> str | None:
    """yt-dlp's sanitize_codec: "av01.0.08M.08" -> "av1", "vp09.00.40.08" -> "vp9", "mp4a.40.2" -> "mp4a"."""
    try:
        return codecs[0].split(".")[0].replace("0", "").lower()
    except (AttributeError, KeyError, TypeError, IndexError):
        return None


def compatible_ext(
    *,
    vcodecs: Sequence[str | None],
    acodecs: Sequence[str | None],
    vexts: Sequence[str],
    aexts: Sequence[str],
    preferences: Sequence[str] | None = None,
) -> str:
    """yt-dlp's utils.get_compatible_ext: the container a merge of these streams gets."""
    if len(vcodecs) != len(vexts) or len(acodecs) != len(aexts):
        raise ValueError("each stream needs both a codec and an extension")
    allow_mkv = not preferences or "mkv" in preferences
    if allow_mkv and max(len(acodecs), len(vcodecs)) > 1:
        return "mkv"
    vcodec, acodec = _sanitize_codec(vcodecs), _sanitize_codec(acodecs)
    for ext in preferences or _COMPATIBLE_CODECS:
        if ext == "mkv" or _COMPATIBLE_CODECS.get(ext, frozenset()).issuperset((vcodec, acodec)):
            return ext
    for ext in preferences or vexts:
        current = {ext, *vexts, *aexts}
        if ext == "mkv" or current == {ext} or any(group.issuperset(current) for group in _COMPATIBLE_EXTS):
            return ext
    return "mkv" if allow_mkv else preferences[-1]


def merged_ext(vcodec: str | None, vext: str, acodec: str | None, aext: str) -> str:
    """The container yt-dlp merges one video and one audio stream into when no --merge-output-format is
    given: e.g. H.264 (mp4) + AAC (m4a) -> mp4, AV1 (mp4) + Opus -> webm, H.264 + Opus -> mkv."""
    return compatible_ext(vcodecs=[vcodec], acodecs=[acodec], vexts=[vext], aexts=[aext])


def possible_merged_exts(vcodec: str | None, vext: str) -> frozenset[str]:
    """Every container a merge of this video with audio that isn't known could get: MKV, the video's own
    container, and MP4 or WebM when the video's codec fits them."""
    codec = _sanitize_codec([vcodec])
    by_codec = {ext for ext, codecs in _COMPATIBLE_CODECS.items() if codec in codecs}
    return frozenset({"mkv", vext, *by_codec})


def extracted_audio_ext(ext: str, acodec: str | None) -> str | None:
    """The container of a WebM audio download after `-x --audio-format best`: ".opus" for Opus, ".ogg" for
    Vorbis; None when the codec isn't known. Other containers are kept as they are."""
    if ext != "webm":
        return ext
    family = (acodec or "").split(".")[0].lower()
    return _WEBM_AUDIO_RESULT.get(family)
