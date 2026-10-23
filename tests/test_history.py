import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from snag.errors import ErrorKind, make_error
from snag.jobs.models import ChoiceKind, Extras, FormatChoice, Job, JobState
from snag.jobs.naming import safe_name
from snag.store.db import open_db
from snag.store.history import HistoryStore, format_summary
from tests.helpers import make_spec


@pytest.fixture
def history(tmp_path):
    conn = open_db(tmp_path / "snag.db")
    yield HistoryStore(conn)
    conn.close()


def finished(tmp_path, state=JobState.DONE, finished_at=100.0, title="Fake Video", **spec_overrides):
    job = Job(spec=make_spec("https://youtu.be/abc123", tmp_path, title=title, **spec_overrides))
    job.state = state
    job.finished_at = finished_at
    if state is JobState.DONE:
        job.file_path = tmp_path / f"{safe_name(title)}.mp4"
        job.file_path.write_bytes(b"x" * 10)
    else:
        job.error = make_error(ErrorKind.NETWORK, "youtube")
    return job


def test_format_summary(tmp_path):
    spec = make_spec("u", tmp_path, extras=Extras(subtitles=True, sponsorblock=True))
    assert format_summary(spec, tmp_path / "A.mp4") == "1080p MP4 + subtitles + SponsorBlock"
    assert format_summary(make_spec("u", tmp_path, choice=FormatChoice(ChoiceKind.MP3)), tmp_path / "A.mp3") == "MP3"
    quick = make_spec("u", tmp_path, choice=FormatChoice(ChoiceKind.QUICK, height=720), extras=Extras(trim_start="0:10"))
    assert format_summary(quick, tmp_path / "A.mkv") == "Up to 720p MKV + trimmed"
    audio = make_spec("u", tmp_path, choice=FormatChoice(ChoiceKind.AUDIO, "251", ext="webm"))
    assert format_summary(audio, tmp_path / "A.opus") == "Audio OPUS"
    best_audio = make_spec("u", tmp_path, choice=FormatChoice(ChoiceKind.BEST_AUDIO), extras=Extras(subtitles=True))
    assert format_summary(best_audio, tmp_path / "A.m4a") == "Original audio M4A"


def test_format_summary_when_subtitles_were_skipped(tmp_path):
    spec = make_spec("u", tmp_path, extras=Extras(subtitles=True, sponsorblock=True))
    assert format_summary(spec, tmp_path / "A.mp4", subtitles_skipped=True) == (
        "1080p MP4 + saved without subtitles + SponsorBlock"
    )


def test_history_records_a_download_saved_without_subtitles(tmp_path, history):
    job = finished(tmp_path, extras=Extras(subtitles=True))
    job.subtitles_skipped = True
    history.add(job)
    assert history.list()[0].format_summary == "1080p MP4 + saved without subtitles"


def test_add_and_list(tmp_path, history):
    history.add(finished(tmp_path))
    history.add(finished(tmp_path, state=JobState.FAILED, finished_at=200.0, title="Broken"))
    entries = history.list()
    assert [e.title for e in entries] == ["Broken", "Fake Video"]  # newest first
    done = entries[1]
    assert done.status == "done" and done.size == 10 and done.kind == "video"
    assert done.format_summary == "1080p MP4"
    assert entries[0].status == "failed" and entries[0].error_message == "Lost connection"


def test_search_and_kind_filter(tmp_path, history):
    history.add(finished(tmp_path, title="Cats 100%"))
    history.add(finished(tmp_path, title="Dogs", choice=FormatChoice(ChoiceKind.MP3)))
    assert [e.title for e in history.list(search="100%")] == ["Cats 100%"]
    assert [e.title for e in history.list(kind="audio")] == ["Dogs"]


def test_count_matches_what_list_would_page_through(tmp_path, history):
    assert history.count() == 0
    for n in range(3):
        history.add(finished(tmp_path, title=f"Cats {n}"))
    history.add(finished(tmp_path, title="Dogs", choice=FormatChoice(ChoiceKind.MP3)))
    history.add(finished(tmp_path, title="CATS on 100%", choice=FormatChoice(ChoiceKind.MP3)))
    assert history.count() == 5 == len(history.list())
    assert history.count(search="  cats ") == 4  # same matching as list(): trimmed, any case
    assert history.count(search="100%") == 1  # literal, not a wildcard
    assert history.count(kind="audio") == 2
    assert history.count(search="cats", kind="audio") == 1
    assert history.count(kind="video") == 3
    assert history.count(search="nothing like this") == 0


