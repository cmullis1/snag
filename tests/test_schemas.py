import json
import time
from dataclasses import replace
from pathlib import Path

import pytest

from snag.checks import DENO_MISSING, ToolCheck, ToolStatus
from snag.errors import ErrorKind, make_error
from snag.info.lookup import classify_url, parse_info
from snag.info.models import PlaylistInfo, VideoInfo
from snag.jobs.models import ChoiceKind, Extras, FormatChoice, Job, JobState, SubtitleMode, extras_to_dict, job_to_dict
from snag.jobs.queue import BatchSummary, QueueState
from snag.server.schemas import (
    NOT_VALID, InvalidExtras, batch_summary_json, error_json, format_row_json, history_entry_json, job_json,
    parse_extras, parse_job_request, playlist_info_json, queue_state_json, tool_status_json, video_info_json,
)
from snag.store.history import HistoryEntry
from tests.helpers import make_spec

SAMPLES = Path(__file__).parent / "samples"


def sample(name: str):
    data = json.loads((SAMPLES / f"{name}.json").read_text(encoding="utf-8"))
    return parse_info(data, data["webpage_url"], classify_url(data["webpage_url"]))


@pytest.fixture(scope="module")
def bbb() -> VideoInfo:
    return sample("youtube_bbb")


@pytest.fixture(scope="module")
def dubbed() -> VideoInfo:
    return sample("youtube_dubbed")


@pytest.fixture(scope="module")
def archive() -> VideoInfo:
    return sample("archive_org")


@pytest.fixture(scope="module")
def playlist() -> PlaylistInfo:
    return sample("youtube_playlist")


def assert_plain_json(value) -> None:
    """Survives a real JSON round trip unchanged: only dicts, lists, str, numbers, bools and None
    (a tuple would come back as a list, a Path or enum wouldn't serialise)."""
    assert json.loads(json.dumps(value, allow_nan=False)) == value


ENTRY = HistoryEntry(
    id=7, site="youtube", video_id="aqz-KE-bpKQ", url="https://www.youtube.com/watch?v=aqz-KE-bpKQ",
    title="Big Buck Bunny", channel="Blender", kind="video", format_summary="1080p MP4 + subtitles",
    file_path="C:\\Videos\\Snag\\Big Buck Bunny.mp4", size=1234, finished_at=1_760_000_000.5, status="done",
    error_message=None,
)


# --- lookups ---

def test_video_info_json(bbb):
    data = video_info_json(bbb, None)
    assert_plain_json(data)
    assert data["type"] == "video"
    assert (data["site"], data["video_id"], data["url"], data["title"]) == (bbb.site, bbb.video_id, bbb.url, bbb.title)
    assert data["duplicate"] is None
    assert data["live_status"] == "not_live"
    assert data["js_runtime_missing"] is False
    assert data["subtitle_langs"] == list(bbb.subtitle_langs)
    assert data["auto_subtitle_langs"] == list(bbb.auto_subtitle_langs)
    assert data["has_chapters"] is bbb.has_chapters
    formats = data["formats"]
    for name in ("video_simple", "video_all", "audio", "audio_tracks"):
        assert [row["format_id"] for row in formats[name]] == [r.format_id for r in getattr(bbb.formats, name)]
    assert formats["audio"][0]["kind"] == "mp3"


def test_video_info_json_carries_the_duplicate_and_flags(bbb):
    data = video_info_json(replace(bbb, js_runtime_missing=True, live_status="is_live"), ENTRY)
    assert data["duplicate"] == history_entry_json(ENTRY)
    assert data["js_runtime_missing"] is True
    assert data["live_status"] == "is_live"


def test_video_info_json_playlist_ref(bbb):
    url = "https://www.youtube.com/watch?v=aqz-KE-bpKQ&list=PL1"
    data = video_info_json(replace(bbb, playlist=classify_url(url).playlist), None)
    assert data["playlist"] == {"playlist_id": "PL1", "url": "https://www.youtube.com/playlist?list=PL1"}
    assert video_info_json(bbb, None)["playlist"] is None


def test_format_row_json(bbb):
    row = bbb.formats.video_simple[0]
    data = format_row_json(row)
    assert_plain_json(data)
    assert data["format_id"] == row.format_id and data["label"] == row.label
    assert data["height"] == row.height and data["size"] == row.size and data["result_ext"] == row.result_ext
    assert data["audio_id"] == row.audio_id and data["kind"] == "video"


