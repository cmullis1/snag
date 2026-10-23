import json
from pathlib import Path

import pytest

from snag.errors import ErrorKind, make_error
from snag.jobs.models import (
    SPONSORBLOCK_CATEGORIES, SPONSORBLOCK_REMOVABLE, ChoiceKind, Extras, FormatChoice, Job, JobSpec, JobState,
    SponsorBlockMode, SubtitleMode, choice_from_dict, choice_to_dict, extras_from_dict, extras_to_dict, is_job_id,
    job_from_dict, job_to_dict, new_id, validate_extras,
)


def spec(**overrides):
    values = dict(
        url="https://youtu.be/a", title="A", site="youtube", video_id="a",
        choice=FormatChoice(ChoiceKind.VIDEO, "299", "140", 1080, "mp4"),
        extras=Extras(subtitles=True, sponsorblock=True), dest_dir=Path("C:/Videos/Snag"),
    )
    values.update(overrides)
    return JobSpec(**values)


def test_audio_only_choices():
    assert FormatChoice(ChoiceKind.MP3).audio_only
    assert FormatChoice(ChoiceKind.AUDIO, "251").audio_only
    assert not FormatChoice(ChoiceKind.QUICK, height=1080).audio_only


def test_extras_round_trip():
    extras = Extras(subtitles=True, sponsorblock_categories=("sponsor",), trim_start="1:30")
    assert extras_from_dict(extras_to_dict(extras)) == extras


def test_job_round_trip():
    job = Job(spec=spec(batch_id="b1", playlist_index=3, playlist_count=12, channel="C", estimated_size=99))
    job.state = JobState.FAILED
    job.error = make_error(ErrorKind.NETWORK, "youtube")
    job.file_path = Path("C:/Videos/Snag/A.mp4")
    job.used_cookies = True
    restored = job_from_dict(job_to_dict(job))
    assert restored == job


def test_playlist_item_round_trips_and_defaults_to_none():
    job = Job(spec=spec(playlist_item=3))
    data = json.loads(json.dumps(job_to_dict(job)))
    assert data["spec"]["playlist_item"] == 3
    assert job_from_dict(data).spec.playlist_item == 3
    del data["spec"]["playlist_item"]  # rows saved before the field existed
    assert job_from_dict(data).spec.playlist_item is None


@pytest.mark.parametrize("value", ["3", "1-100", 0, -1, True, 2.0])
def test_playlist_item_must_be_a_positive_whole_number(value):
    data = job_to_dict(Job(spec=spec()))
    data["spec"]["playlist_item"] = value
    with pytest.raises(ValueError, match="playlist_item"):
        job_from_dict(data)


def test_new_jobs_get_unique_ids_and_wait():
    first, second = Job(spec=spec()), Job(spec=spec())
    assert first.id != second.id
    assert first.state is JobState.WAITING


def test_is_job_id():
    assert is_job_id(new_id())
    assert not is_job_id("job1") and not is_job_id("My Videos") and not is_job_id(new_id().upper())


def full_job() -> Job:
    extras = Extras(
        subtitles=True, subtitle_lang="de", subtitle_mode=SubtitleMode.FILE, thumbnail_tags=True, chapters=True,
        sponsorblock=True, sponsorblock_mode=SponsorBlockMode.MARK, sponsorblock_categories=("sponsor", "intro"),
        trim_start="1:30", trim_end="2:00.5",
    )
    job = Job(spec=spec(
        choice=FormatChoice(ChoiceKind.VIDEO, "299", "140", 1080, "mp4"), extras=extras,
        channel="C", estimated_size=99, batch_id="b1", playlist_index=3, playlist_count=12, playlist_item=2,
    ))
    job.state = JobState.FAILED
    job.percent, job.speed, job.eta, job.stage = 42.5, 1234.5, 17, "Joining video and audio…"
    job.error = make_error(ErrorKind.NETWORK, "youtube")
    job.file_path = Path("C:/Videos/Snag/A.mp4")
    job.used_cookies = job.refetched = job.subtitles_skipped = True
    job.finished_at = job.created_at + 5
    return job


