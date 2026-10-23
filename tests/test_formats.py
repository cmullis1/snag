from snag.info.formats import build_format_lists

FORMATS = [
    {"format_id": "sb0", "ext": "mhtml", "protocol": "mhtml", "vcodec": "none", "acodec": "none"},
    {"format_id": "140-drc", "ext": "m4a", "protocol": "https", "vcodec": "none", "acodec": "mp4a.40.2", "abr": 129.5, "filesize": 10271496},
    {"format_id": "140", "ext": "m4a", "protocol": "https", "vcodec": "none", "acodec": "mp4a.40.2", "abr": 129.5, "filesize": 10271496},
    {"format_id": "139", "ext": "m4a", "protocol": "https", "vcodec": "none", "acodec": "mp4a.40.5", "abr": 48.8, "filesize": 3871021},
    {"format_id": "251", "ext": "webm", "protocol": "https", "vcodec": "none", "acodec": "opus", "abr": 135.0, "filesize": 10202210},
    {"format_id": "299", "ext": "mp4", "protocol": "https", "vcodec": "avc1.64002A", "acodec": "none", "height": 1080, "fps": 60, "filesize": 257619653},
    {"format_id": "303", "ext": "webm", "protocol": "https", "vcodec": "vp9", "acodec": "none", "height": 1080, "fps": 60, "filesize": 168736189},
    {"format_id": "399", "ext": "mp4", "protocol": "https", "vcodec": "av01.0.09M.08", "acodec": "none", "height": 1080, "fps": 60, "filesize": 124386876},
    {"format_id": "617", "ext": "mp4", "protocol": "m3u8_native", "vcodec": "vp09.00.41.08", "acodec": None, "height": 1080, "fps": 60.0},
    {"format_id": "315", "ext": "webm", "protocol": "https", "vcodec": "vp9", "acodec": "none", "height": 2160, "fps": 60, "filesize_approx": 1362269472},
    {"format_id": "401", "ext": "mp4", "protocol": "https", "vcodec": "av01.0.13M.08", "acodec": "none", "height": 2160, "fps": 60, "filesize": 712445280},
    {"format_id": "18", "ext": "mp4", "protocol": "https", "vcodec": "avc1.42001E", "acodec": "mp4a.40.2", "height": 360, "fps": 30, "filesize_approx": 30000000},
]


def lists():
    return build_format_lists(FORMATS, duration=634.5)


def by_id(rows):
    return {row.format_id: row for row in rows}


def test_ignored_formats_are_dropped():
    ids = {row.format_id for row in lists().video_all + lists().audio_tracks}
    assert "sb0" not in ids
    assert "140-drc" not in ids
    assert "617" not in ids  # HLS duplicate of the direct VP9 1080p format


def test_video_all_order():
    assert [row.format_id for row in lists().video_all] == ["401", "315", "399", "303", "299", "18"]


def test_simple_view_one_row_per_height():
    simple = lists().video_simple
    assert [row.height for row in simple] == [2160, 1080, 360]
    assert [row.format_id for row in simple] == ["401", "399", "18"]  # AV1 at 2160p and 1080p


def test_simple_view_picks_the_best_codec_av1_vp9_hevc_h264():
    formats = [
        {**H264_1080, "format_id": "other", "vcodec": "mpeg4", "tbr": 9000, "filesize": 9000},
        {**H264_1080, "format_id": "h264", "tbr": 8000, "filesize": 8000},
        {**H264_1080, "format_id": "hevc", "vcodec": "hvc1.2.4.L120", "tbr": 3000, "filesize": 3000},
        {**H264_1080, "format_id": "vp9", "vcodec": "vp09.00.40.08", "ext": "webm", "tbr": 2500, "filesize": 2500},
        {**H264_1080, "format_id": "av1", "vcodec": "av01.0.08M.08", "tbr": 2000, "filesize": 2000},
    ]
    for expected in ["av1", "vp9", "hevc", "h264", "other"]:
        picked = build_format_lists(formats, None).video_simple
        assert [row.format_id for row in picked] == [expected]
        formats = [f for f in formats if f["format_id"] != expected]


def test_simple_view_codec_names_from_other_sites_rank_too():
    formats = [{**H264_1080, "format_id": "a", "vcodec": "h264", "tbr": 5000},
               {**H264_1080, "format_id": "b", "vcodec": "h265", "tbr": 4000}]
    assert [row.format_id for row in build_format_lists(formats, None).video_simple] == ["b"]


