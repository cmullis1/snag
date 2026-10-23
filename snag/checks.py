"""Startup checks: does each tool Snag runs (yt-dlp, ffmpeg, ffprobe, Deno) start and report a version?

Run before the first download needs the tools, so a missing one is reported up front (spec 5.2) rather
than as a failed download."""
from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from snag.paths import Tools
from snag.proc import Completed, run_capture

log = logging.getLogger(__name__)

Run = Callable[[list[str]], Awaitable[Completed]]

CHECK_TIMEOUT = 15.0  # seconds; a first run after boot can be slow while antivirus scans the file
YTDLP_MISSING = "yt-dlp is missing"
FFMPEG_MISSING = "ffmpeg is missing"
FFPROBE_MISSING = "ffprobe is missing"
DENO_MISSING = "Deno is missing, so some YouTube formats won't show"

_FFMPEG_VERSION = re.compile(r"\bversion (\S+)")
_DENO_VERSION = re.compile(r"^deno (\S+)")


@dataclass(frozen=True)
class ToolCheck:
    """present: the tool ran and reported a version. message: what to tell the user when it didn't."""

    present: bool
    version: str | None = None
    message: str | None = None


@dataclass(frozen=True)
class ToolStatus:
    ytdlp: ToolCheck
    ffmpeg: ToolCheck
    ffprobe: ToolCheck
    deno: ToolCheck

    @property
    def ready(self) -> bool:
        """Downloads can run: yt-dlp, ffmpeg and ffprobe work. Deno is optional (without it some YouTube
        formats are missing)."""
        return self.ytdlp.present and self.ffmpeg.present and self.ffprobe.present


async def _run_with_timeout(args: list[str]) -> Completed:
    return await run_capture(args, timeout=CHECK_TIMEOUT)


def _version(output: str, pattern: re.Pattern[str] | None) -> str | None:
    first = output.strip().splitlines()[0].strip() if output.strip() else ""
    if not first:
        return None
    match = pattern.search(first) if pattern else None
    return match.group(1) if match else first


async def _check(run: Run, args: list[str], pattern: re.Pattern[str] | None, missing: str) -> ToolCheck:
    try:
        done = await run(args)
    except Exception:  # not found, not runnable, timed out: all mean the tool can't be used
        log.info("%s: %s didn't run", missing, args[0], exc_info=True)
        return ToolCheck(False, None, missing)
    version = _version(done.stdout, pattern) if done.returncode == 0 else None
    if version is None:
        log.info("%s: %s exited with %d: %s", missing, args[0], done.returncode, done.stderr.strip()[:500])
        return ToolCheck(False, None, missing)
    return ToolCheck(True, version)


async def check_tools(tools: Tools, *, run: Run = _run_with_timeout) -> ToolStatus:
    """Run each tool's version command (at once) and report what works. Never raises, except to pass on
    a cancellation."""
    ffmpeg_dir = Path(tools.ffmpeg_dir)
    ytdlp, ffmpeg, ffprobe, deno = await asyncio.gather(
        _check(run, [*tools.ytdlp, "--version"], None, YTDLP_MISSING),
        _check(run, [str(ffmpeg_dir / "ffmpeg.exe"), "-version"], _FFMPEG_VERSION, FFMPEG_MISSING),
        _check(run, [str(ffmpeg_dir / "ffprobe.exe"), "-version"], _FFMPEG_VERSION, FFPROBE_MISSING),
        _check(run, [str(tools.deno), "--version"], _DENO_VERSION, DENO_MISSING),
    )
    return ToolStatus(ytdlp=ytdlp, ffmpeg=ffmpeg, ffprobe=ffprobe, deno=deno)
