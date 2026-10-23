import ast
import inspect
import itertools
import math
import textwrap
from pathlib import Path

import pytest
import yt_dlp
from yt_dlp.postprocessor.embedthumbnail import EmbedThumbnailPP
from yt_dlp.postprocessor.ffmpeg import resolve_mapping

from yt_dlp.postprocessor.ffmpeg import ACODECS, FFmpegExtractAudioPP

from snag.info.formats import build_format_lists
from snag.jobs.command import (
    BEST_AUDIO_REMUX_RULE,
    COVER_ART_EXTS,
    QUICK_REMUX_RULE,
    TEMP_SUFFIX_UNITS,
    build_command,
    embeds_thumbnail,
    expected_streams,
    extras_args,
    format_args,
    is_subtitle_failure,
    output_template,
    result_exts,
    title_limit,
)
from snag.jobs.workdir import MAX_NAME_UNITS, MAX_PATH_UNITS, MIN_STEM_UNITS
from snag.jobs.models import ChoiceKind, Extras, FormatChoice, choice_for_row
from snag.jobs.progress import DOWNLOAD_TEMPLATE, FILE_PRINT, FORMAT_PRINT, POSTPROCESS_TEMPLATE
from tests.helpers import make_spec

VIDEO = FormatChoice(ChoiceKind.VIDEO, "299", "140", 1080, "mp4")
WEBM_VIDEO = FormatChoice(ChoiceKind.VIDEO, "303", "251", 1080, "webm")
M4A = FormatChoice(ChoiceKind.AUDIO, "140", ext="m4a")
WEBM_AUDIO = FormatChoice(ChoiceKind.AUDIO, "251", ext="webm")
MP3 = FormatChoice(ChoiceKind.MP3)
QUICK = FormatChoice(ChoiceKind.QUICK, height=720)
QUICK_UNLIMITED = FormatChoice(ChoiceKind.QUICK)
BEST_AUDIO = FormatChoice(ChoiceKind.BEST_AUDIO)
AV1_OPUS = FormatChoice(ChoiceKind.VIDEO, "399", "251", 1080, "mp4", "av01.0.08M.08", "webm", "opus")
H264_OPUS = FormatChoice(ChoiceKind.VIDEO, "299", "251", 1080, "mp4", "avc1.64002A", "webm", "opus")
VP9_M4A = FormatChoice(ChoiceKind.VIDEO, "303", "140", 1080, "webm", "vp09.00.40.08", "m4a", "mp4a.40.2")


def test_format_video_with_audio():
    assert format_args(VIDEO) == ["-f", "299+140"]


def test_format_combined_video():
    assert format_args(FormatChoice(ChoiceKind.VIDEO, "18", None, 360, "mp4")) == ["-f", "18"]


def test_format_audio_m4a_and_webm():
    assert format_args(M4A) == ["-f", "140"]
    assert format_args(WEBM_AUDIO) == ["-f", "251", "-x", "--audio-format", "best"]


@pytest.mark.parametrize("kind", [ChoiceKind.VIDEO, ChoiceKind.AUDIO])
def test_format_requires_a_format_id(kind):
    with pytest.raises(ValueError):
        format_args(FormatChoice(kind, None, None, 1080, "mp4"))


def test_format_mp3_takes_best_audio_or_combined():
    assert format_args(MP3) == ["-f", "ba/b", "-x", "--audio-format", "mp3", "--audio-quality", "192K"]


def test_format_quick_is_best_quality_capped_by_the_shorter_side_and_never_webm():
    args = format_args(QUICK)
    # res is the smaller dimension, so a vertical 1080x1920 video counts as 1080; -S only prefers,
    # so a video offered only above the cap still downloads.
    assert args[:4] == ["-f", "bv*+ba/b", "-S", "res:720"]
    assert args[4:] == ["--merge-output-format", "mp4/mkv", "--remux-video", QUICK_REMUX_RULE]


