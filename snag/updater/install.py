"""Download, verify, smoke-test and swap in one tool from its latest GitHub release (spec 4.8).

Every install goes through the same steps, all inside bin_dir:
  1. download <asset>.part, hashing it as it arrives, and compare with the release's checksum file
     (and with the API's digest of the asset, when it gives one). Downloads only come from
     https://github.com/<repo>/releases/download/, and only over https, redirects included;
  2. extract the executables (from a zip) or rename the download, to <name>.new.exe;
  3. run each <name>.new.exe once (the smoke check);
  4. swap: <name>.exe -> <name>.old.exe, <name>.new.exe -> <name>.exe, delete <name>.old.exe.
A failure before the swap leaves the current tools untouched and removes everything it created. A
failure during the swap puts every file of the tool back. prepare() stops after step 3, so an update
can wait for running downloads before the swap (Windows can't replace a running .exe in place).

Antivirus scanners and the search indexer open new executables for a moment, so every move in the
swap (and in its rollback) is retried for about 3 s when Windows says the file is in use. The swap
runs in a worker thread, so those waits never stall the event loop.

clean_leftovers() repairs what a crash in the middle of this leaves behind; run it at startup, before
any install starts."""
from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import hashlib
import os
import time
import zipfile
import zlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from snag.proc import run_capture
from snag.updater.releases import USER_AGENT, Release, UpdaterError, latest_release, parse_checksums

Progress = Callable[[int, "int | None"], None]  # (bytes done, total bytes or None when unknown)
Phase = Callable[[str], None]  # "downloading", "verifying", "installing"
Smoke = Callable[[Path], Awaitable[bool]]

DOWNLOAD_TIMEOUT = httpx.Timeout(30.0)  # per network operation; a 200 MB download takes as long as it takes
SMOKE_TIMEOUT = 60.0  # yt-dlp.exe unpacks itself on first run, and antivirus scans a new .exe
_CHUNK = 256 * 1024
# Waits between attempts to move a file Windows reports as in use; about 3 s in all.
SWAP_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4, 0.75, 1.5)
# ERROR_ACCESS_DENIED and ERROR_SHARING_VIOLATION: what a scanner holding the file open causes.
_IN_USE = frozenset({5, 32})


class InstallError(UpdaterError):
    """A tool couldn't be installed; the current one, if any, is still in place."""


@dataclass(frozen=True)
class ToolSource:
    tool: str  # the name Snag shows
    repo: str
    asset: str
    checksums: str  # the checksum file published with the asset
    # (executable in bin_dir, pattern matching it inside the zip); empty: the asset is the executable
    members: tuple[tuple[str, str], ...] = ()


YTDLP = ToolSource("yt-dlp", "yt-dlp/yt-dlp", "yt-dlp.exe", "SHA2-256SUMS")
FFMPEG = ToolSource(
    "ffmpeg", "yt-dlp/FFmpeg-Builds", "ffmpeg-master-latest-win64-gpl.zip", "checksums.sha256",
    (("ffmpeg.exe", "*/bin/ffmpeg.exe"), ("ffprobe.exe", "*/bin/ffprobe.exe")),
)
DENO = ToolSource(
    "Deno", "denoland/deno", "deno-x86_64-pc-windows-msvc.zip", "deno-x86_64-pc-windows-msvc.zip.sha256sum",
    (("deno.exe", "deno.exe"),),
)


def executables(source: ToolSource) -> tuple[str, ...]:
    """The files in bin_dir that source installs."""
    return tuple(name for name, _ in source.members) or (source.asset,)


def new_path(current: Path) -> Path:
    return current.with_name(f"{current.stem}.new{current.suffix}")


def old_path(current: Path) -> Path:
    return current.with_name(f"{current.stem}.old{current.suffix}")


def version_args(exe: Path) -> list[str]:
    """ffmpeg and ffprobe take -version; yt-dlp and Deno take --version."""
    return ["-version"] if exe.name.lower().startswith(("ffmpeg", "ffprobe")) else ["--version"]