def test_find_duplicate_returns_latest_success(tmp_path, history):
    assert history.find_duplicate("youtube", "abc123") is None
    history.add(finished(tmp_path, finished_at=100.0))
    history.add(finished(tmp_path, finished_at=300.0))
    history.add(finished(tmp_path, state=JobState.FAILED, finished_at=400.0))
    assert history.find_duplicate("youtube", "abc123").finished_at == 300.0


def test_delete_and_clear_keep_files(tmp_path, history):
    job = finished(tmp_path)
    entry_id = history.add(job)
    history.add(finished(tmp_path))
    history.delete(entry_id)
    assert len(history.list()) == 1
    assert history.clear() == 1
    assert history.list() == []
    assert job.file_path.exists()


def test_format_summary_is_never_empty(tmp_path):
    video = make_spec("u", tmp_path, choice=FormatChoice(ChoiceKind.VIDEO, "299"))
    assert format_summary(video, None) == "Video"
    audio = make_spec("u", tmp_path, choice=FormatChoice(ChoiceKind.AUDIO, "251"))
    assert format_summary(audio, None) == "Audio"
    assert format_summary(video, tmp_path / "noextension") == "Video"


def test_format_summary_ignores_odd_suffixes(tmp_path):
    video = make_spec("u", tmp_path, choice=FormatChoice(ChoiceKind.VIDEO, "299", height=720, ext="mp4"))
    assert format_summary(video, tmp_path / "A.part-Frag3") == "720p MP4"  # falls back to the chosen ext
    assert format_summary(video, tmp_path / "A. weird") == "720p MP4"
    assert format_summary(video, tmp_path / "A.toolongext") == "720p MP4"
    assert format_summary(video, tmp_path / "A.mkv") == "720p MKV"


def test_format_summary_without_a_file_names_the_container_the_file_would_have_had(tmp_path):
    av1_opus = FormatChoice(ChoiceKind.VIDEO, "399", "251", 1080, "mp4", "av01.0.08M.08", "webm", "opus")
    assert format_summary(make_spec("u", tmp_path, choice=av1_opus), None) == "1080p WEBM"  # the merge's, not MP4
    assert format_summary(make_spec("u", tmp_path, choice=av1_opus), tmp_path / "A.part-Frag3") == "1080p WEBM"
    opus = FormatChoice(ChoiceKind.AUDIO, "251", ext="webm", audio_ext="webm", audio_codec="opus")
    assert format_summary(make_spec("u", tmp_path, choice=opus), None) == "Audio OPUS"  # extracted from the WebM
    # Saved before the audio's container was recorded: the merge could give several, so the chosen ext.
    unknown = FormatChoice(ChoiceKind.VIDEO, "399", "251", 1080, "mp4", "av01.0.08M.08")
    assert format_summary(make_spec("u", tmp_path, choice=unknown), None) == "1080p MP4"
    assert format_summary(make_spec("u", tmp_path, choice=av1_opus), tmp_path / "A.mkv") == "1080p MKV"  # the file wins


def test_failed_job_summary_names_the_merged_container(history, tmp_path):
    choice = FormatChoice(ChoiceKind.VIDEO, "399", "251", 1080, "mp4", "av01.0.08M.08", "webm", "opus")
    history.add(finished(tmp_path, state=JobState.FAILED, choice=choice))
    assert history.list()[0].format_summary == "1080p WEBM"


def test_format_summary_quick_without_height(tmp_path):
    quick = make_spec("u", tmp_path, choice=FormatChoice(ChoiceKind.QUICK))
    assert format_summary(quick, tmp_path / "A.mp4") == "Best quality MP4"
    assert format_summary(quick, None) == "Best quality"


@pytest.mark.parametrize("state", [JobState.WAITING, JobState.DOWNLOADING, JobState.CANCELLED, JobState.INTERRUPTED])
def test_add_only_accepts_finished_jobs(tmp_path, history, state):
    job = finished(tmp_path)
    job.state = state
    with pytest.raises(ValueError):
        history.add(job)
    assert history.list() == []


def test_add_uses_now_when_finish_time_missing(tmp_path, history):
    job = finished(tmp_path)
    job.finished_at = None
    before = time.time()
    history.add(job)
    assert before <= history.list()[0].finished_at <= time.time()