def test_simple_view_same_codec_prefers_higher_bitrate_then_larger_size():
    formats = [{**H264_1080, "format_id": "low", "tbr": 2000, "filesize": 9000},
               {**H264_1080, "format_id": "high", "tbr": 4000, "filesize": 4000}]
    assert [row.format_id for row in build_format_lists(formats, None).video_simple] == ["high"]
    formats = [{**H264_1080, "format_id": "small", "filesize": 1000},
               {**H264_1080, "format_id": "large", "filesize": 3000},
               {**H264_1080, "format_id": "unknown", "filesize": None}]
    assert [row.format_id for row in build_format_lists(formats, None).video_simple] == ["large"]


def test_simple_view_prefers_higher_frame_rate_at_a_height():
    formats = [{**H264_1080, "format_id": "30", "fps": 30, "vcodec": "av01.0.08M.08"},
               {**H264_1080, "format_id": "60", "fps": 60}]
    assert [row.format_id for row in build_format_lists(formats, None).video_simple] == ["60"]


def test_labels_include_high_frame_rate():
    rows = by_id(lists().video_all)
    assert rows["299"].label == "1080p60"
    assert rows["18"].label == "360p"


def test_audio_pairing_by_container():
    rows = by_id(lists().video_all)
    assert rows["299"].audio_id == "140"
    assert rows["399"].audio_id == "140"
    assert rows["303"].audio_id == "251"


def test_sizes_include_audio_and_mark_estimates():
    rows = by_id(lists().video_all)
    assert rows["299"].size == 257619653 + 10271496
    assert rows["299"].size_is_estimate is False
    assert rows["315"].size == 1362269472 + 10202210
    assert rows["315"].size_is_estimate is True


def test_combined_format_has_audio_and_own_size():
    row = by_id(lists().video_all)["18"]
    assert row.has_audio is True
    assert row.audio_id is None
    assert row.size == 30000000


def test_audio_list_mp3_first_then_by_bitrate():
    audio = lists().audio
    assert [row.format_id for row in audio] == ["mp3", "251", "140", "139"]
    assert audio[0].size == int(634.5 * 192000 / 8)
    assert audio[0].size_is_estimate is True
    assert audio[1].label == "opus 135k"
    assert audio[2].label == "m4a 130k"


def test_audio_tracks_are_the_original_audio_formats():
    result = lists()
    assert result.audio_tracks == result.audio[1:]


def test_mp3_size_unknown_without_duration():
    assert build_format_lists(FORMATS, duration=None).audio[0].size is None


def test_formats_without_codec_info_count_as_combined():
    result = build_format_lists([{"format_id": "0", "ext": "mp4", "protocol": "https"}], duration=None)
    assert [row.format_id for row in result.video_simple] == ["0"]
    assert result.video_simple[0].has_audio is True


def _m4a(format_id, language, preference, size, **extra):
    return {"format_id": format_id, "ext": "m4a", "protocol": "https", "vcodec": "none", "acodec": "mp4a.40.2",
            "abr": 129.5, "filesize": size, "language": language, "language_preference": preference, **extra}


H264_1080 = {"format_id": "137", "ext": "mp4", "protocol": "https", "vcodec": "avc1.640028", "acodec": "none",
             "height": 1080, "filesize": 5000}


def test_pairing_prefers_original_language_over_larger_dub():
    formats = [_m4a("140-0", "de", -1, 2000), _m4a("140-1", "en", 10, 1000), H264_1080]
    assert by_id(build_format_lists(formats, None).video_all)["137"].audio_id == "140-1"


def test_pairing_prefers_original_language_over_preferred_container():
    opus = {"format_id": "251-1", "ext": "webm", "protocol": "https", "vcodec": "none", "acodec": "opus",
            "abr": 130.0, "filesize": 1000, "language": "en", "language_preference": 10}
    formats = [_m4a("140-0", "de", -1, 2000), opus, H264_1080]
    assert by_id(build_format_lists(formats, None).video_all)["137"].audio_id == "251-1"


def test_descriptive_audio_is_paired_only_when_nothing_else():
    descriptive = _m4a("140-2", "en-desc", -10, 9000, abr=200.0)
    formats = [descriptive, _m4a("140-0", "de", -1, 2000), H264_1080]
    assert by_id(build_format_lists(formats, None).video_all)["137"].audio_id == "140-0"
    only_descriptive = build_format_lists([descriptive, H264_1080], None)
    row = by_id(only_descriptive.video_all)["137"]
    assert (row.audio_id, row.language, row.is_original) == ("140-2", "en-desc", False)  # better than silence
    assert row.size == 5000 + 9000
    assert [r.format_id for r in only_descriptive.audio_tracks] == ["140-2"]


