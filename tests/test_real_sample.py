"""Invariants of Snag's format logic checked against real, recorded `yt-dlp -J` output.

Expectations are computed from the raw formats in each sample, not hard-coded format ids, so a
re-recorded sample (YouTube renumbers and adds formats) keeps testing the same rules."""
import json
from pathlib import Path

import pytest

from snag.info.lookup import classify_url, parse_info

SAMPLES = Path(__file__).parent / "samples"
# sample file name -> video id
SAMPLE_IDS = {"youtube_bbb": "aqz-KE-bpKQ", "youtube_dubbed": "0e3GPea1Tyg"}
DIRECT = {"https", "http"}
ORIGINAL = 10  # yt-dlp's language_preference for the original audio language
CODEC_RANK = {"av01": 0, "vp09": 1, "vp9": 1, "hev1": 2, "hvc1": 2, "avc1": 3}


def _raw(name: str) -> dict:
    return json.loads((SAMPLES / f"{name}.json").read_text(encoding="utf-8"))


def _info(name: str):
    data = _raw(name)
    return parse_info(data, data["webpage_url"], classify_url(data["webpage_url"]))


def _is_drc(fmt: dict) -> bool:
    return "-drc" in fmt["format_id"] or "DRC" in (fmt.get("format_note") or "")


def _direct(raw: dict, *, audio: bool) -> list[dict]:
    """Formats yt-dlp downloads directly (no storyboards, HLS or DRC) of one kind."""
    return [
        f for f in raw["formats"]
        if f.get("protocol") in DIRECT and not _is_drc(f) and (f.get("vcodec") == "none") == audio
    ]


def _codec_rank(vcodec: str) -> int:
    return CODEC_RANK.get(vcodec.split(".")[0], len(CODEC_RANK))


def _expected_best(raw: dict, height: int) -> dict:
    """Best SDR format at a height: higher frame rate, then better codec, then higher bitrate."""
    candidates = [f for f in _direct(raw, audio=False) if f.get("height") == height and f.get("dynamic_range") in (None, "SDR")]
    return min(candidates, key=lambda f: (-(f.get("fps") or 0), _codec_rank(f["vcodec"]), -(f.get("tbr") or 0)))


@pytest.fixture(params=SAMPLE_IDS)
def sample(request):
    return request.param, _raw(request.param), _info(request.param)


def test_identity(sample):
    name, _raw_data, info = sample
    assert info.site == "youtube"
    assert info.video_id == SAMPLE_IDS[name]
    assert info.title
    assert info.duration


def test_no_storyboards_drc_or_hls_duplicates(sample):
    _name, raw, info = sample
    by_id = {f["format_id"]: f for f in raw["formats"]}
    lists = info.formats
    ids = [row.format_id for row in lists.video_all + lists.audio_tracks]
    assert not any(i.startswith("sb") or "-drc" in i for i in ids)
    assert all(by_id[i].get("protocol") in DIRECT for i in ids)
    # Nothing usable was lost either: every direct video and audio format is listed exactly once.
    assert sorted(row.format_id for row in lists.video_all) == sorted(f["format_id"] for f in _direct(raw, audio=False))
    assert sorted(row.format_id for row in lists.audio_tracks) == sorted(f["format_id"] for f in _direct(raw, audio=True))


def test_simple_view_is_one_row_per_height_descending(sample):
    _name, raw, info = sample
    heights = [row.height for row in info.formats.video_simple]
    assert heights == sorted(heights, reverse=True)
    assert len(heights) == len(set(heights))
    assert set(heights) == {f["height"] for f in _direct(raw, audio=False)}


def test_every_video_row_has_sound(sample):
    _name, _raw_data, info = sample
    for row in info.formats.video_all:
        assert row.has_audio or row.audio_id is not None


def test_1080p_simple_row_is_the_best_quality_format(sample):
    _name, raw, info = sample
    row = next(r for r in info.formats.video_simple if r.height == 1080)
    assert row.format_id == _expected_best(raw, 1080)["format_id"]
    codecs_at_1080 = {f["vcodec"].split(".")[0] for f in _direct(raw, audio=False) if f.get("height") == 1080}
    if "av01" in codecs_at_1080:
        assert row.vcodec.startswith("av01")
        assert not row.vcodec.startswith("avc1")


def test_1080p_row_pairs_the_original_language_audio_in_the_matching_container(sample):
    _name, raw, info = sample
    row = next(r for r in info.formats.video_simple if r.height == 1080)
    video = _expected_best(raw, 1080)
    audios = _direct(raw, audio=True)
    top = max(a.get("language_preference") or 0 for a in audios)
    in_language = [a for a in audios if (a.get("language_preference") or 0) == top]
    wanted_ext = {"mp4": "m4a", "webm": "webm"}[video["ext"]]
    best = max((a for a in in_language if a["ext"] == wanted_ext), key=lambda a: a.get("abr") or a.get("tbr") or 0)
    assert row.audio_id == best["format_id"]


def test_mp3_listed_first(sample):
    _name, _raw_data, info = sample
    audio = info.formats.audio
    assert audio[0].format_id == "mp3"
    assert audio[1:] == info.formats.audio_tracks