def test_format_quick_without_a_height_has_no_resolution_cap():
    # No limit (the default, user decision 2026-10-07): yt-dlp's own best, still never WebM.
    assert format_args(QUICK_UNLIMITED) == [
        "-f", "bv*+ba/b", "--merge-output-format", "mp4/mkv", "--remux-video", QUICK_REMUX_RULE,
    ]


@pytest.mark.parametrize("source, target", [
    # Audio-only results (sites with only audio) keep their container instead of being wrapped in MKV.
    ("opus", "opus"), ("ogg", "ogg"), ("m4a", "m4a"), ("mp3", "mp3"), ("flac", "flac"), ("mka", "mka"),
    ("mp4", "mp4"), ("mkv", "mkv"),
    # Anything else (WebM, FLV, 3GP...) becomes MKV, which holds any codec and takes cover art.
    ("webm", "mkv"), ("flv", "mkv"), ("3gp", "mkv"), ("mov", "mkv"),
])
def test_quick_remux_rule_as_yt_dlp_resolves_it(source, target):
    assert resolve_mapping(source, QUICK_REMUX_RULE)[0] == target
    assert target in COVER_ART_EXTS


def test_subtitles_embed_and_file():
    embed = extras_args(Extras(subtitles=True, subtitle_lang="de"), VIDEO)
    assert embed == [
        "--write-subs", "--write-auto-subs", "--sub-langs", "de",
        "--embed-subs", "--compat-options", "no-keep-subs",
    ]
    as_file = extras_args(Extras(subtitles=True, subtitle_mode="file"), VIDEO)
    assert as_file == ["--write-subs", "--write-auto-subs", "--sub-langs", "en", "--convert-subs", "srt"]


def test_no_subtitles_for_audio():
    assert extras_args(Extras(subtitles=True), MP3) == []
    assert extras_args(Extras(subtitles=True), M4A) == []


@pytest.mark.parametrize("start, end, keeps", [
    ("1:30", None, False),  # yt-dlp doesn't shift subtitle timings, so they'd be out of sync
    ("5", "10", False),
    (None, "2:00", True),   # an end-only trim keeps the timings right
    ("0", "2:00", True),    # starting at zero shifts nothing
    ("0:00.0", None, True),
])
def test_trim_with_a_start_time_skips_subtitles(start, end, keeps):
    args = extras_args(Extras(subtitles=True, trim_start=start, trim_end=end), VIDEO)
    assert ("--write-subs" in args) is keeps


def test_without_subtitles_drops_subtitle_flags():
    extras = Extras(subtitles=True, chapters=True)
    assert extras_args(extras, VIDEO, without_subtitles=True) == ["--embed-chapters"]


@pytest.mark.parametrize("choice, embeds", [
    (VIDEO, True),
    (WEBM_VIDEO, False),
    (AV1_OPUS, False),  # yt-dlp merges AV1 + Opus into WebM
    (H264_OPUS, True),  # ... H.264 + Opus into MKV
    (VP9_M4A, True),    # ... VP9 + M4A into MKV
    (FormatChoice(ChoiceKind.VIDEO, "303", "251", 1080, "webm", "vp9", "webm", "opus"), False),
    (FormatChoice(ChoiceKind.VIDEO, "399", "140", 1080, "mp4", "av01.0.08M.08", "m4a", "mp4a.40.2"), True),
    # Saved before the audio was recorded: AV1 could end up in WebM, H.264 can't.
    (FormatChoice(ChoiceKind.VIDEO, "399", "251", 1080, "mp4", "av01.0.08M.08"), False),
    (FormatChoice(ChoiceKind.VIDEO, "299", "251", 1080, "mp4", "avc1.64002A"), True),
    (FormatChoice(ChoiceKind.VIDEO, "18", None, 360, "mp4"), True),
    (FormatChoice(ChoiceKind.VIDEO, "x", None, 360, "mkv"), True),
    (FormatChoice(ChoiceKind.VIDEO, "x", None, 360, "flv"), False),
    (FormatChoice(ChoiceKind.VIDEO, "x", None, 360, None), False),
    (M4A, True),
    (WEBM_AUDIO, True),  # remuxed to .opus/.ogg
    (FormatChoice(ChoiceKind.AUDIO, "x", ext="mp3"), True),
    (FormatChoice(ChoiceKind.AUDIO, "x", ext="flac"), True),
    (FormatChoice(ChoiceKind.AUDIO, "x", ext="wav"), False),
    (FormatChoice(ChoiceKind.AUDIO, "x", ext="aac"), False),
    (FormatChoice(ChoiceKind.AUDIO, "251", ext="webm", audio_ext="webm", audio_codec="opus"), True),
    (MP3, True),
    (QUICK, True),
    (BEST_AUDIO, True),
])
def test_thumbnail_only_for_cover_art_containers(choice, embeds):
    assert embeds_thumbnail(choice) is embeds
    args = extras_args(Extras(thumbnail_tags=True), choice)
    assert args == (["--embed-thumbnail"] if embeds else []) + ["--embed-metadata", "--no-embed-chapters"]