def test_job_round_trips_through_real_json():
    job = full_job()
    restored = job_from_dict(json.loads(json.dumps(job_to_dict(job))))
    assert restored == job
    assert restored.spec.extras.subtitle_mode is SubtitleMode.FILE
    assert restored.spec.extras.sponsorblock_mode is SponsorBlockMode.MARK
    assert isinstance(restored.spec.extras.sponsorblock_categories, tuple)


def test_modes_serialize_as_plain_strings():
    data = extras_to_dict(Extras(subtitle_mode=SubtitleMode.FILE, sponsorblock_mode=SponsorBlockMode.MARK))
    assert type(data["subtitle_mode"]) is str and data["subtitle_mode"] == "file"
    assert type(data["sponsorblock_mode"]) is str and data["sponsorblock_mode"] == "mark"


def test_mode_strings_become_enums():
    extras = Extras(subtitle_mode="file", sponsorblock_mode="mark", sponsorblock_categories=["sponsor"])
    assert extras.subtitle_mode is SubtitleMode.FILE and extras.sponsorblock_mode is SponsorBlockMode.MARK
    assert extras.sponsorblock_categories == ("sponsor",)
    with pytest.raises(ValueError):
        Extras(subtitle_mode="sideways")


def test_choice_round_trip():
    choice = FormatChoice(ChoiceKind.AUDIO, "251", None, None, "webm")
    assert choice_from_dict(json.loads(json.dumps(choice_to_dict(choice)))) == choice


def test_choice_keeps_the_video_codec_for_closest_match_recovery():
    choice = FormatChoice(ChoiceKind.VIDEO, "299", "140", 1080, "mp4", "avc1.64002A")
    data = json.loads(json.dumps(choice_to_dict(choice)))
    assert data["vcodec"] == "avc1.64002A"
    assert choice_from_dict(data) == choice
    del data["vcodec"]  # rows saved before the field existed
    assert choice_from_dict(data).vcodec is None


def test_decoding_ignores_unknown_keys():
    job = full_job()
    data = json.loads(json.dumps(job_to_dict(job)))
    data["from_the_future"] = 1
    data["spec"]["new_spec_field"] = "x"
    data["spec"]["choice"]["codec"] = "av1"
    data["spec"]["extras"]["dubbing"] = True
    assert job_from_dict(data) == job


def test_decoding_fills_missing_optional_keys_with_defaults():
    data = job_to_dict(Job(spec=spec()))
    for key in (
        "percent", "speed", "eta", "stage", "error", "file_path", "used_cookies", "refetched", "subtitles_skipped",
        "finished_at",
    ):
        del data[key]
    for key in ("channel", "estimated_size", "batch_id", "playlist_index", "playlist_count", "playlist_item"):
        del data["spec"][key]
    for key in ("format_id", "audio_id", "height", "ext"):
        del data["spec"]["choice"][key]
    del data["spec"]["extras"]["trim_end"], data["spec"]["extras"]["sponsorblock_categories"]
    job = job_from_dict(data)
    assert job.percent is None and job.error is None and job.used_cookies is False and job.finished_at is None
    assert job.subtitles_skipped is False
    assert job.spec.channel is None and job.spec.playlist_index is None
    assert job.spec.choice == FormatChoice(ChoiceKind.VIDEO)
    assert job.spec.extras.sponsorblock_categories == Extras().sponsorblock_categories


@pytest.mark.parametrize("breakage", [
    lambda d: d.pop("id"),
    lambda d: d["spec"].pop("url"),
    lambda d: d["spec"].pop("dest_dir"),
    lambda d: d["spec"]["choice"].pop("kind"),
    lambda d: d["spec"].update(title=7),
    lambda d: d["spec"].update(choice="video"),
    lambda d: d.update(state="exploded"),
    lambda d: d["spec"]["choice"].update(kind="hologram"),
])
def test_decoding_rejects_bad_required_fields(breakage):
    data = job_to_dict(Job(spec=spec()))
    breakage(data)
    with pytest.raises(ValueError):
        job_from_dict(data)


