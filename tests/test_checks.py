import asyncio
import sys
from pathlib import Path

import pytest

from snag.checks import DENO_MISSING, FFMPEG_MISSING, ToolCheck, ToolStatus, check_tools
from snag.paths import Tools
from snag.proc import Completed
from tests.fake_ytdlp import VERSION

FFMPEG_OUT = "ffmpeg version N-121000-g1234abcd-20260901 Copyright (c) 2000-2026 the FFmpeg developers\nbuilt with gcc\n"
FFPROBE_OUT = "ffprobe version N-121000-g1234abcd-20260901 Copyright (c) 2007-2026 the FFmpeg developers\n"
DENO_OUT = "deno 2.5.1 (stable, release, x86_64-pc-windows-msvc)\nv8 14.0\ntypescript 5.9\n"


def tools_in(folder: Path) -> Tools:
    return Tools(ytdlp=(str(folder / "yt-dlp.exe"),), ffmpeg_dir=folder, deno=folder / "deno.exe")


def fake_run(answers: dict[str, Completed | BaseException]):
    """A run function answering by the executable's file name; records what it ran."""
    calls: list[list[str]] = []

    async def run(args: list[str]) -> Completed:
        calls.append(args)
        answer = answers[Path(args[0]).name]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    run.calls = calls
    return run


ALL_GOOD = {
    "yt-dlp.exe": Completed(0, "2026.08.19\n", ""),
    "ffmpeg.exe": Completed(0, FFMPEG_OUT, ""),
    "ffprobe.exe": Completed(0, FFPROBE_OUT, ""),
    "deno.exe": Completed(0, DENO_OUT, ""),
}


async def test_every_tool_present_with_its_version(tmp_path):
    run = fake_run(ALL_GOOD)
    status = await check_tools(tools_in(tmp_path), run=run)
    assert status == ToolStatus(
        ytdlp=ToolCheck(True, "2026.08.19"),
        ffmpeg=ToolCheck(True, "N-121000-g1234abcd-20260901"),
        ffprobe=ToolCheck(True, "N-121000-g1234abcd-20260901"),
        deno=ToolCheck(True, "2.5.1"),
    )
    assert status.ready


async def test_each_tool_is_asked_for_its_version_the_way_it_understands(tmp_path):
    run = fake_run(ALL_GOOD)
    await check_tools(tools_in(tmp_path), run=run)
    assert sorted(run.calls) == sorted([
        [str(tmp_path / "yt-dlp.exe"), "--version"],
        [str(tmp_path / "ffmpeg.exe"), "-version"],
        [str(tmp_path / "ffprobe.exe"), "-version"],
        [str(tmp_path / "deno.exe"), "--version"],
    ])


@pytest.mark.parametrize("failure", [
    FileNotFoundError(2, "The system cannot find the file specified"),
    PermissionError(13, "Access is denied"),
    TimeoutError(),
    Completed(1, "", "error while loading shared libraries"),
    Completed(0, "", ""),  # ran but said nothing: not a working ffmpeg
])
async def test_missing_or_broken_ffmpeg_says_so_without_raising(tmp_path, failure):
    run = fake_run({**ALL_GOOD, "ffmpeg.exe": failure})
    status = await check_tools(tools_in(tmp_path), run=run)
    assert status.ffmpeg == ToolCheck(False, None, FFMPEG_MISSING)
    assert FFMPEG_MISSING == "ffmpeg is missing"
    assert status.ytdlp.present and status.deno.present
    assert not status.ready


async def test_missing_deno_has_the_formats_message_and_is_optional(tmp_path):
    run = fake_run({**ALL_GOOD, "deno.exe": FileNotFoundError(2, "missing")})
    status = await check_tools(tools_in(tmp_path), run=run)
    assert status.deno == ToolCheck(False, None, DENO_MISSING)
    assert DENO_MISSING == "Deno is missing, so some YouTube formats won't show"
    assert status.ready  # yt-dlp works without Deno, with fewer YouTube formats


async def test_missing_ytdlp_and_ffprobe_have_messages(tmp_path):
    run = fake_run({**ALL_GOOD, "yt-dlp.exe": FileNotFoundError(2, "x"), "ffprobe.exe": FileNotFoundError(2, "x")})
    status = await check_tools(tools_in(tmp_path), run=run)
    assert status.ytdlp == ToolCheck(False, None, "yt-dlp is missing")
    assert status.ffprobe == ToolCheck(False, None, "ffprobe is missing")
    assert not status.ready


async def test_an_unexpected_error_is_reported_as_missing(tmp_path):
    run = fake_run({**ALL_GOOD, "deno.exe": RuntimeError("something odd")})
    status = await check_tools(tools_in(tmp_path), run=run)
    assert status.deno.present is False and status.deno.message == DENO_MISSING


async def test_cancellation_is_not_swallowed(tmp_path):
    run = fake_run({**ALL_GOOD, "ffmpeg.exe": asyncio.CancelledError()})
    with pytest.raises(asyncio.CancelledError):
        await check_tools(tools_in(tmp_path), run=run)


async def test_an_unrecognised_version_line_is_kept_as_it_is(tmp_path):
    run = fake_run({**ALL_GOOD, "deno.exe": Completed(0, "something 1.0\n", "")})
    status = await check_tools(tools_in(tmp_path), run=run)
    assert status.deno == ToolCheck(True, "something 1.0")


async def test_real_run_with_no_tools_installed(tmp_path):
    status = await check_tools(tools_in(tmp_path / "empty-bin"))
    assert [check.present for check in (status.ytdlp, status.ffmpeg, status.ffprobe, status.deno)] == [False] * 4
    assert status.ffmpeg.message == FFMPEG_MISSING and status.deno.message == DENO_MISSING


async def test_real_run_of_the_fake_ytdlp(fake_tools):
    status = await check_tools(fake_tools)
    assert status.ytdlp == ToolCheck(True, VERSION)
    assert status.ffmpeg.present is False


async def test_real_run_with_a_working_program(tmp_path, monkeypatch):
    # python --version answers like deno --version does: one line with the version.
    tools = Tools(ytdlp=(sys.executable,), ffmpeg_dir=tmp_path, deno=Path(sys.executable))
    status = await check_tools(tools)
    assert status.ytdlp.present and status.ytdlp.version.startswith("Python 3.")
    assert status.deno.present