def test_playlist_info_json(playlist):
    data = playlist_info_json(playlist)
    assert_plain_json(data)
    assert data["type"] == "playlist"
    assert (data["site"], data["playlist_id"], data["title"]) == ("youtube", playlist.playlist_id, playlist.title)
    assert len(data["entries"]) == len(playlist.entries)
    first, entry = data["entries"][0], playlist.entries[0]
    assert first["index"] == 1 and first["video_id"] == entry.video_id and first["url"] == entry.url
    assert first["item_index"] is None and first["duplicate"] is None


def test_playlist_info_json_marks_downloaded_entries(playlist):
    downloaded = playlist.entries[1].video_id
    data = playlist_info_json(playlist, {downloaded: ENTRY})
    assert data["entries"][1]["duplicate"] == history_entry_json(ENTRY)
    assert [e["duplicate"] for i, e in enumerate(data["entries"]) if i != 1] == [None] * (len(playlist.entries) - 1)


# --- jobs, history, queue, tools, errors ---

def test_job_json_adds_what_the_ui_needs(tmp_path):
    job = Job(spec=make_spec("https://www.youtube.com/watch?v=abc123", tmp_path))
    data = job_json(job)
    assert_plain_json(data)
    for key, value in job_to_dict(job).items():
        assert data[key] == json.loads(json.dumps(value))
    assert data["kind"] == "video"
    assert data["summary"] == "1080p MP4"
    assert data["can_retry"] is False and data["can_clear"] is False


def test_job_json_for_audio_with_a_file(tmp_path):
    job = Job(spec=make_spec("u", tmp_path, choice=FormatChoice(ChoiceKind.MP3)), state=JobState.DONE)
    job.file_path = tmp_path / "Song.mp3"
    data = job_json(job)
    assert data["kind"] == "audio"
    assert data["summary"] == "MP3"
    assert data["file_path"] == str(tmp_path / "Song.mp3")
    assert data["can_retry"] is False and data["can_clear"] is True  # cleared alone, or by clear-finished


def test_job_json_summary_mentions_skipped_subtitles(tmp_path):
    job = Job(spec=make_spec("u", tmp_path, extras=Extras(subtitles=True)), state=JobState.DONE)
    job.subtitles_skipped = True
    assert job_json(job)["summary"] == "1080p MP4 + saved without subtitles"


@pytest.mark.parametrize("state, error, can_retry, can_clear", [
    (JobState.FAILED, make_error(ErrorKind.NETWORK), True, True),
    (JobState.FAILED, make_error(ErrorKind.UNAVAILABLE), False, True),
    (JobState.FAILED, None, True, True),
    (JobState.CANCELLED, None, True, True),
    (JobState.WAITING, None, False, False),
    (JobState.DOWNLOADING, None, False, False),
    (JobState.INTERRUPTED, None, False, False),
    (JobState.DONE, None, False, True),
])
def test_job_json_actions_follow_the_queue_rules(tmp_path, state, error, can_retry, can_clear):
    job = Job(spec=make_spec("u", tmp_path), state=state, error=error)
    data = job_json(job)
    assert (data["can_retry"], data["can_clear"]) == (can_retry, can_clear)
    if error is not None:
        assert data["error"] == error_json(error)


def test_history_entry_json():
    data = history_entry_json(ENTRY)
    assert_plain_json(data)
    assert data == {
        "id": 7, "site": "youtube", "video_id": "aqz-KE-bpKQ", "url": ENTRY.url, "title": "Big Buck Bunny",
        "channel": "Blender", "kind": "video", "format_summary": "1080p MP4 + subtitles",
        "file_path": ENTRY.file_path, "size": 1234, "finished_at": 1_760_000_000.5, "status": "done",
        "error_message": None,
    }


def test_queue_state_json():
    state = QueueState(
        paused=False, held=True, throttled=True, throttled_until=time.time() + 300, current_limit=1, running=1,
        waiting=4,
    )
    data = queue_state_json(state)
    assert_plain_json(data)
    assert data == {
        "paused": False, "held": True, "throttled": True, "throttled_until": state.throttled_until,
        "current_limit": 1, "running": 1, "waiting": 4,
    }


def test_batch_summary_json():
    data = batch_summary_json(BatchSummary(total=42, done=40, failed=2, cancelled=0, pending=0))
    assert data == {"total": 42, "done": 40, "failed": 2, "cancelled": 0, "pending": 0}