async def default_smoke(exe: Path) -> bool:
    """True when exe runs and exits 0 when asked for its version."""
    try:
        result = await run_capture([str(exe), *version_args(exe)], timeout=SMOKE_TIMEOUT)
    except (OSError, TimeoutError):
        return False
    return result.returncode == 0


@dataclass
class Staged:
    """A verified, smoke-tested tool waiting in bin_dir to be swapped in."""

    source: ToolSource
    tag: str
    files: dict[Path, Path] = field(default_factory=dict)  # installed path -> staged <name>.new.exe
    swapped: bool = False  # the new files are in place

    async def swap(self) -> None:
        """Put the staged files in place, all or none, in a worker thread. Raises InstallError (with
        every file back as it was). If cancelled, the swap still finishes (or rolls back) first; when
        it finished, swapped is True before the CancelledError reaches the caller, so the caller can
        still record the new version."""

        def run() -> None:
            swap(self.files)
            self.swapped = True  # read only after the thread has ended (_in_thread waits for it)

        try:
            await _in_thread(run)
        except OSError as exc:
            raise InstallError(f"Couldn't replace {self.source.tool}: {_reason(exc)}") from exc
        finally:
            if self.swapped:
                self.files = {}

    def discard(self) -> None:
        """Delete the staged files (nothing to do once swapped)."""
        for staged in self.files.values():
            with contextlib.suppress(OSError):
                staged.unlink(missing_ok=True)
        self.files = {}


async def prepare(
    client: httpx.AsyncClient,
    source: ToolSource,
    bin_dir: Path,
    *,
    release: Release | None = None,
    progress: Progress | None = None,
    phase: Phase | None = None,
    smoke: Smoke = default_smoke,
) -> Staged:
    """Steps 1-3: the tool, verified and smoke-tested, staged next to the current one (which is left
    alone). Raises UpdaterError (ReleaseError or InstallError) with a plain message; on any failure,
    cancellation included, nothing it created is left behind."""
    if release is None:
        release = await latest_release(client, source.repo)
    asset_url = _asset_url(release, source, source.asset)
    sums_url = _asset_url(release, source, source.checksums)
    bin_dir.mkdir(parents=True, exist_ok=True)
    _phase(phase, "downloading")
    sums = parse_checksums(await _fetch_text(client, source, sums_url), single_file=source.asset)
    expected = sums.get(source.asset)
    if expected is None:
        raise InstallError(f"The latest {source.tool} release doesn't list a checksum for {source.asset}.")
    if release.digests.get(source.asset, expected) != expected:  # the API and the checksum file disagree
        raise _mismatch(source)
    staged = Staged(source, release.tag)
    part = bin_dir / f"{source.asset}.part"
    try:
        actual = await _download(client, source, asset_url, part, progress)
        _phase(phase, "verifying")
        if actual != expected:
            raise _mismatch(source)
        _phase(phase, "installing")
        staged.files = {bin_dir / name: new_path(bin_dir / name) for name in executables(source)}
        if source.members:
            await _in_thread(_extract, source, part, staged.files)
        else:
            os.replace(part, new_path(bin_dir / source.asset))
        for exe in staged.files.values():
            if not await smoke(exe):
                raise InstallError(f"The new {source.tool} didn't run, so it wasn't installed.")
    except BaseException as exc:
        staged.discard()
        with contextlib.suppress(OSError):
            part.unlink(missing_ok=True)
        if isinstance(exc, OSError):
            raise InstallError(f"Couldn't save {source.tool}: {_reason(exc)}") from exc
        raise
    with contextlib.suppress(OSError):
        part.unlink(missing_ok=True)
    return staged


async def install(client: httpx.AsyncClient, source: ToolSource, bin_dir: Path, **options) -> str:
    """All four steps; returns the release tag installed. Options as for prepare()."""
    staged = await prepare(client, source, bin_dir, **options)
    try:
        await staged.swap()
    finally:
        staged.discard()
    return staged.tag