@pytest.mark.parametrize("key, value", [
    ("created_at", None),
    ("created_at", "1700000000"),
    ("created_at", True),
    ("created_at", float("nan")),
    ("created_at", float("inf")),
    ("finished_at", "1700000000"),
    ("finished_at", False),
    ("finished_at", float("nan")),
    ("percent", "50"),
    ("percent", True),
    ("percent", [50]),
    ("speed", "fast"),
    ("speed", float("inf")),
    ("eta", "10"),
    ("eta", False),
])
def test_decoding_rejects_mistyped_numbers(key, value):
    data = job_to_dict(Job(spec=spec()))
    data[key] = value
    with pytest.raises(ValueError, match=key):
        job_from_dict(data)


def test_decoding_requires_created_at():
    data = job_to_dict(Job(spec=spec()))
    del data["created_at"]
    with pytest.raises(ValueError, match="created_at"):
        job_from_dict(data)


def test_decoding_accepts_whole_and_fractional_numbers():
    data = job_to_dict(Job(spec=spec()))
    data.update(created_at=1_700_000_000, finished_at=1_700_000_100.5, percent=50, speed=1024.5, eta=12)
    job = job_from_dict(json.loads(json.dumps(data)))
    assert (job.created_at, job.finished_at, job.percent, job.speed, job.eta) == (1_700_000_000, 1_700_000_100.5, 50, 1024.5, 12)
    data.update(finished_at=None, percent=None, speed=None, eta=None)
    job = job_from_dict(data)
    assert (job.finished_at, job.percent, job.speed, job.eta) == (None, None, None, None)


def test_sponsorblock_categories_match_ytdlp():
    from yt_dlp.postprocessor.sponsorblock import SponsorBlockPP
    assert set(SPONSORBLOCK_CATEGORIES) == set(SponsorBlockPP.CATEGORIES)
    assert set(SPONSORBLOCK_REMOVABLE) == set(SponsorBlockPP.CATEGORIES) - set(SponsorBlockPP.NON_SKIPPABLE_CATEGORIES)


@pytest.mark.parametrize("extras", [
    Extras(),
    Extras(subtitles=True, subtitle_lang="en", sponsorblock=True, trim_start="0", trim_end="1:02:03.25"),
    Extras(sponsorblock=True, sponsorblock_mode=SponsorBlockMode.MARK, sponsorblock_categories=("chapter",)),
    Extras(sponsorblock=False, sponsorblock_categories=()),
    Extras(trim_start="0:59", trim_end="1:59:59.5"),
    Extras(trim_start="90", trim_end="125:00"),  # the first field is unbounded: 90 s, 125 min
    Extras(trim_start="1:05", trim_end="10:00:09"),
])
def test_valid_extras(extras):
    validate_extras(extras)


@pytest.mark.parametrize("extras", [
    Extras(subtitles=1),
    Extras(chapters="yes"),
    Extras(subtitle_lang=""),
    Extras(subtitle_lang=5),
    Extras(sponsorblock=True, sponsorblock_categories=()),
    Extras(sponsorblock_categories=("sponsor", "ads")),
    Extras(sponsorblock_categories=("chapter",)),  # can be marked but not removed
    Extras(sponsorblock_categories=(1,)),
    Extras(trim_start="90s"),
    Extras(trim_start="1:30:00:00"),
    Extras(trim_end="١٢"),  # non-ASCII digits
    Extras(trim_start=90),
    Extras(trim_start="2:00", trim_end="1:00"),
    Extras(trim_end="1:60"),
    Extras(trim_end="1:75"),
    Extras(trim_end="1:60:00"),
    Extras(trim_end="1:00:60"),
    Extras(trim_end="1:99:99.5"),
])
def test_invalid_extras(extras):
    with pytest.raises(ValueError):
        validate_extras(extras)