def test_audio_tracks_carry_language_and_put_original_first():
    formats = [
        _m4a("140-0", "de", -1, 2000, abr=130.0, tbr=130.0),
        _m4a("140-1", "en", 10, 1000, abr=129.0, tbr=129.0),
        _m4a("139-0", "de", -1, 500, abr=48.0),
        _m4a("139-1", "en", 10, 400, abr=48.0),
    ]
    tracks = build_format_lists(formats, None).audio_tracks
    assert [row.format_id for row in tracks] == ["140-1", "139-1", "140-0", "139-0"]
    first = tracks[0]
    assert (first.language, first.is_original, first.tbr) == ("en", True, 129.0)
    assert (tracks[2].language, tracks[2].is_original) == ("de", False)
    assert first.label == "m4a 129k"  # the UI names the language itself


def test_audio_without_language_info_is_not_original():
    row = by_id(lists().audio_tracks)["140"]
    assert row.language is None and row.is_original is False


def test_video_rows_carry_video_details_and_paired_audio_language():
    hdr = {"format_id": "337", "ext": "webm", "protocol": "https", "vcodec": "vp09.02.51.10", "acodec": "none",
           "height": 2160, "tbr": 18000.5, "dynamic_range": "HDR10", "filesize": 7000}
    formats = [_m4a("140-0", "de", -1, 2000), _m4a("140-1", "en", 10, 1000), hdr]
    row = by_id(build_format_lists(formats, None).video_all)["337"]
    assert (row.tbr, row.dynamic_range) == (18000.5, "HDR10")
    assert (row.audio_id, row.language, row.is_original) == ("140-1", "en", True)


def test_combined_rows_use_their_own_language():
    combined = {"format_id": "18", "ext": "mp4", "protocol": "https", "vcodec": "avc1.42001E",
                "acodec": "mp4a.40.2", "height": 360, "language": "en", "language_preference": 10}
    row = build_format_lists([combined], None).video_all[0]
    assert (row.language, row.is_original) == ("en", True)


def test_drc_variants_dropped_even_when_renamed():
    formats = [
        {**_m4a("251-drc-0", "de", -1, 900), "ext": "webm", "acodec": "opus"},
        _m4a("140-1", "en", 10, 1000, format_note="English original (default), medium, DRC"),
        _m4a("140-0", "en", 10, 1000, format_note="English original (default), medium"),
    ]
    assert [row.format_id for row in build_format_lists(formats, None).audio_tracks] == ["140-0"]


def _sc(format_id, protocol, ext, acodec, abr):
    return {"format_id": format_id, "ext": ext, "protocol": protocol, "vcodec": "none", "acodec": acodec,
            "abr": abr, "tbr": abr, "filesize_approx": abr * 26700}


def test_hls_audio_kept_when_codec_or_bitrate_differs():
    formats = [
        _sc("hls_mp3_0_0", "m3u8_native", "mp3", "mp3", 128),
        _sc("http_mp3_0_0", "http", "mp3", "mp3", 128),
        _sc("hls_aac_96k", "m3u8_native", "m4a", "mp4a.40.2", 96),
        _sc("hls_aac_160k", "m3u8_native", "m4a", "mp4a.40.2", 160),
    ]
    ids = [row.format_id for row in build_format_lists(formats, None).audio_tracks]
    assert ids == ["hls_aac_160k", "http_mp3_0_0", "hls_aac_96k"]


def test_hls_audio_without_codec_or_bitrate_dropped_only_beside_direct_audio():
    hls = [{"format_id": i, "ext": "mp4", "protocol": "m3u8_native", "vcodec": "none", "acodec": None}
           for i in ("233", "234")]
    direct = _m4a("140", None, None, 1000)
    assert [row.format_id for row in build_format_lists([*hls, direct], None).audio_tracks] == ["140"]
    assert {row.format_id for row in build_format_lists(hls, None).audio_tracks} == {"233", "234"}


def _vp9(format_id, height, size, **extra):
    return {"format_id": format_id, "ext": "webm", "protocol": "https", "vcodec": "vp9", "acodec": "none",
            "height": height, "filesize": size, **extra}