def test_add_returns_the_new_id(tmp_path, history):
    first = history.add(finished(tmp_path))
    second = history.add(finished(tmp_path))
    assert second == first + 1
    assert [e.id for e in history.list()] == [second, first]


def test_search_is_literal_for_wildcards_and_backslashes(tmp_path, history):
    for title in ["a_b", "axb", "c\\d", "c%d", "cxd"]:
        history.add(finished(tmp_path, title=title))
    assert [e.title for e in history.list(search="a_b")] == ["a_b"]
    assert [e.title for e in history.list(search="c\\d")] == ["c\\d"]
    assert [e.title for e in history.list(search="c%d")] == ["c%d"]


def test_search_ignores_case_including_non_ascii(tmp_path, history):
    history.add(finished(tmp_path, title="ÉCOLE Straße"))
    history.add(finished(tmp_path, title="Other"))
    assert [e.title for e in history.list(search="école")] == ["ÉCOLE Straße"]
    assert [e.title for e in history.list(search="STRASSE")] == ["ÉCOLE Straße"]


def test_blank_search_lists_everything(tmp_path, history):
    history.add(finished(tmp_path, title="A"))
    history.add(finished(tmp_path, title="B"))
    assert len(history.list(search="   ")) == 2
    assert [e.title for e in history.list(search="  a ")] == ["A"]


def test_limit_and_offset(tmp_path, history):
    for n in range(5):
        history.add(finished(tmp_path, title=f"T{n}", finished_at=float(n)))
    assert [e.title for e in history.list(limit=2)] == ["T4", "T3"]
    assert [e.title for e in history.list(limit=2, offset=2)] == ["T2", "T1"]
    assert [e.title for e in history.list(limit=2, offset=4)] == ["T0"]


def test_duplicate_check_is_per_site(tmp_path, history):
    history.add(finished(tmp_path, site="vimeo"))
    assert history.find_duplicate("youtube", "abc123") is None
    assert history.find_duplicate("vimeo", "abc123").site == "vimeo"


def test_entries_survive_extra_columns(tmp_path, history):
    history._conn.execute("ALTER TABLE history ADD COLUMN added_later TEXT")
    history.add(finished(tmp_path))
    assert history.list()[0].title == "Fake Video"