@pytest.mark.parametrize("choice, exts", [
    (AV1_OPUS, {"webm"}),
    (H264_OPUS, {"mkv"}),
    (VP9_M4A, {"mkv"}),
    (FormatChoice(ChoiceKind.VIDEO, "18", None, 360, "mp4"), {"mp4"}),
    (FormatChoice(ChoiceKind.AUDIO, "251", ext="webm", audio_ext="webm", audio_codec="opus"), {"opus"}),
    (FormatChoice(ChoiceKind.AUDIO, "250", ext="webm", audio_ext="webm", audio_codec="vorbis"), {"ogg"}),
    (WEBM_AUDIO, {"opus", "ogg"}),
    (MP3, {"mp3"}),
])
def test_result_exts(choice, exts):
    assert result_exts(choice) == exts


def _rows(formats):
    lists = build_format_lists(formats, None)
    return {row.format_id: row for row in lists.video_all + lists.audio_tracks}


def test_rows_and_commands_agree_on_the_container_when_the_original_audio_is_only_opus():
    """A dubbed video whose original language has no M4A: MP4 video pairs with Opus."""
    dub = {"format_id": "140-0", "ext": "m4a", "protocol": "https", "vcodec": "none", "acodec": "mp4a.40.2",
           "language": "de", "language_preference": -1}
    original = {"format_id": "251-1", "ext": "webm", "protocol": "https", "vcodec": "none", "acodec": "opus",
                "language": "en", "language_preference": 10}
    h264 = {"format_id": "137", "ext": "mp4", "protocol": "https", "vcodec": "avc1.640028", "acodec": "none", "height": 1080}
    av1 = {**h264, "format_id": "399", "vcodec": "av01.0.08M.08"}
    rows = _rows([dub, original, h264, av1])
    for format_id, ext, embeds in [("137", "mkv", True), ("399", "webm", False)]:
        choice = choice_for_row(rows[format_id])
        assert choice.audio_id == "251-1"
        assert result_exts(choice) == {ext} == {rows[format_id].result_ext}
        assert embeds_thumbnail(choice) is embeds


def test_format_best_audio_keeps_the_original_audio_as_it_is():
    assert format_args(BEST_AUDIO) == [
        "-f", "ba/b", "-x", "--audio-format", "best", "--remux-video", BEST_AUDIO_REMUX_RULE,
    ]
    assert expected_streams(BEST_AUDIO) == 1


def _best_extraction_outputs() -> set[str]:
    """Every container `-x --audio-format best` can leave (FFmpegExtractAudioPP.run): a common audio
    format is kept as it is; anything else is copied into its codec's container, or made MP3."""
    return {*FFmpegExtractAudioPP.COMMON_AUDIO_EXTS, *(ext for ext, _encoder, _opts in ACODECS.values())}