def test_simple_view_prefers_sdr_over_better_hdr():
    formats = [_vp9("701", 2160, 9000, vcodec="av01.0.13M.10", dynamic_range="HDR10", tbr=30000),
               _vp9("315", 2160, 3000, dynamic_range="SDR")]
    result = build_format_lists(formats, None)
    assert [row.format_id for row in result.video_simple] == ["315"]
    assert {row.format_id for row in result.video_all} == {"701", "315"}


def test_simple_view_avoids_ai_upscaled_formats():
    formats = [_vp9("616-sr", 1080, 5000, format_note="1080p, AI-upscaled"),
               _vp9("248-sr-1", 1080, 5100),
               {**_vp9("399-sr", 1080, 5200), "vcodec": "av01.0.08M.08", "ext": "mp4"},
               {**_vp9("137", 1080, 3000), "vcodec": "avc1.640028", "ext": "mp4"}]
    result = build_format_lists(formats, None)
    assert [row.format_id for row in result.video_simple] == ["137"]  # H.264, but not upscaled
    assert by_id(result.video_all)["616-sr"].upscaled is True
    assert by_id(result.video_all)["137"].upscaled is False


def test_simple_view_falls_back_to_hdr_when_nothing_else():
    formats = [_vp9("337", 2160, 1000, dynamic_range="HDR10", vcodec="vp09.02.51.10"),
               _vp9("701", 2160, 900, dynamic_range="HDR10", vcodec="av01.0.13M.10")]
    assert [row.format_id for row in build_format_lists(formats, None).video_simple] == ["701"]


def test_video_only_format_without_any_audio_has_no_audio():
    row = build_format_lists([H264_1080], None).video_all[0]
    assert row.audio_id is None and row.has_audio is False


def test_format_lists_are_immutable_tuples():
    result = lists()
    for rows in (result.video_simple, result.video_all, result.audio, result.audio_tracks):
        assert isinstance(rows, tuple)


# --- the container the finished download really gets ---

import itertools  # noqa: E402

import pytest  # noqa: E402
from yt_dlp.utils import get_compatible_ext  # noqa: E402

from snag.info.containers import (  # noqa: E402
    compatible_ext, extracted_audio_ext, merged_ext, possible_merged_exts,
)

VIDEO_STREAMS = [
    ("avc1.64002A", "mp4"), ("avc1.4d401f", "mp4"), ("av01.0.08M.08", "mp4"), ("av01.0.12M.10", "webm"),
    ("vp09.00.40.08", "webm"), ("vp9", "webm"), ("vp8", "webm"), ("hvc1.2.4.L120", "mp4"), ("hev1.1.6.L93", "mp4"),
    ("h264", "mp4"), ("h264", "flv"), ("mpeg4", "3gp"), (None, "mp4"), (None, "webm"), (None, "mov"), ("avc1", "m4v"),
]
AUDIO_STREAMS = [
    ("mp4a.40.2", "m4a"), ("mp4a.40.5", "m4a"), ("opus", "webm"), ("vorbis", "webm"), ("opus", "m4a"),
    ("ac-3", "m4a"), ("ec-3", "mp4"), ("mp3", "mp3"), (None, "m4a"), (None, "webm"), (None, "aac"), ("flac", "flac"),
]


@pytest.mark.parametrize("video, audio", list(itertools.product(VIDEO_STREAMS, AUDIO_STREAMS)))
def test_merged_ext_matches_yt_dlp(video, audio):
    (vcodec, vext), (acodec, aext) = video, audio
    expected = get_compatible_ext(vcodecs=[vcodec], acodecs=[acodec], vexts=[vext], aexts=[aext])
    assert merged_ext(vcodec, vext, acodec, aext) == expected
    assert expected in possible_merged_exts(vcodec, vext)


@pytest.mark.parametrize("preferences", [None, ("mp4", "mkv"), ("webm", "mkv"), ("mp4",), ("webm",), ("mkv",)])
@pytest.mark.parametrize("video, audio", list(itertools.product(VIDEO_STREAMS[::3], AUDIO_STREAMS[::2])))
def test_compatible_ext_matches_yt_dlp_with_merge_preferences(video, audio, preferences):
    (vcodec, vext), (acodec, aext) = video, audio
    kwargs = dict(vcodecs=[vcodec], acodecs=[acodec], vexts=[vext], aexts=[aext], preferences=preferences)
    assert compatible_ext(**kwargs) == get_compatible_ext(**kwargs)