def test_tool_status_json():
    status = ToolStatus(
        ytdlp=ToolCheck(True, "2026.08.19"), ffmpeg=ToolCheck(True, "N-1"), ffprobe=ToolCheck(True, "N-1"),
        deno=ToolCheck(False, None, DENO_MISSING),
    )
    data = tool_status_json(status)
    assert_plain_json(data)
    assert data == {
        "ytdlp": {"present": True, "version": "2026.08.19", "message": None},
        "ffmpeg": {"present": True, "version": "N-1", "message": None},
        "ffprobe": {"present": True, "version": "N-1", "message": None},
        "deno": {"present": False, "version": None, "message": DENO_MISSING},
        "ready": True,
    }


def test_error_json():
    error = make_error(ErrorKind.SITE_CHANGED, "youtube", details="ERROR: nsig extraction failed")
    data = error_json(error)
    assert_plain_json(data)
    assert data == {
        "kind": "site_changed", "message": error.message, "action": "check_update", "can_retry": True,
        "keep_partial": error.keep_partial, "details": "ERROR: nsig extraction failed",
    }


# --- parsing a download request ---

def test_parse_a_simple_view_row(bbb):
    row = bbb.formats.video_simple[2]
    choice, extras, dest = parse_job_request({"format_id": row.format_id}, bbb)
    assert choice == FormatChoice(
        ChoiceKind.VIDEO, row.format_id, row.audio_id, row.height, row.ext, row.vcodec, row.audio_ext,
        row.audio_codec, row.language,
    )
    assert extras == Extras()
    assert dest is None


def test_parse_any_row_of_the_full_list(bbb):
    row = bbb.formats.video_all[-1]
    choice, _, _ = parse_job_request({"format_id": row.format_id}, bbb)
    assert (choice.kind, choice.format_id, choice.height) == (ChoiceKind.VIDEO, row.format_id, row.height)


def test_parse_mp3(bbb):
    choice, _, _ = parse_job_request({"format_id": "mp3"}, bbb)
    assert choice == FormatChoice(ChoiceKind.MP3)


def test_parse_an_audio_row(dubbed):
    row = next(r for r in dubbed.formats.audio_tracks if not r.is_original)
    choice, _, _ = parse_job_request({"format_id": row.format_id}, dubbed)
    assert choice.kind is ChoiceKind.AUDIO and choice.format_id == row.format_id
    assert choice.audio_language == row.language


def test_parse_a_video_row_with_another_audio_track(dubbed):
    video = next(r for r in dubbed.formats.video_all if not r.has_audio)
    dub = next(r for r in dubbed.formats.audio_tracks if not r.is_original)
    choice, _, _ = parse_job_request({"format_id": video.format_id, "audio_id": dub.format_id}, dubbed)
    assert choice.kind is ChoiceKind.VIDEO
    assert (choice.format_id, choice.audio_id) == (video.format_id, dub.format_id)
    assert (choice.audio_ext, choice.audio_codec, choice.audio_language) == (dub.ext, dub.acodec, dub.language)


def test_parse_a_null_audio_id_keeps_the_paired_track(bbb):
    row = bbb.formats.video_simple[0]
    choice, _, _ = parse_job_request({"format_id": row.format_id, "audio_id": None}, bbb)
    assert choice.audio_id == row.audio_id


def test_parse_extras_and_dest(bbb, tmp_path):
    extras = Extras(subtitles=True, subtitle_mode=SubtitleMode.FILE, chapters=True, trim_start="0:10")
    body = json.loads(json.dumps({"format_id": "mp3", "extras": extras_to_dict(extras), "dest_dir": str(tmp_path)}))
    _, parsed, dest = parse_job_request(body, bbb)
    assert parsed == extras
    assert dest == tmp_path


def test_parse_partial_extras_take_defaults_for_the_rest(bbb):
    _, extras, _ = parse_job_request({"format_id": "mp3", "extras": {"chapters": True}}, bbb)
    assert extras == Extras(chapters=True)


@pytest.mark.parametrize("body, words", [
    ({"format_id": "nope"}, "format"),
    ({}, "format"),
    ({"format_id": 140}, "format"),
    ({"format_id": "mp3", "audio_id": "nope"}, "audio"),
    ({"format_id": "mp3", "audio_id": "140"}, "audio"),  # MP3 can't take a track
    ({"format_id": "140", "audio_id": "251"}, "audio"),  # an audio format can't take a track
    ({"format_id": "401", "audio_id": "mp3"}, "audio"),  # the MP3 row isn't a track
    ({"format_id": "401", "audio_id": 251}, "audio"),
    ({"format_id": "mp3", "extras": "subtitles"}, "extras"),
    ({"format_id": "mp3", "extras": {"subtitle_mode": "sideways"}}, "extras"),
    ({"format_id": "mp3", "extras": {"trim_start": "1 minute"}}, "trim"),
    ({"format_id": "mp3", "extras": {"subtitles": "yes"}}, "extras"),
    ({"format_id": "mp3", "dest_dir": "relative\\folder"}, "folder"),
    ({"format_id": "mp3", "dest_dir": ""}, "folder"),
    ({"format_id": "mp3", "dest_dir": 5}, "folder"),
])
def test_parse_rejects_bad_requests_with_a_plain_message(bbb, body, words):
    with pytest.raises(ValueError) as raised:
        parse_job_request(body, bbb)
    assert words in str(raised.value).lower()