def test_best_audio_result_always_takes_cover_art():
    outputs = _best_extraction_outputs()
    finals = {resolve_mapping(ext, BEST_AUDIO_REMUX_RULE)[0] or ext for ext in outputs}
    assert finals <= COVER_ART_EXTS
    assert finals == result_exts(BEST_AUDIO)
    # Only containers that can't take cover art are remuxed, and only into MKA (a lossless copy).
    for ext in outputs:
        target = resolve_mapping(ext, BEST_AUDIO_REMUX_RULE)[0]
        assert (target is not None) is (ext not in COVER_ART_EXTS)
        assert target in (None, "mka")


def _exts_embed_thumbnail_handles() -> set[str]:
    """Every container EmbedThumbnailPP.run has a branch for: info['ext'] == 'x' / info['ext'] in [...]."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(EmbedThumbnailPP.run)))
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare) or len(node.ops) != 1:
            continue
        left = node.left
        is_ext = (
            isinstance(left, ast.Subscript) and isinstance(left.value, ast.Name) and left.value.id == "info"
            and isinstance(left.slice, ast.Constant) and left.slice.value == "ext"
        )
        if not is_ext:
            continue
        right, op = node.comparators[0], node.ops[0]
        if isinstance(op, ast.Eq) and isinstance(right, ast.Constant):
            found.add(right.value)
        elif isinstance(op, ast.In) and isinstance(right, (ast.List, ast.Tuple)):
            found.update(element.value for element in right.elts)
    return found


def test_cover_art_containers_match_yt_dlp():
    assert COVER_ART_EXTS == _exts_embed_thumbnail_handles()


def test_chapters_and_sponsorblock():
    extras = Extras(chapters=True, sponsorblock=True, sponsorblock_mode="mark", sponsorblock_categories=("sponsor", "intro"))
    assert extras_args(extras, VIDEO) == ["--embed-chapters", "--sponsorblock-mark", "sponsor,intro"]


@pytest.mark.parametrize("start, end, section", [("1:30", None, "*1:30-inf"), (None, "90", "*0-90"), ("5", "10.5", "*5-10.5")])
def test_trim_skips_chapters_and_sponsorblock(start, end, section):
    extras = Extras(chapters=True, sponsorblock=True, trim_start=start, trim_end=end)
    assert extras_args(extras, VIDEO) == ["--download-sections", section, "--force-keyframes-at-cuts"]


def test_trim_keeps_metadata_from_embedding_full_video_chapters():
    extras = Extras(thumbnail_tags=True, chapters=True, trim_start="5")
    assert extras_args(extras, VIDEO) == [
        "--embed-thumbnail", "--embed-metadata", "--no-embed-chapters",
        "--download-sections", "*5-inf", "--force-keyframes-at-cuts",
    ]


def test_metadata_embeds_chapters_only_when_asked():
    # --embed-metadata implies chapters in yt-dlp; the Chapters extra decides instead.
    assert extras_args(Extras(thumbnail_tags=True), VIDEO) == ["--embed-thumbnail", "--embed-metadata", "--no-embed-chapters"]
    assert extras_args(Extras(thumbnail_tags=True, chapters=True), VIDEO) == ["--embed-thumbnail", "--embed-metadata", "--embed-chapters"]
    marking = Extras(thumbnail_tags=True, sponsorblock=True, sponsorblock_mode="mark", sponsorblock_categories=("sponsor",))
    assert extras_args(marking, VIDEO) == ["--embed-thumbnail", "--embed-metadata", "--sponsorblock-mark", "sponsor"]


def _value(args, flag):
    return args[args.index(flag) + 1]


def test_build_command_full(fake_tools, tmp_path):
    work = tmp_path / "work"
    cookies = work / "cookies.txt"
    spec = make_spec("https://youtu.be/a", tmp_path, extras=Extras(sponsorblock=True))
    args = build_command(spec, fake_tools, work, cookies)
    assert args[:len(fake_tools.ytdlp)] == list(fake_tools.ytdlp)
    for flag, value in [
        ("--js-runtimes", f"deno:{fake_tools.deno}"),
        ("--ffmpeg-location", str(fake_tools.ffmpeg_dir)),
        ("-o", output_template(spec.title, work)),
        ("--cookies", str(cookies)),
        ("-f", "299+140"),
        ("--sponsorblock-remove", "sponsor,selfpromo,interaction"),
        ("--encoding", "utf-8"),
        ("--color", "no_color"),
    ]:
        assert _value(args, flag) == value
    assert "-P" not in args  # yt-dlp runs inside the work folder instead (see snag.jobs.runner)
    assert [args[i + 1] for i, a in enumerate(args) if a == "--print"] == [FORMAT_PRINT, FILE_PRINT]
    assert args.count("--progress-template") == 2
    assert DOWNLOAD_TEMPLATE in args and POSTPROCESS_TEMPLATE in args
    for flag in ["--progress", "--newline", "--windows-filenames", "--no-mtime", "--no-playlist", "--continue"]:
        assert flag in args
    assert args[-2:] == ["--", "https://youtu.be/a"]


def test_build_command_keeps_the_work_folder_off_the_command_line(fake_tools, tmp_path):
    work = tmp_path / "100% %(title)s %SNAGTESTVAR%"
    args = build_command(make_spec("u", tmp_path), fake_tools, work, None)
    assert not any(str(work) in arg for arg in args)
    assert _value(args, "-o") == output_template("Fake Video", work)


def test_work_folder_is_never_expanded_by_yt_dlp(fake_tools, tmp_path, monkeypatch):
    """yt-dlp expands %VAR% and ~ in -P folders; with no -P the name it builds is relative, so it lands
    in the working folder the process runs in, whatever the folder is called."""
    monkeypatch.setenv("SNAGTESTVAR", "expanded")
    work = tmp_path / "%SNAGTESTVAR%" / "job"
    args = build_command(make_spec("u", tmp_path), fake_tools, work, None)
    opts = yt_dlp.parse_options(args[len(fake_tools.ytdlp):]).ydl_opts
    assert not opts.get("paths")
    with yt_dlp.YoutubeDL(opts) as ydl:
        name = ydl.prepare_filename({"id": "abc", "title": "Clip %SNAGTESTVAR%", "ext": "mp4"})
    assert name == "Clip %SNAGTESTVAR%.mp4"


def test_build_command_downloads_one_part_of_a_multi_part_page(fake_tools, tmp_path):
    args = build_command(make_spec("u", tmp_path, playlist_item=3), fake_tools, tmp_path, None)
    assert _value(args, "--playlist-items") == "3"
    assert "--no-playlist" not in args
    assert "--playlist-items" not in build_command(make_spec("u", tmp_path), fake_tools, tmp_path, None)


# --- the title limit keeps yt-dlp's temporary names inside Windows limits ---

ROOT = Path("C:/")  # no folder in the way: only the name limit applies


def units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def test_title_limit_in_a_short_work_folder_is_capped_by_the_name_limit():
    assert title_limit("x" * 400, ROOT) == MAX_NAME_UNITS - TEMP_SUFFIX_UNITS


@pytest.mark.parametrize("depth", [60, 120, 200])
def test_title_limit_keeps_the_longest_temporary_path_under_max_path(depth):
    work = Path("C:/") / ("w" * depth)
    limit = title_limit("x" * 400, work)
    longest = str(work / ("x" * limit + "." + "f" * (TEMP_SUFFIX_UNITS - 1)))
    assert units(longest) <= MAX_PATH_UNITS
    assert units(longest) >= MAX_PATH_UNITS - 1  # and doesn't waste room


def test_title_limit_keeps_a_readable_title_in_very_deep_folders():
    assert title_limit("x" * 400, Path("C:/") / ("w" * 240)) == MIN_STEM_UNITS


def test_title_limit_counts_emoji_as_two_windows_units():
    # yt-dlp's %(title).<n>s counts characters; each emoji is two UTF-16 units in a Windows name.
    budget = MAX_NAME_UNITS - TEMP_SUFFIX_UNITS
    assert title_limit("\U0001F600" * 300, ROOT) == budget // 2
    mixed = "ab\U0001F600" * 100
    limit = title_limit(mixed, ROOT)
    assert units(mixed[:limit]) <= budget < units(mixed[:limit + 1])


def test_title_limit_with_a_short_title_allows_the_whole_budget_in_characters():
    assert title_limit("Short \U0001F600", ROOT) == MAX_NAME_UNITS - TEMP_SUFFIX_UNITS - 1


def test_long_titles_are_cut_by_yt_dlp_to_fit(fake_tools):
    work = Path("C:/Users/someone/AppData/Local/Snag/work") / ("0" * 32)
    title = "\u00dcn\u00efcode \U0001F600 title " * 40
    spec = make_spec("u", work, title=title)
    opts = yt_dlp.parse_options(build_command(spec, fake_tools, work, None)[len(fake_tools.ytdlp):]).ydl_opts
    with yt_dlp.YoutubeDL(opts) as ydl:
        name = ydl.prepare_filename({"id": "abc", "title": title, "ext": "mp4"})
    stem = name.removesuffix(".mp4")
    longest = str(work / f"{stem}.f{'9' * 10}.mp4.part-Frag9999")
    assert units(longest) <= MAX_PATH_UNITS
    assert units(Path(longest).name) <= MAX_NAME_UNITS


def test_output_template_uses_a_character_limit():
    assert output_template("x" * 400, ROOT) == f"%(title).{MAX_NAME_UNITS - TEMP_SUFFIX_UNITS}s.%(ext)s"


def test_build_command_without_cookies(fake_tools, tmp_path):
    assert "--cookies" not in build_command(make_spec("u", tmp_path), fake_tools, tmp_path, None)


def test_build_command_without_subtitles(fake_tools, tmp_path):
    spec = make_spec("u", tmp_path, extras=Extras(subtitles=True))
    assert "--write-subs" in build_command(spec, fake_tools, tmp_path, None)
    assert "--write-subs" not in build_command(spec, fake_tools, tmp_path, None, without_subtitles=True)


def test_expected_streams():
    assert expected_streams(VIDEO) == 2
    assert expected_streams(FormatChoice(ChoiceKind.VIDEO, "18", None, 360, "mp4")) == 1
    assert expected_streams(FormatChoice(ChoiceKind.QUICK, height=1080)) == 2
    assert expected_streams(MP3) == 1


@pytest.mark.parametrize("output", [
    "ERROR: Unable to download video subtitles for 'en': HTTP Error 429: Too Many Requests",
    "[info] Writing video subtitles to: C:\\w\\Clip.en.vtt\n"
    "ERROR: Unable to download video subtitles for 'en-orig': <urlopen error [WinError 10054] An existing "
    "connection was forcibly closed by the remote host>\n",
    "ERROR: [BiliBili] 1234: Unable to download subtitles: HTTP Error 412: Precondition Failed",
])
def test_is_subtitle_failure(output):
    assert is_subtitle_failure(output) is True


@pytest.mark.parametrize("output", [
    "",
    "ERROR: [youtube] abc: Video unavailable",
    "ERROR: unable to download video data: HTTP Error 403: Forbidden",
    # A warning (the download carried on) next to an unrelated fatal error.
    "WARNING: Unable to download video subtitles for 'en': HTTP Error 429: Too Many Requests\n"
    "ERROR: unable to download video data: HTTP Error 403: Forbidden",
])
def test_is_not_subtitle_failure(output):
    assert is_subtitle_failure(output) is False


# --- every combination parses with the real yt-dlp and asks for what we intend ---

CHOICES = {
    "video": VIDEO, "webm-video": WEBM_VIDEO, "combined": FormatChoice(ChoiceKind.VIDEO, "18", None, 360, "mp4"),
    "m4a": M4A, "webm-audio": WEBM_AUDIO, "mp3": MP3, "quick": QUICK,
    "quick-unlimited": QUICK_UNLIMITED, "best-audio": BEST_AUDIO,
    "av1-opus": AV1_OPUS, "h264-opus": H264_OPUS,
}
EXTRAS = {
    "none": (Extras(), False),
    "subs-embed": (Extras(subtitles=True, subtitle_lang="de"), False),
    "subs-file": (Extras(subtitles=True, subtitle_mode="file"), False),
    "thumbnail-tags": (Extras(thumbnail_tags=True), False),
    "chapters": (Extras(chapters=True), False),
    "sb-remove": (Extras(sponsorblock=True), False),
    "sb-mark": (Extras(sponsorblock=True, sponsorblock_mode="mark", sponsorblock_categories=("sponsor", "chapter")), False),
    "trim-start": (Extras(trim_start="1:30", chapters=True, sponsorblock=True), False),
    "trim-end": (Extras(trim_end="2:00", chapters=True, sponsorblock=True), False),
    "trim-both": (Extras(trim_start="10", trim_end="20.5"), False),
    "trim-start-subs": (Extras(subtitles=True, trim_start="1:30"), False),
    "trim-end-subs": (Extras(subtitles=True, subtitle_mode="file", trim_end="2:00"), False),
    "everything": (Extras(subtitles=True, thumbnail_tags=True, chapters=True, sponsorblock=True), False),
    "without-subtitles": (Extras(subtitles=True, thumbnail_tags=True), True),
}
FORMATS = {
    "video": "299+140", "webm-video": "303+251", "combined": "18", "m4a": "140", "webm-audio": "251",
    "mp3": "ba/b", "quick": "bv*+ba/b", "quick-unlimited": "bv*+ba/b", "best-audio": "ba/b",
    "av1-opus": "399+251", "h264-opus": "299+251",
}


@pytest.mark.parametrize("choice_name, extras_name, with_cookies, playlist_item", [
    (c, e, cookies, item)
    for (c, e), cookies, item in zip(
        itertools.product(CHOICES, EXTRAS), itertools.cycle([False, True]), itertools.cycle([None, None, 2]),
    )
])
def test_command_matrix_parses_as_intended(fake_tools, tmp_path, choice_name, extras_name, with_cookies, playlist_item):
    choice = CHOICES[choice_name]
    extras, without_subtitles = EXTRAS[extras_name]
    work = tmp_path / "work 100%"
    cookies = tmp_path / "cookies.txt" if with_cookies else None
    spec = make_spec(
        "https://www.youtube.com/watch?v=abc", tmp_path, choice=choice, extras=extras, playlist_item=playlist_item,
    )
    args = build_command(spec, fake_tools, work, cookies, without_subtitles=without_subtitles)

    parsed = yt_dlp.parse_options(args[len(fake_tools.ytdlp):])
    opts = parsed.ydl_opts
    pps = {pp["key"]: pp for pp in opts["postprocessors"]}

    assert parsed.urls == ["https://www.youtube.com/watch?v=abc"]
    assert opts["format"] == FORMATS[choice_name]
    assert not opts.get("paths")
    assert opts["outtmpl"]["default"] == output_template(spec.title, work)
    assert opts["forceprint"] == {"before_dl": [FORMAT_PRINT[len("before_dl:"):]], "after_move": [FILE_PRINT[len("after_move:"):]]}
    assert not opts.get("simulate")
    assert opts.get("cookiefile") == (str(cookies) if cookies else None)
    if playlist_item is None:
        assert opts["noplaylist"] is True and opts.get("playlist_items") is None
    else:
        assert not opts.get("noplaylist") and opts["playlist_items"] == str(playlist_item)

    trimmed = bool(extras.trim_start or extras.trim_end)
    starts_late = bool(extras.trim_start) and extras.trim_start != "0"
    subtitles = extras.subtitles and not choice.audio_only and not without_subtitles and not starts_late
    assert bool(opts.get("writesubtitles")) is subtitles
    assert bool(opts.get("writeautomaticsub")) is subtitles
    if subtitles:
        assert opts["subtitleslangs"] == [extras.subtitle_lang]
    embed_subs = subtitles and extras.subtitle_mode == "embed"
    assert ("FFmpegEmbedSubtitle" in pps) is embed_subs
    if embed_subs:
        assert pps["FFmpegEmbedSubtitle"]["already_have_subtitle"] is False  # the .vtt is deleted after embedding
    assert ("FFmpegSubtitlesConvertor" in pps) is (subtitles and extras.subtitle_mode == "file")

    thumbnail = extras.thumbnail_tags and embeds_thumbnail(choice)
    assert ("EmbedThumbnail" in pps) is thumbnail
    assert bool(opts.get("writethumbnail")) is thumbnail
    marking = extras.sponsorblock and extras.sponsorblock_mode == "mark"
    chapters = not trimmed and (extras.chapters or marking)  # SponsorBlock marks are written as chapters
    metadata = pps.get("FFmpegMetadata", {})
    assert bool(metadata.get("add_metadata")) is extras.thumbnail_tags
    assert bool(metadata.get("add_chapters")) is chapters

    sponsorblock = extras.sponsorblock and not trimmed
    assert ("SponsorBlock" in pps) is sponsorblock
    if sponsorblock:
        assert pps["SponsorBlock"]["categories"] == set(extras.sponsorblock_categories)
        removing = extras.sponsorblock_mode == "remove"
        # ModifyChapters runs either way: it cuts the segments out, or turns them into chapters.
        removed = pps["ModifyChapters"]["remove_sponsor_segments"]
        assert removed == (set(extras.sponsorblock_categories) if removing else set())
    else:
        assert "ModifyChapters" not in pps

    if trimmed:
        start = 90 if extras.trim_start == "1:30" else float(extras.trim_start or 0)
        end = 120 if extras.trim_end == "2:00" else (float(extras.trim_end) if extras.trim_end else math.inf)
        assert opts["download_ranges"].ranges == [[start, end]]
        assert opts["force_keyframes_at_cuts"] is True
    else:
        assert not opts["download_ranges"].ranges

    extracting = choice.kind in (ChoiceKind.MP3, ChoiceKind.BEST_AUDIO) or choice is WEBM_AUDIO
    assert ("FFmpegExtractAudio" in pps) is extracting
    if choice.kind is ChoiceKind.MP3:
        assert (pps["FFmpegExtractAudio"]["preferredcodec"], pps["FFmpegExtractAudio"]["preferredquality"]) == ("mp3", "192")
    if choice.kind is ChoiceKind.BEST_AUDIO:
        assert pps["FFmpegExtractAudio"]["preferredcodec"] == "best"  # copied as it is, never converted
    if choice.kind is ChoiceKind.QUICK:
        if choice.height:
            assert opts["format_sort"] == [f"res:{choice.height}"]
        else:
            assert not opts.get("format_sort")
        assert opts["merge_output_format"] == "mp4/mkv"
        assert pps["FFmpegVideoRemuxer"]["preferedformat"] == QUICK_REMUX_RULE
    else:
        assert not opts.get("format_sort")
        assert not opts.get("merge_output_format")
        remux = BEST_AUDIO_REMUX_RULE if choice.kind is ChoiceKind.BEST_AUDIO else None
        assert pps.get("FFmpegVideoRemuxer", {}).get("preferedformat") == remux