def test_compatible_ext_with_several_streams_matches_yt_dlp():
    kwargs = dict(vcodecs=["avc1", "vp9"], acodecs=["mp4a"], vexts=["mp4", "webm"], aexts=["m4a"])
    assert compatible_ext(**kwargs) == get_compatible_ext(**kwargs) == "mkv"
    kwargs["preferences"] = ("mp4",)
    assert compatible_ext(**kwargs) == get_compatible_ext(**kwargs)


@pytest.mark.parametrize("vcodec, vext, acodec, aext, result", [
    ("avc1.64002A", "mp4", "mp4a.40.2", "m4a", "mp4"),
    ("av01.0.08M.08", "mp4", "opus", "webm", "webm"),
    ("avc1.64002A", "mp4", "opus", "webm", "mkv"),
    ("vp09.00.40.08", "webm", "mp4a.40.2", "m4a", "mkv"),
    ("vp09.00.40.08", "webm", "opus", "webm", "webm"),
    ("av01.0.08M.08", "mp4", "mp4a.40.2", "m4a", "mp4"),
])
def test_merged_ext_examples(vcodec, vext, acodec, aext, result):
    assert merged_ext(vcodec, vext, acodec, aext) == result


@pytest.mark.parametrize("ext, acodec, result", [
    ("webm", "opus", "opus"), ("webm", "vorbis", "ogg"), ("webm", None, None), ("m4a", "mp4a.40.2", "m4a"),
    ("mp3", "mp3", "mp3"),
])
def test_extracted_audio_ext(ext, acodec, result):
    assert extracted_audio_ext(ext, acodec) == result


OPUS_ORIGINAL = {"format_id": "251-1", "ext": "webm", "protocol": "https", "vcodec": "none", "acodec": "opus",
                 "abr": 130.0, "filesize": 1000, "language": "en", "language_preference": 10}
AV1_1080 = {**H264_1080, "format_id": "399", "vcodec": "av01.0.08M.08"}
VP9_1080 = {**H264_1080, "format_id": "248", "ext": "webm", "vcodec": "vp09.00.40.08"}


def test_rows_show_the_container_of_the_merged_file():
    rows = by_id(lists().video_all)
    assert rows["299"].result_ext == "mp4"  # H.264 + M4A
    assert rows["399"].result_ext == "mp4"  # AV1 + M4A
    assert rows["303"].result_ext == "webm"  # VP9 + Opus
    assert rows["18"].result_ext == "mp4"  # combined: downloaded as it is


def test_rows_carry_the_paired_audio_container_and_codec():
    rows = by_id(lists().video_all)
    assert (rows["299"].audio_ext, rows["299"].audio_codec) == ("m4a", "mp4a.40.2")
    assert (rows["303"].audio_ext, rows["303"].audio_codec) == ("webm", "opus")
    assert (rows["18"].audio_ext, rows["18"].audio_codec) == (None, None)


def test_original_language_only_in_opus_changes_the_mp4_rows_containers():
    """No M4A in the original language: MP4 video is paired with Opus, which yt-dlp can't put in MP4."""
    formats = [_m4a("140-0", "de", -1, 2000), OPUS_ORIGINAL, H264_1080, AV1_1080, VP9_1080]
    rows = by_id(build_format_lists(formats, None).video_all)
    assert all(row.audio_id == "251-1" for row in rows.values())
    assert rows["137"].result_ext == "mkv"   # H.264 + Opus
    assert rows["399"].result_ext == "webm"  # AV1 + Opus
    assert rows["248"].result_ext == "webm"  # VP9 + Opus


def test_vp9_with_only_m4a_audio_ends_up_in_mkv():
    rows = by_id(build_format_lists([_m4a("140", "en", 10, 1000), VP9_1080], None).video_all)
    assert rows["248"].result_ext == "mkv"


def test_silent_video_keeps_its_own_container():
    row = build_format_lists([VP9_1080], None).video_all[0]
    assert row.audio_id is None and row.result_ext == "webm"


def test_audio_rows_show_the_container_they_are_saved_in():
    result = lists()
    audio = by_id(result.audio)
    assert audio["mp3"].result_ext == "mp3"
    assert audio["251"].result_ext == "opus"  # WebM audio is saved as .opus
    assert audio["140"].result_ext == "m4a"


def test_rows_say_what_kind_of_row_they_are():
    result = lists()
    assert {row.kind for row in result.video_all} == {"video"}
    assert {row.kind for row in result.audio_tracks} == {"audio"}
    assert result.audio[0].kind == "mp3"