@pytest.mark.parametrize("extras, message", [
    ({"trim_start": "2:00", "trim_end": "1:00"}, "The trim end must come after the start."),
    ({"trim_start": "1:00", "trim_end": "1:00"}, "The trim end must come after the start."),
    ({"trim_start": "1 minute"}, "Trim times look like 1:30 or 1:02:03."),
    ({"trim_end": "1:99"}, "Trim times look like 1:30 or 1:02:03."),
    ({"trim_end": 90}, "Trim times look like 1:30 or 1:02:03."),
    ({"trim_start": "9" * 5000}, "Trim times look like 1:30 or 1:02:03."),
    ({"sponsorblock": True, "sponsorblock_categories": []}, "Pick at least one SponsorBlock category."),
    ({"subtitles": "yes"}, NOT_VALID),
    ({"subtitle_mode": "sideways"}, NOT_VALID),
    ({"sponsorblock_categories": ["ads"]}, NOT_VALID),
    ("subtitles", NOT_VALID),
    ({"chapters": True, "trim_middle": "1:00", "loud": 1}, "Snag doesn't know these extras: loud, trim_middle."),
])
def test_bad_extras_say_what_to_fix_in_plain_words(extras, message):
    with pytest.raises(InvalidExtras) as raised:
        parse_extras(extras)
    assert raised.value.message == message and str(raised.value) == message
    assert raised.value.details


def test_parse_extras_takes_none_and_good_extras():
    assert parse_extras(None) == Extras()
    good = Extras(sponsorblock=True, trim_start="0:30", trim_end="1:02:03.5")
    assert parse_extras(extras_to_dict(good)) == good


def test_a_download_request_with_bad_extras_raises_invalid_extras(bbb):
    with pytest.raises(InvalidExtras, match="trim end"):
        parse_job_request({"format_id": "mp3", "extras": {"trim_start": "5", "trim_end": "4"}}, bbb)


@pytest.mark.parametrize("folder", ["\\\\.\\C:\\Videos", "C:\\Videos\\NUL"])
def test_parse_refuses_device_folders(bbb, folder):
    with pytest.raises(ValueError, match="device"):
        parse_job_request({"format_id": "mp3", "dest_dir": folder}, bbb)


def test_parse_takes_a_network_share(bbb):
    share = "\\\\nas\\media"
    assert parse_job_request({"format_id": "mp3", "dest_dir": share}, bbb)[2] == Path(share)


def test_parse_a_combined_format_cannot_take_another_track(archive):
    combined = next(r for r in archive.formats.video_all if r.has_audio)
    with pytest.raises(ValueError):
        parse_job_request({"format_id": combined.format_id, "audio_id": "anything"}, archive)


@pytest.mark.parametrize("body", [None, [], "mp3"])
def test_parse_rejects_a_body_that_is_not_an_object(bbb, body):
    with pytest.raises(ValueError):
        parse_job_request(body, bbb)


def test_nan_and_infinity_become_null(bbb, playlist):
    import json
    import math
    from dataclasses import replace

    from snag.info.models import FormatLists

    nan, inf = math.nan, math.inf
    rows = bbb.formats.video_all
    odd = replace(rows[0], size=inf, abr=nan, fps=-inf, tbr=nan)
    formats = replace(bbb.formats, video_all=(odd, *rows[1:]))
    assert isinstance(formats, FormatLists)
    data = video_info_json(replace(bbb, duration=nan, formats=formats), None)
    json.dumps(data, allow_nan=False)
    assert data["duration"] is None
    row = data["formats"]["video_all"][0]
    assert (row["size"], row["abr"], row["fps"], row["tbr"]) == (None, None, None, None)
    entry = replace(playlist.entries[0], duration=inf)
    json.dumps(playlist_info_json(replace(playlist, entries=(entry,))), allow_nan=False)
    assert format_row_json(odd)["size"] is None
