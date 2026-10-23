import asyncio
import json
from pathlib import Path

from snag.jobs.command import is_subtitle_failure
from snag.proc import kill_tree, run_capture, spawn

TEMPLATE = "%(title).150s.%(ext)s"


async def run_in(fake_tools, cwd: Path, *args: str) -> tuple[int, str, str]:
    """Run the fake like the runner runs yt-dlp: inside the working folder, relative template."""
    proc = await spawn([*fake_tools.ytdlp, *args], cwd=cwd)
    out, err = await asyncio.wait_for(proc.communicate(), 30)
    return proc.returncode, out.decode("utf-8"), err.decode("utf-8")


def lines_starting(text: str, prefix: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith(prefix)]


async def test_fake_success_writes_into_its_current_folder(fake_tools, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    code, out, err = await run_in(fake_tools, work, "-o", TEMPLATE, "--", "fake://success")
    assert code == 0
    assert sorted(p.name for p in work.iterdir()) == ["Fake Video.mp4"]  # stream files merged away
    assert lines_starting(out, "SNAGFILE ") == [f"SNAGFILE {work / 'Fake Video.mp4'}"]
    assert "SNAGDL finished|4000|4000" in out
    assert "SNAGPP started|Merger" in err


async def test_fake_prints_the_chosen_format_before_downloading(fake_tools, tmp_path):
    _, out, _ = await run_in(fake_tools, tmp_path, "-o", TEMPLATE, "--", "fake://success")
    snag_lines = lines_starting(out, "SNAG")
    assert snag_lines[0] == "SNAGFMT v+a"
    assert snag_lines[1].startswith("SNAGDL ")


async def test_fake_honours_p_like_yt_dlp(fake_tools, tmp_path):
    done = await run_capture([*fake_tools.ytdlp, "-P", str(tmp_path), "-o", TEMPLATE, "--", "fake://success"])
    assert done.returncode == 0
    assert (tmp_path / "Fake Video.mp4").exists()


async def test_fake_success_with_an_absolute_template(fake_tools, tmp_path):
    done = await run_capture([*fake_tools.ytdlp, "-o", str(tmp_path / "%(title)s.%(ext)s"), "--", "fake://success"])
    assert done.returncode == 0
    assert (tmp_path / "Fake Video.mp4").exists()


async def test_fake_applies_title_limits(fake_tools, tmp_path):
    await run_in(fake_tools, tmp_path, "-o", "%(title).4s.%(ext)s", "--", "fake://success")
    await run_in(fake_tools, tmp_path, "-o", "%(title).4B.%(ext)s", "--", "fake://unicode")
    assert sorted(p.name for p in tmp_path.iterdir() if p.is_file()) == ["Caf.mp4", "Fake.mp4"]


async def test_fake_single_downloads_one_combined_stream(fake_tools, tmp_path):
    code, out, err = await run_in(fake_tools, tmp_path, "-o", TEMPLATE, "--", "fake://single")
    assert code == 0
    assert lines_starting(out, "SNAGFMT ") == ["SNAGFMT combined"]
    assert {line.rsplit("|", 1)[1] for line in lines_starting(out, "SNAGDL ")} == {"combined"}
    assert "Merger" not in err
    assert (tmp_path / "Fake Video.mp4").exists()


async def test_fake_subtitle_failure_uses_yt_dlps_message(fake_tools, tmp_path):
    code, out, err = await run_in(
        fake_tools, tmp_path, "-o", TEMPLATE, "--write-subs", "--sub-langs", "en", "--", "fake://subs-fail",
    )
    assert code == 1
    assert "ERROR: Unable to download video subtitles for 'en': HTTP Error 429: Too Many Requests" in err
    assert is_subtitle_failure(out + err)
    assert not lines_starting(out, "SNAGFMT ")  # yt-dlp fetches subtitles before the video
    assert list(tmp_path.iterdir()) == [tmp_path / "fake-state"]


async def test_fake_subtitle_failure_scenario_succeeds_without_subtitles(fake_tools, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    code, _, _ = await run_in(fake_tools, work, "-o", TEMPLATE, "--", "fake://subs-fail")
    assert code == 0
    assert (work / "Fake Video.mp4").exists()


async def test_fake_no_file_exits_cleanly_without_a_file(fake_tools, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    code, out, _ = await run_in(fake_tools, work, "-o", TEMPLATE, "--", "fake://no-file")
    assert code == 0
    assert "SNAGDL finished" in out and not lines_starting(out, "SNAGFILE ")
    assert list(work.iterdir()) == []


async def test_fake_subtitle_file_follows_the_language(fake_tools, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    await run_in(fake_tools, work, "-o", TEMPLATE, "--sub-langs", "de", "--convert-subs", "srt", "--", "fake://success")
    assert sorted(p.name for p in work.iterdir()) == ["Fake Video.de.srt", "Fake Video.mp4"]


async def test_fake_info_json(fake_tools):
    done = await run_capture([*fake_tools.ytdlp, "-J", "--", "fake://video"])
    assert json.loads(done.stdout)["id"] == "abc123"


async def test_fake_failure(fake_tools):
    done = await run_capture([*fake_tools.ytdlp, "--", "fake://unavailable"])
    assert done.returncode == 1
    assert "Video unavailable" in done.stderr


async def test_fake_throttle_once_keeps_state_per_test(fake_tools, tmp_path):
    args = [*fake_tools.ytdlp, "-o", str(tmp_path / "%(title)s.%(ext)s"), "--", "fake://throttle-once/tok"]
    first = await run_capture(args)
    assert first.returncode == 1
    assert "HTTP Error 429" in first.stderr
    assert (tmp_path / "fake-state" / "snag-fake-tok").exists()
    second = await run_capture(args)
    assert second.returncode == 0


async def test_fake_forbidden_once_answers_403_then_downloads(fake_tools, tmp_path):
    args = [*fake_tools.ytdlp, "-o", str(tmp_path / "%(title)s.%(ext)s"), "--", "fake://forbidden-once/tok"]
    first = await run_capture(args)
    assert first.returncode == 1 and "ERROR: unable to download video data: HTTP Error 403: Forbidden" in first.stderr
    second = await run_capture(args)
    assert second.returncode == 0


async def test_fake_download_without_output_template_fails_clearly(fake_tools):
    done = await run_capture([*fake_tools.ytdlp, "--", "fake://success"])
    assert done.returncode == 1
    assert "ERROR: fake yt-dlp: missing -o" in done.stderr


async def test_fake_ends_with_movefiles_like_yt_dlp(fake_tools, tmp_path):
    for scenario in ("success", "single"):
        work = tmp_path / scenario
        work.mkdir()
        _, _, err = await run_in(fake_tools, work, "-o", TEMPLATE, "--", f"fake://{scenario}")
        assert lines_starting(err, "SNAGPP ")[-2:] == ["SNAGPP started|MoveFiles", "SNAGPP finished|MoveFiles"]


async def test_fake_downloads_subtitles_before_the_video(fake_tools, tmp_path):
    _, out, _ = await run_in(fake_tools, tmp_path, "-o", TEMPLATE, "--write-subs", "--sub-langs", "en", "--", "fake://single")
    snag_lines = lines_starting(out, "SNAG")
    assert snag_lines[0].startswith("SNAGDL downloading|") and snag_lines[0].endswith("|NA")
    assert snag_lines[1].startswith("SNAGDL finished|") and snag_lines[1].endswith("|NA")
    assert snag_lines[2] == "SNAGFMT combined"


async def test_fake_single_stream_partial_has_no_format_id(fake_tools, tmp_path):
    proc = await spawn([*fake_tools.ytdlp, "-o", TEMPLATE, "--", "fake://slow-single"], cwd=tmp_path)
    try:
        while not (line := await asyncio.wait_for(proc.stdout.readline(), 30)).startswith(b"SNAGDL downloading"):
            assert line, "the fake ended before downloading"
        names = [p.name for p in tmp_path.iterdir() if p.is_file()]
        assert names == ["Fake Video.mp4.part"]
    finally:
        await kill_tree(proc)


async def test_fake_prints_no_chatter_under_print(fake_tools, tmp_path):
    # --print makes yt-dlp quiet: only errors, warnings and the templates are printed.
    _, out, err = await run_in(
        fake_tools, tmp_path, "-o", TEMPLATE, "--write-subs", "--sub-langs", "en", "--", "fake://subs-fail",
    )
    assert out == ""
    assert all(line.startswith(("ERROR:", "WARNING:")) for line in err.splitlines())


async def test_fake_resume_complete_reports_finished_without_bytes(fake_tools, tmp_path):
    code, out, _ = await run_in(fake_tools, tmp_path, "-o", TEMPLATE, "--", "fake://resume-complete")
    assert code == 0
    assert lines_starting(out, "SNAG")[:2] == ["SNAGFMT combined", "SNAGDL finished|NA|5000|NA|NA|NA|combined"]
    assert (tmp_path / "Fake Video.mp4").stat().st_size == 5000
    assert lines_starting(out, "SNAGFILE ") == [f"SNAGFILE {tmp_path / 'Fake Video.mp4'}"]