def swap(files: dict[Path, Path]) -> None:
    """Move each staged file (value) onto its installed path (key), keeping the current one as
    <name>.old.exe until all are in place. On an OSError every file is put back as it was before the
    error propagates; the .old files are deleted at the end (a leftover is cleaned at the next start).
    Moves of a file Windows reports as in use are retried (see SWAP_RETRY_DELAYS); this blocks, so
    call it from a worker thread."""
    kept: dict[Path, Path] = {}  # installed path -> its previous version, moved aside
    placed: list[Path] = []
    try:
        for current in files:
            if current.exists():
                old = old_path(current)
                old.unlink(missing_ok=True)
                _move(current, old)
                kept[current] = old
        for current, staged in files.items():
            _move(staged, current)
            placed.append(current)
    except OSError:
        for current, old in kept.items():
            with contextlib.suppress(OSError):
                _move(old, current)
        for current in placed:
            if current not in kept:
                with contextlib.suppress(OSError):
                    current.unlink()
        raise
    for old in kept.values():
        with contextlib.suppress(OSError):
            old.unlink()


def clean_leftovers(bin_dir: Path) -> None:
    """Undo what an install interrupted by a crash left: a <name>.old.exe whose <name>.exe is missing
    goes back in place (the crash came mid-swap); other .old.exe, .new.exe and .part files are
    deleted. Never run it while an install is under way.

    Each file is repaired on its own, so a crash halfway through swapping ffmpeg's pair (ffmpeg.exe
    already new, ffprobe.exe still only ffprobe.old.exe) leaves the new ffmpeg with the old ffprobe.
    That is deliberate: both are standalone builds that work apart, putting the old ffmpeg back would
    need the pair's membership here, and the next update or reinstall makes them a pair again. A
    .old.exe that couldn't be deleted after a swap (the old program was still running) is deleted here."""
    if not bin_dir.is_dir():
        return
    for leftover in bin_dir.iterdir():
        name = leftover.name.lower()
        with contextlib.suppress(OSError):
            if name.endswith(".old.exe"):
                current = leftover.with_name(leftover.name[: -len(".old.exe")] + ".exe")
                if current.exists():
                    leftover.unlink()
                else:
                    os.replace(leftover, current)
            elif name.endswith((".new.exe", ".part")):
                leftover.unlink()


# ----- helpers -----

def _move(source: Path, target: Path) -> None:
    """os.replace, retried while Windows says a file is in use (blocks for up to ~3 s)."""
    for delay in (*SWAP_RETRY_DELAYS, None):
        try:
            os.replace(source, target)
            return
        except PermissionError as exc:
            if delay is None or getattr(exc, "winerror", None) not in _IN_USE:
                raise
        time.sleep(delay)


def _phase(callback: Phase | None, name: str) -> None:
    if callback is not None:
        callback(name)


def _asset_url(release: Release, source: ToolSource, name: str) -> str:
    """The asset's download URL, which must be one of source.repo's release downloads on github.com."""
    url = release.assets.get(name)
    if url is None:
        raise InstallError(f"The latest {source.tool} release doesn't include {name}.")
    try:
        parsed = httpx.URL(url)
    except (httpx.InvalidURL, TypeError, ValueError):
        parsed = None
    prefix = f"/{source.repo}/releases/download/".lower()
    if (
        parsed is None
        or parsed.scheme != "https"
        or parsed.host != "github.com"
        or parsed.port is not None
        or parsed.userinfo
        or not parsed.path.lower().startswith(prefix)
        or ".." in parsed.path.split("/")
    ):
        raise InstallError(
            f"The latest {source.tool} release points somewhere other than its GitHub releases, so Snag "
            "didn't download it."
        )
    return url


def _require_https(response: httpx.Response, source: ToolSource) -> None:
    """Refuse a download that went over plain http at any hop: the redirects that led to it
    (response.history) as well as the final answer. A hop in the clear could have been redirected
    anywhere, so ending on https proves nothing."""
    if any(hop.url.scheme != "https" for hop in (*response.history, response)):
        raise InstallError(f"The {source.tool} download wasn't sent securely, so Snag didn't use it.")


