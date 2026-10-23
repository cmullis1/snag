from snag.jobs.progress import (
    DOWNLOAD_TEMPLATE, FORMAT_PRINT, DownloadEvent, FileEvent, FormatEvent, PostprocessEvent, ProgressTracker, parse_line,
)


def test_templates_use_the_markers():
    assert DOWNLOAD_TEMPLATE.startswith("download:SNAGDL ")
    assert FORMAT_PRINT == "before_dl:SNAGFMT %(format_id)s"


def test_parse_format_line():
    assert parse_line("SNAGFMT 299+140") == FormatEvent("299+140")
    assert parse_line("SNAGFMT 18") == FormatEvent("18")


def test_out_of_range_numbers_become_unknown():
    event = parse_line("SNAGDL downloading|1e999|1000|NA|inf|nan|v")
    assert event == DownloadEvent("downloading", None, 1000, None, None, None, "v")


def test_parse_download_line():
    event = parse_line("SNAGDL downloading|3072|4323893|NA|1536111.33|2|160")
    assert event == DownloadEvent("downloading", 3072, 4323893, None, 1536111.33, 2, "160")


def test_parse_download_line_with_missing_values():
    event = parse_line("SNAGDL downloading|1024|NA|5000.0|NA|NA|160")
    assert event == DownloadEvent("downloading", 1024, None, 5000, None, None, "160")


def test_parse_postprocess_and_file_lines():
    assert parse_line("SNAGPP started|Merger") == PostprocessEvent("started", "Merger")
    assert parse_line("SNAGPP started|Merger").label == "Joining video and audio…"
    assert parse_line("SNAGPP started|FixupM4a").label == "Processing…"
    assert parse_line("SNAGPP started|ExtractAudio").label == "Converting audio…"
    assert parse_line("SNAGFILE C:\\x\\A.mp4") == FileEvent("C:\\x\\A.mp4")


def test_other_lines_are_ignored():
    assert parse_line("[youtube] abc: Downloading webpage") is None
    assert parse_line("SNAGDL broken") is None


def test_tracker_single_stream():
    tracker = ProgressTracker(expected_streams=1)
    tracker.update(DownloadEvent("downloading", 250, 1000, None, 10.0, 7, "v"))
    assert tracker.percent == 25.0
    assert tracker.speed == 10.0 and tracker.eta == 7


def test_tracker_two_streams_moves_forward_only():
    tracker = ProgressTracker(expected_streams=2)
    tracker.update(DownloadEvent("finished", 4000, 4000, None, None, None, "v"))
    assert tracker.percent == 50.0  # only one of two expected streams seen
    tracker.update(DownloadEvent("downloading", 0, 1000, None, None, None, "a"))
    assert tracker.percent == 80.0  # now weighted by bytes: 4000 of 5000
    tracker.update(DownloadEvent("finished", 1000, 1000, None, None, None, "a"))
    assert tracker.percent == 100.0


def test_tracker_never_goes_backwards():
    tracker = ProgressTracker(expected_streams=1)
    tracker.update(DownloadEvent("downloading", 600, 1000, None, None, None, "v"))
    tracker.update(DownloadEvent("downloading", 100, 1000, None, None, None, "v"))
    assert tracker.percent == 60.0


def test_tracker_uses_estimate_when_total_unknown():
    tracker = ProgressTracker(expected_streams=1)
    tracker.update(DownloadEvent("downloading", 500, None, 1000, None, None, "v"))
    assert tracker.percent == 50.0


def test_tracker_unknown_total():
    tracker = ProgressTracker(expected_streams=1)
    tracker.update(DownloadEvent("downloading", 500, None, None, None, None, "v"))
    assert tracker.percent is None


def test_quick_falling_back_to_one_combined_format_reaches_100():
    tracker = ProgressTracker(expected_streams=2)  # QUICK usually merges video and audio
    tracker.set_streams("18")  # but this video only offered a combined format
    tracker.update(DownloadEvent("downloading", 500, 1000, None, None, None, "18"))
    assert tracker.percent == 50.0
    tracker.update(DownloadEvent("finished", 1000, 1000, None, None, None, "18"))
    assert tracker.percent == 100.0


def test_trim_downloads_both_parts_in_one_stream():
    tracker = ProgressTracker(expected_streams=2)
    tracker.set_streams("299+140")
    tracker.update(DownloadEvent("downloading", 300, 1000, None, None, None, "299+140"))
    assert tracker.percent == 30.0
    tracker.update(DownloadEvent("finished", 1000, 1000, None, None, None, "299+140"))
    assert tracker.percent == 100.0


def test_video_plus_audio_after_format_line():
    tracker = ProgressTracker()
    tracker.set_streams("299+140")
    tracker.update(DownloadEvent("finished", 4000, 4000, None, None, None, "299"))
    assert tracker.percent == 50.0
    tracker.update(DownloadEvent("finished", 1000, 1000, None, None, None, "140"))
    assert tracker.percent == 100.0


def test_unknown_format_line_keeps_the_expected_count():
    tracker = ProgressTracker(expected_streams=2)
    tracker.set_streams("NA")
    tracker.update(DownloadEvent("finished", 1000, 1000, None, None, None, "v"))
    assert tracker.percent == 50.0


def test_late_format_line_lifts_progress_but_never_lowers_it():
    tracker = ProgressTracker(expected_streams=2)
    tracker.update(DownloadEvent("finished", 1000, 1000, None, None, None, "18"))
    assert tracker.percent == 50.0
    tracker.set_streams("18")
    assert tracker.percent == 100.0
    tracker.set_streams("299+140+251")
    assert tracker.percent == 100.0


def test_downloads_without_a_format_id_are_ignored():
    # With --write-subs yt-dlp downloads the subtitle file first; its events carry no format id.
    tracker = ProgressTracker(expected_streams=1)
    tracker.update(parse_line("SNAGDL downloading|38|38|NA|NA|NA|NA"))
    tracker.update(parse_line("SNAGDL finished|38|38|NA|9000.0|NA|NA"))
    assert tracker.percent is None
    assert tracker.speed is None
    tracker.set_streams("18")
    tracker.update(DownloadEvent("downloading", 100, 1000, None, 5.0, 3, "18"))
    assert tracker.percent == 10.0
    assert tracker.speed == 5.0


def test_already_downloaded_file_counts_as_complete():
    # yt-dlp reports a file that's already complete as finished with no byte count.
    tracker = ProgressTracker(expected_streams=1)
    tracker.set_streams("18")
    tracker.update(parse_line("SNAGDL finished|NA|200000|NA|NA|NA|18"))
    assert tracker.percent == 100.0


def test_finished_without_any_size_stays_unknown():
    tracker = ProgressTracker(expected_streams=1)
    tracker.update(DownloadEvent("finished", None, None, None, None, None, "18"))
    assert tracker.percent is None