def test_concurrent_adds(tmp_path, history):
    jobs = [finished(tmp_path, title=f"T{n}") for n in range(40)]
    ids: list[int] = []
    threads = [threading.Thread(target=lambda j=job: ids.append(history.add(j))) for job in jobs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(set(ids)) == 40 and len(history.list(limit=100)) == 40


# Runs in a child process: the deadlock this guards against hangs the whole interpreter (one thread
# waits for SQLite's connection mutex while holding the GIL), so no in-process timeout could fire.
_SHARED_CONNECTION_SCRIPT = textwrap.dedent("""
    import sys, threading, time
    from pathlib import Path
    from snag.store.db import open_db
    from snag.store.history import HistoryStore
    from snag.store.jobs import JobStore
    from snag.store.settings import SettingsStore, default_settings

    folder = Path(sys.argv[1])
    conn = open_db(folder / "snag.db")
    with conn.lock:
        conn.executemany(
            "INSERT INTO history(site, video_id, url, title, kind, format_summary, finished_at, status)"
            " VALUES('youtube', 'v', 'u', ?, 'video', 'MP4', 1, 'done')",
            [(f"title {n}",) for n in range(2000)],
        )
    stop = threading.Event()
    searches = 0

    def search():
        global searches
        while not stop.is_set():
            HistoryStore(conn).list(search="t")
            searches += 1

    reader = threading.Thread(target=search, daemon=True)
    reader.start()
    defaults = default_settings(folder, folder)
    made = 0
    # Make stores until the reader has finished a few searches, so plenty are made mid-search. Each
    # search calls back into Python for every row, so yield the GIL after each round: without that,
    # on a busy machine the reader barely progresses (starvation, not the deadlock).
    deadline = time.monotonic() + 60
    while searches < 3 and time.monotonic() < deadline:
        HistoryStore(conn)
        SettingsStore(conn, defaults)
        JobStore(conn)
        made += 1
        time.sleep(0)
    stop.set()
    reader.join(60)
    print("alive" if reader.is_alive() else f"ok {searches} {made}")
""")


def test_making_stores_while_another_thread_searches_does_not_deadlock(tmp_path):
    for _ in range(3):
        try:
            result = subprocess.run(
                [sys.executable, "-c", _SHARED_CONNECTION_SCRIPT, str(tmp_path)],
                cwd=Path(__file__).parents[1], capture_output=True, text=True, timeout=180,
            )
        except subprocess.TimeoutExpired:
            pytest.fail("creating stores while another thread searched history hung the process")
        assert result.returncode == 0, result.stderr
        status, searches, made = result.stdout.split()
        assert status == "ok" and int(searches) >= 3 and int(made) > 0, result.stdout
        (tmp_path / "snag.db").unlink()


# --- one entry per download ---

def test_a_retried_download_that_succeeds_has_one_done_entry(tmp_path, history):
    job = finished(tmp_path, state=JobState.FAILED, finished_at=100.0)
    first = history.add(job)
    job.state, job.error, job.finished_at = JobState.DONE, None, 200.0
    job.file_path = tmp_path / "Fake Video.mp4"
    job.file_path.write_bytes(b"x" * 10)
    assert history.add(job) == first
    [entry] = history.list()
    assert (entry.id, entry.status, entry.error_message) == (first, "done", None)
    assert (entry.finished_at, entry.size, entry.file_path) == (200.0, 10, str(job.file_path))
    assert history.find_duplicate("youtube", "abc123").id == first


def test_a_retried_download_that_fails_again_has_one_failed_entry(tmp_path, history):
    job = finished(tmp_path, state=JobState.FAILED, finished_at=100.0)
    first = history.add(job)
    job.finished_at = 200.0
    assert history.add(job) == first
    [entry] = history.list()
    assert (entry.status, entry.finished_at) == ("failed", 200.0)


def test_a_retry_after_its_entry_was_deleted_gets_a_new_one(tmp_path, history):
    job = finished(tmp_path, state=JobState.FAILED)
    history.delete(history.add(job))
    job.finished_at = 200.0
    history.add(job)
    assert [e.finished_at for e in history.list()] == [200.0]


def test_other_downloads_of_the_same_video_get_their_own_entries(tmp_path, history):
    history.add(finished(tmp_path))
    history.add(finished(tmp_path))
    assert len(history.list()) == 2


def test_entries_from_before_job_ids_stay_separate(tmp_path, history):
    for title in ("Old one", "Old two"):
        history._conn.execute(
            "INSERT INTO history(site, video_id, url, title, kind, format_summary, finished_at, status)"
            " VALUES('youtube', 'v', 'u', ?, 'video', 'MP4', 1, 'done')",
            (title,),
        )
    history.add(finished(tmp_path))
    assert len(history.list()) == 3


# --- what a trimmed download really got ---

def test_a_trim_with_a_start_time_gets_no_subtitles(tmp_path):
    spec = make_spec("u", tmp_path, extras=Extras(subtitles=True, trim_start="1:30"))
    assert format_summary(spec, tmp_path / "A.mp4") == "1080p MP4 + trimmed"
    skipped = format_summary(spec, tmp_path / "A.mp4", subtitles_skipped=True)
    assert skipped == "1080p MP4 + trimmed"


def test_an_end_only_trim_keeps_subtitles(tmp_path):
    for extras in (Extras(subtitles=True, trim_end="2:00"), Extras(subtitles=True, trim_start="0", trim_end="2:00")):
        spec = make_spec("u", tmp_path, extras=extras)
        assert format_summary(spec, tmp_path / "A.mp4") == "1080p MP4 + subtitles + trimmed"


def test_a_trim_gets_no_sponsorblock(tmp_path):
    for extras in (Extras(sponsorblock=True, trim_start="10"), Extras(sponsorblock=True, trim_end="20")):
        spec = make_spec("u", tmp_path, extras=extras)
        assert format_summary(spec, tmp_path / "A.mp4") == "1080p MP4 + trimmed"


def test_sponsorblock_without_categories_is_not_claimed(tmp_path):
    spec = make_spec("u", tmp_path, extras=Extras(sponsorblock=True, sponsorblock_categories=()))
    assert format_summary(spec, tmp_path / "A.mp4") == "1080p MP4"


_EXTRAS_CASES = [
    Extras(subtitles=True, sponsorblock=True),
    Extras(subtitles=True, chapters=True, thumbnail_tags=True),
    Extras(subtitles=True, chapters=True, thumbnail_tags=True, trim_end="1:00"),
    Extras(subtitles=True, sponsorblock=True, trim_start="1:30"),
    Extras(subtitles=True, sponsorblock=True, trim_start="0"),
    Extras(subtitles=True, sponsorblock=True, trim_start="0.5"),
    Extras(subtitles=True, sponsorblock=True, trim_start="0:00", trim_end="1:00"),
    Extras(subtitles=True, sponsorblock=True, trim_end="1:00"),
    Extras(subtitles=True, sponsorblock=True, trim_start="1:02:03.5", trim_end="1:03:00"),
    Extras(subtitles=True, sponsorblock=True, sponsorblock_categories=()),
    Extras(subtitles=False, sponsorblock=False, trim_start="10"),
]
_CHOICES = [
    FormatChoice(ChoiceKind.VIDEO, "299", "140", 1080, "mp4"),
    FormatChoice(ChoiceKind.QUICK, height=720),
    FormatChoice(ChoiceKind.AUDIO, "140", ext="m4a"),
    FormatChoice(ChoiceKind.MP3),
]


@pytest.mark.parametrize("choice", _CHOICES)
@pytest.mark.parametrize("extras", _EXTRAS_CASES)
def test_the_summary_rules_match_the_command(tmp_path, extras, choice):
    from snag.jobs.command import build_command
    from snag.paths import Tools
    from snag.jobs.models import gets_chapters, gets_sponsorblock, gets_subtitles

    spec = make_spec("u", tmp_path, extras=extras, choice=choice)
    tools = Tools(ytdlp=("yt-dlp.exe",), ffmpeg_dir=tmp_path, deno=tmp_path / "deno.exe")
    args = build_command(spec, tools, tmp_path / "work", None)
    assert gets_subtitles(extras, choice) == ("--write-subs" in args)
    assert gets_sponsorblock(extras) == any(a.startswith("--sponsorblock-") for a in args)
    assert gets_chapters(extras) == ("--embed-chapters" in args)


# --- batches ---

def test_entries_remember_their_batch(tmp_path, history):
    done = finished(tmp_path, batch_id="b1")
    failed = finished(tmp_path, state=JobState.FAILED, batch_id="b1")
    other = finished(tmp_path, batch_id="b2")
    alone = finished(tmp_path)
    for job in (done, failed, other, alone):
        history.add(job)
    assert history.batch_statuses("b1") == {done.id: "done", failed.id: "failed"}
    assert history.batch_statuses("b2") == {other.id: "done"}
    assert history.batch_statuses("missing") == {}


def test_a_retried_batch_entry_keeps_one_status(tmp_path, history):
    job = finished(tmp_path, state=JobState.FAILED, batch_id="b1")
    history.add(job)
    job.state, job.error, job.file_path = JobState.DONE, None, tmp_path / "Fake Video.mp4"
    history.add(job)
    assert history.batch_statuses("b1") == {job.id: "done"}


def test_a_deleted_entry_leaves_its_batch(tmp_path, history):
    job = finished(tmp_path, batch_id="b1")
    entry_id = history.add(job)
    history.delete(entry_id)
    assert history.batch_statuses("b1") == {}


def test_get_returns_one_entry(tmp_path, history):
    entry_id = history.add(finished(tmp_path, finished_at=100.0))
    assert history.get(entry_id).finished_at == 100.0
    assert history.get(entry_id + 1) is None


def test_delete_says_whether_there_was_an_entry(tmp_path, history):
    entry_id = history.add(finished(tmp_path))
    assert history.delete(entry_id) is True
    assert history.delete(entry_id) is False


def test_find_duplicates_returns_each_videos_latest_success(tmp_path, history):
    history.add(finished(tmp_path, finished_at=100.0, video_id="v1"))
    history.add(finished(tmp_path, finished_at=300.0, video_id="v1"))
    history.add(finished(tmp_path, state=JobState.FAILED, finished_at=400.0, video_id="v1"))
    history.add(finished(tmp_path, state=JobState.FAILED, finished_at=400.0, video_id="v2"))
    history.add(finished(tmp_path, finished_at=200.0, video_id="v3", site="vimeo"))
    found = history.find_duplicates("youtube", ["v1", "v2", "v3", "v4"])
    assert {video_id: entry.finished_at for video_id, entry in found.items()} == {"v1": 300.0}
    assert history.find_duplicates("youtube", []) == {}


def test_find_duplicates_handles_long_playlists(tmp_path, history):
    for n in range(0, 3000, 500):
        history.add(finished(tmp_path, video_id=f"v{n}"))
    found = history.find_duplicates("youtube", [f"v{n}" for n in range(3000)])
    assert sorted(found) == sorted(f"v{n}" for n in range(0, 3000, 500))