def test_subtitles_skipped_round_trips_and_defaults_to_false():
    job = Job(spec=spec())
    assert job.subtitles_skipped is False
    job.subtitles_skipped = True
    data = job_to_dict(job)
    assert data["subtitles_skipped"] is True
    assert job_from_dict(json.loads(json.dumps(data))).subtitles_skipped is True
    del data["subtitles_skipped"]  # rows saved before the field existed
    assert job_from_dict(data).subtitles_skipped is False


@pytest.mark.parametrize("value", ["yes", 1, None])
def test_subtitles_skipped_must_be_true_or_false(value):
    data = job_to_dict(Job(spec=spec()))
    data["subtitles_skipped"] = value
    with pytest.raises(ValueError, match="subtitles_skipped"):
        job_from_dict(data)


# --- the audio stream's container and codec, and the original-audio choice ---

def test_best_audio_is_audio_only():
    assert FormatChoice(ChoiceKind.BEST_AUDIO).audio_only
    assert ChoiceKind("best_audio") is ChoiceKind.BEST_AUDIO


def test_choice_keeps_the_audio_container_and_codec():
    choice = FormatChoice(ChoiceKind.VIDEO, "399", "251", 1080, "mp4", "av01.0.08M.08", "webm", "opus")
    assert (choice.audio_ext, choice.audio_codec) == ("webm", "opus")
    data = json.loads(json.dumps(choice_to_dict(choice)))
    assert (data["audio_ext"], data["audio_codec"]) == ("webm", "opus")
    assert choice_from_dict(data) == choice
    del data["audio_ext"], data["audio_codec"]  # rows saved before the fields existed
    restored = choice_from_dict(data)
    assert (restored.audio_ext, restored.audio_codec) == (None, None)


def test_best_audio_choice_round_trips():
    choice = FormatChoice(ChoiceKind.BEST_AUDIO)
    assert choice_from_dict(json.loads(json.dumps(choice_to_dict(choice)))) == choice


# --- choosing a row of the format list ---

from snag.info.formats import build_format_lists  # noqa: E402
from snag.jobs.models import choice_for_row, summary_base  # noqa: E402

AUDIO_FORMATS = [
    {"format_id": "140", "ext": "m4a", "protocol": "https", "vcodec": "none", "acodec": "mp4a.40.2", "abr": 129.5},
    {"format_id": "251", "ext": "webm", "protocol": "https", "vcodec": "none", "acodec": "opus", "abr": 135.0},
]
VIDEO_FORMATS = [
    {"format_id": "299", "ext": "mp4", "protocol": "https", "vcodec": "avc1.64002A", "acodec": "none", "height": 1080},
    {"format_id": "303", "ext": "webm", "protocol": "https", "vcodec": "vp9", "acodec": "none", "height": 1080},
    {"format_id": "18", "ext": "mp4", "protocol": "https", "vcodec": "avc1.42001E", "acodec": "mp4a.40.2", "height": 360},
]


def _rows():
    lists = build_format_lists(AUDIO_FORMATS + VIDEO_FORMATS, 100.0)
    return {row.format_id: row for row in lists.video_all + lists.audio}


def test_choice_for_a_video_row_takes_its_paired_audio():
    rows = _rows()
    assert choice_for_row(rows["299"]) == FormatChoice(
        ChoiceKind.VIDEO, "299", "140", 1080, "mp4", "avc1.64002A", "m4a", "mp4a.40.2",
    )
    assert choice_for_row(rows["303"]) == FormatChoice(ChoiceKind.VIDEO, "303", "251", 1080, "webm", "vp9", "webm", "opus")


def test_choice_for_a_video_row_with_another_audio_track():
    rows = _rows()
    assert choice_for_row(rows["299"], rows["251"]) == FormatChoice(
        ChoiceKind.VIDEO, "299", "251", 1080, "mp4", "avc1.64002A", "webm", "opus",
    )