def _mismatch(source: ToolSource) -> InstallError:
    return InstallError(f"The {source.tool} download didn't match its published checksum, so it wasn't installed.")


def _reason(exc: OSError) -> str:
    return exc.strerror or str(exc)


def _interrupted(source: ToolSource) -> InstallError:
    return InstallError(
        f"The {source.tool} download was interrupted. Check your internet connection and try again."
    )


async def _fetch_text(client: httpx.AsyncClient, source: ToolSource, url: str) -> str:
    try:
        response = await client.get(url, headers={"User-Agent": USER_AGENT}, timeout=DOWNLOAD_TIMEOUT,
                                    follow_redirects=True)
    except httpx.HTTPError as exc:
        raise _interrupted(source) from exc
    _require_https(response, source)
    if response.status_code != 200:
        raise InstallError(f"Couldn't download {source.tool} (error {response.status_code}). Try again later.")
    return response.content.decode("utf-8", "replace")


async def _download(
    client: httpx.AsyncClient, source: ToolSource, url: str, target: Path, progress: Progress | None
) -> str:
    """Stream url into target; returns its SHA-256. The caller deletes target on failure."""
    digest = hashlib.sha256()
    try:
        async with client.stream("GET", url, headers={"User-Agent": USER_AGENT}, timeout=DOWNLOAD_TIMEOUT,
                                 follow_redirects=True) as response:
            _require_https(response, source)
            if response.status_code != 200:
                raise InstallError(
                    f"Couldn't download {source.tool} (error {response.status_code}). Try again later."
                )
            total = _content_length(response)  # bytes on the wire, as num_bytes_downloaded counts them
            with open(target, "wb") as out:
                async for chunk in response.aiter_bytes():
                    out.write(chunk)
                    digest.update(chunk)
                    if progress is not None:
                        progress(response.num_bytes_downloaded, total)
                received = response.num_bytes_downloaded
    except httpx.HTTPError as exc:
        raise _interrupted(source) from exc
    if total is not None and received != total:
        raise _interrupted(source)  # the server closed the connection early
    return digest.hexdigest()


def _content_length(response: httpx.Response) -> int | None:
    try:
        return int(response.headers["content-length"])
    except (KeyError, ValueError):
        return None


# What zipfile raises for an archive it can't read: not a zip or a bad CRC (BadZipFile), a damaged
# compressed stream (zlib.error, EOFError), a compression method it doesn't support
# (NotImplementedError), an encrypted member (RuntimeError), a zip64 it won't open (LargeZipFile),
# and malformed headers (ValueError). OSError is left alone: it is about saving the files.
_UNREADABLE_ZIP = (zipfile.BadZipFile, zipfile.LargeZipFile, zlib.error, EOFError, NotImplementedError,
                   RuntimeError, ValueError)


def _extract(source: ToolSource, archive: Path, files: dict[Path, Path]) -> None:
    """Copy each wanted member of the zip to its staged path (runs in a worker thread)."""
    patterns = dict(source.members)
    try:
        with zipfile.ZipFile(archive) as zf:
            members = [info for info in zf.infolist() if not info.is_dir()]
            for current, staged in files.items():
                matches = [m for m in members if fnmatch.fnmatchcase(m.filename, patterns[current.name])]
                if len(matches) != 1:
                    raise InstallError(f"The {source.tool} download doesn't contain {current.name}.")
                with zf.open(matches[0]) as src, open(staged, "wb") as dst:
                    while block := src.read(_CHUNK):
                        dst.write(block)
    except _UNREADABLE_ZIP as exc:
        raise InstallError(f"The {source.tool} download couldn't be unpacked, so it wasn't installed.") from exc


async def _in_thread(func, *args) -> None:
    """Run func in a worker thread. If cancelled, wait for the thread to finish first, so the caller's
    cleanup never races with files the thread still has open."""
    task = asyncio.ensure_future(asyncio.to_thread(func, *args))
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        with contextlib.suppress(BaseException):
            await asyncio.wait((task,))
        raise