def test_dubbed_simple_rows_pair_the_original_language_track():
    raw, info = _raw("youtube_dubbed"), _info("youtube_dubbed")
    original_langs = {a["language"] for a in _direct(raw, audio=True) if (a.get("language_preference") or 0) >= ORIGINAL}
    assert len(original_langs) == 1
    assert len({a["language"] for a in _direct(raw, audio=True)}) > 5  # genuinely multi-language
    for row in info.formats.video_simple:
        assert row.audio_id is not None
        assert row.is_original
        assert row.language in original_langs


def test_dubbed_audio_tracks_list_original_language_first():
    tracks = _info("youtube_dubbed").formats.audio_tracks
    flags = [t.is_original for t in tracks]
    assert flags[0] and not all(flags)
    assert flags == sorted(flags, reverse=True)


def test_video_rows_show_the_container_yt_dlp_merges_into(sample):
    from yt_dlp.utils import get_compatible_ext

    _name, raw, info = sample
    by_id = {f["format_id"]: f for f in raw["formats"]}
    for row in info.formats.video_all:
        if row.audio_id is None:
            assert row.result_ext == row.ext
            continue
        video, audio = by_id[row.format_id], by_id[row.audio_id]
        expected = get_compatible_ext(
            vcodecs=[video.get("vcodec")], acodecs=[audio.get("acodec")], vexts=[video["ext"]], aexts=[audio["ext"]],
        )
        assert row.result_ext == expected


# --- a YouTube playlist (`-J --flat-playlist`), a non-YouTube page, and an age-restricted video ---

from snag.errors import ErrorKind, classify, missing_js_runtime  # noqa: E402
from snag.info.models import PlaylistInfo  # noqa: E402
from snag.jobs.command import embeds_thumbnail  # noqa: E402
from snag.jobs.models import ChoiceKind, FormatChoice, choice_for_row  # noqa: E402


def test_youtube_playlist_lists_every_entry_in_order():
    raw = _raw("youtube_playlist")
    url = raw["webpage_url"]
    kind = classify_url(url)
    assert kind.mode == "playlist"
    info = parse_info(raw, url, kind)
    assert isinstance(info, PlaylistInfo)
    assert (info.site, info.playlist_id, info.title) == ("youtube", raw["id"], raw["title"])
    present = [e for e in raw["entries"] if e]
    assert len(info.entries) == len(present) > 5
    assert [e.index for e in info.entries] == list(range(1, len(present) + 1))
    for entry, source in zip(info.entries, present):
        assert entry.video_id == source["id"]
        assert entry.url == f"https://www.youtube.com/watch?v={source['id']}"
        assert entry.title == source["title"]
        assert entry.item_index is None  # each entry has its own URL
        assert entry.thumbnail and entry.thumbnail.startswith("https://")
    assert any(entry.duration for entry in info.entries)


def test_non_youtube_page_gives_sensible_lists():
    """archive.org: two files that each hold video and sound, with no codec details."""
    raw = _raw("archive_org")
    info = parse_info(raw, raw["webpage_url"], classify_url(raw["webpage_url"]))
    assert info.site == "archiveorg"
    assert (info.video_id, info.duration) == ("Cops1922", raw["duration"])
    simple = info.formats.video_simple
    assert [row.height for row in simple] == sorted({f["height"] for f in raw["formats"]}, reverse=True)
    for row in simple:
        assert row.has_audio and row.audio_id is None
        assert row.result_ext == row.ext
        assert row.size == next(f["filesize"] for f in raw["formats"] if f["format_id"] == row.format_id)
    # No audio-only formats: only MP3, converted from the best file with sound.
    assert [row.kind for row in info.formats.audio] == ["mp3"]
    assert info.formats.audio_tracks == ()
    assert info.formats.audio[0].size_is_estimate


def test_non_youtube_rows_embed_cover_art_only_where_it_fits():
    raw = _raw("archive_org")
    info = parse_info(raw, raw["webpage_url"], classify_url(raw["webpage_url"]))
    for row in info.formats.video_all:
        choice = choice_for_row(row)
        assert choice == FormatChoice(ChoiceKind.VIDEO, row.format_id, None, row.height, row.ext, row.vcodec)
        assert embeds_thumbnail(choice) is (row.ext == "mp4")  # Ogg video (.ogv) can't take cover art


def test_age_restricted_video_without_cookies_needs_sign_in():
    stderr = (SAMPLES / "youtube_age_restricted.stderr.txt").read_text(encoding="utf-8")
    error = classify(stderr, "youtube")
    assert error.kind is ErrorKind.SIGN_IN
    assert error.message == "Sign in to YouTube in Chrome or Edge, then retry"
    assert missing_js_runtime(stderr)  # recorded without Deno, so yt-dlp warned about that too


def test_paid_video_is_unavailable_and_says_it_needs_payment():
    """Recorded from a rental (yt-dlp -J, no cookies): YouTube's own reason, no rule but this one fits."""
    stderr = (SAMPLES / "youtube_payment.stderr.txt").read_text(encoding="utf-8")
    error = classify(stderr, "youtube")
    assert error.kind is ErrorKind.UNAVAILABLE
    assert error.message == "This video requires payment to watch, so Snag can't download it."
    assert error.can_retry is False