def test_choice_for_a_combined_row_has_no_separate_audio():
    assert choice_for_row(_rows()["18"]) == FormatChoice(ChoiceKind.VIDEO, "18", None, 360, "mp4", "avc1.42001E")


def test_choice_for_audio_and_mp3_rows():
    rows = _rows()
    assert choice_for_row(rows["251"]) == FormatChoice(
        ChoiceKind.AUDIO, "251", ext="webm", audio_ext="webm", audio_codec="opus",
    )
    assert choice_for_row(rows["140"]) == FormatChoice(
        ChoiceKind.AUDIO, "140", ext="m4a", audio_ext="m4a", audio_codec="mp4a.40.2",
    )
    assert choice_for_row(rows["mp3"]) == FormatChoice(ChoiceKind.MP3)


def test_choice_records_the_language_of_the_audio_it_uses():
    dub = {"format_id": "140-1", "ext": "m4a", "protocol": "https", "vcodec": "none", "acodec": "mp4a.40.2",
           "abr": 129.5, "language": "de", "language_preference": -1}
    english = {"language": "en", "language_preference": 10}
    lists = build_format_lists(
        [{**f, **english} for f in AUDIO_FORMATS] + [dub] + [{**f, **english} for f in VIDEO_FORMATS], 100.0,
    )
    rows = {row.format_id: row for row in lists.video_all + lists.audio}
    assert choice_for_row(rows["299"]).audio_language == "en"  # the paired track
    assert choice_for_row(rows["299"], rows["140-1"]).audio_language == "de"  # a deliberately chosen dub
    assert choice_for_row(rows["140-1"]).audio_language == "de"
    assert choice_for_row(rows["18"]).audio_language == "en"  # a combined format's own audio
    assert choice_for_row(rows["mp3"]).audio_language is None  # yt-dlp picks the audio


def test_choice_keeps_the_audio_language():
    choice = FormatChoice(ChoiceKind.VIDEO, "299", "140-1", 1080, "mp4", "avc1.64002A", "m4a", "mp4a.40.2", "de")
    data = json.loads(json.dumps(choice_to_dict(choice)))
    assert data["audio_language"] == "de"
    assert choice_from_dict(data) == choice
    del data["audio_language"]  # rows saved before the field existed
    assert choice_from_dict(data).audio_language is None


@pytest.mark.parametrize("row_id, audio_id", [("18", "140"), ("299", "303"), ("299", "mp3"), ("140", "251"), ("mp3", "140")])
def test_choice_for_row_rejects_an_audio_override_that_cannot_apply(row_id, audio_id):
    rows = _rows()
    with pytest.raises(ValueError):
        choice_for_row(rows[row_id], rows[audio_id])


@pytest.mark.parametrize("choice, ext, text", [
    (FormatChoice(ChoiceKind.MP3), "MP3", "MP3"),
    (FormatChoice(ChoiceKind.AUDIO, "251"), "OPUS", "Audio OPUS"),
    (FormatChoice(ChoiceKind.AUDIO, "251"), "", "Audio"),
    (FormatChoice(ChoiceKind.BEST_AUDIO), "OPUS", "Original audio OPUS"),
    (FormatChoice(ChoiceKind.BEST_AUDIO), "", "Original audio"),
    (FormatChoice(ChoiceKind.QUICK, height=720), "MKV", "Up to 720p MKV"),
    (FormatChoice(ChoiceKind.QUICK), "", "Best quality"),
    (FormatChoice(ChoiceKind.VIDEO, "299", height=1080), "MP4", "1080p MP4"),
    (FormatChoice(ChoiceKind.VIDEO, "x"), "WEBM", "WEBM"),
    (FormatChoice(ChoiceKind.VIDEO, "x"), "", "Video"),
])
def test_summary_base(choice, ext, text):
    assert summary_base(choice, ext) == text
