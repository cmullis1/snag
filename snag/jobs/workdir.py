"""Per-job working folders and the final move into the destination.

A job downloads into <local_work>\\<job id>\\ when the destination is on the same drive as Snag's own
work folder (so partial files never land in a possibly OneDrive-synced folder), otherwise into a hidden
<destination>\\.snag-tmp\\<job id>\\. Either way the final move is normally an instant same-drive rename.
"""
from __future__ import annotations

import ctypes
import errno
import os
import shutil
import sys
import time
from collections.abc import Iterable
from pathlib import Path

from .models import is_job_id, new_id

TMP_FOLDER = ".snag-tmp"
MAX_NAME_UNITS = 255  # NTFS limit per name, counted in UTF-16 code units
MAX_PATH_UNITS = 259  # MAX_PATH minus the terminating NUL; longer paths break Explorer and many players
MIN_STEM_UNITS = 16   # in very deep folders keep at least this much of the title rather than the path limit

_NOT_MEDIA = {
    ".part", ".ytdl", ".temp", ".srt", ".vtt", ".ass", ".lrc", ".jpg", ".jpeg", ".webp", ".png", ".txt",
    ".json", ".description",
}
_PARTIAL_SUFFIXES = {"part", "ytdl", "temp"}
_FILE_ATTRIBUTE_HIDDEN = 0x02
_ERROR_NOT_SAME_DEVICE = 17
_REMOVE_ATTEMPTS = 5
_REMOVE_DELAY = 0.2
PROCESS_START = time.time()  # cookies files modified before this belong to an earlier run


# --- where the working folder goes ---

def _drive(path: Path) -> str:
    return os.path.splitdrive(path.resolve())[0].casefold()


def work_root_for(dest_dir: Path, local_work: Path) -> Path:
    """local_work when it shares a drive with dest_dir, else dest_dir\\.snag-tmp."""
    return local_work if _drive(dest_dir) == _drive(local_work) else dest_dir / TMP_FOLDER


def work_dir_path(dest_dir: Path, job_id: str, local_work: Path) -> Path:
    return work_root_for(dest_dir, local_work) / job_id


def _make_dir(folder: Path) -> Path:
    """Create folder; a .snag-tmp holding it is hidden."""
    folder.mkdir(parents=True, exist_ok=True)
    if folder.parent.name == TMP_FOLDER and sys.platform == "win32":
        ctypes.windll.kernel32.SetFileAttributesW(str(folder.parent), _FILE_ATTRIBUTE_HIDDEN)
    return folder


def make_work_dir(dest_dir: Path, job_id: str, local_work: Path) -> Path:
    return _make_dir(work_dir_path(dest_dir, job_id, local_work))


def cookies_file_path(cookies_dir: Path, job_id: str) -> Path:
    return cookies_dir / f"{job_id}.txt"


def _is_leftover_cookies(path: Path) -> bool:
    """A job's cookies file, or a lookup's that an earlier run left (a running lookup's is newer than this process)."""
    if not path.is_file() or path.suffix != ".txt":
        return False
    if is_job_id(path.stem):
        return True
    if not path.stem.startswith("lookup-"):
        return False
    try:
        return path.stat().st_mtime < PROCESS_START
    except OSError:
        return False


def clear_cookies_dir(cookies_dir: Path) -> bool:
    """Delete the cookies files left by earlier runs; True when every one is gone.

    Job cookies files (<job id>.txt) always go: at startup no attempt is using one. A lookup's
    lookup-<id>.txt goes only when older than this process, so a lookup already running keeps its file.
    Other files are not Snag's to delete.
    """
    if not cookies_dir.is_dir():
        return True
    cleared = True
    for path in cookies_dir.iterdir():
        if not _is_leftover_cookies(path):
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError:
            cleared = False
    return cleared


# --- removing working folders ---

def remove_work_dir(work: Path) -> bool:
    """Delete a job's working folder; True when it is fully gone.

    Retries briefly because a just-killed ffmpeg or an antivirus scan can hold a file for a moment.
    An emptied .snag-tmp is removed too; Snag's own local work folder never is.
    """
    for attempt in range(_REMOVE_ATTEMPTS):
        try:
            shutil.rmtree(work)
        except FileNotFoundError:
            pass
        except OSError:
            if attempt < _REMOVE_ATTEMPTS - 1:
                time.sleep(_REMOVE_DELAY)
            continue
        break
    gone = not work.exists()
    if work.parent.name == TMP_FOLDER:
        try:
            work.parent.rmdir()  # only succeeds when no other job is using it
        except OSError:
            pass
    return gone


def sweep_orphans(roots: Iterable[Path], keep_ids: set[str]) -> list[Path]:
    """Delete working folders under each root (local work folder, each .snag-tmp) whose job is gone.

    Only folders named like a job id are considered, so a wrongly passed root can't lose user files.
    Returns the folders that were fully removed.
    """
    removed: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            if child.is_dir() and is_job_id(child.name) and child.name not in keep_ids and remove_work_dir(child):
                removed.append(child)
    return removed


# --- finding the downloaded file ---

def _is_partial(name: str) -> bool:
    """True for files yt-dlp is still writing: X.part, X.part-Frag3, X.ytdl, X.temp and X.temp.mp4.

    Only the end of the name decides, so a finished "Ep.Part.One.mp4" isn't mistaken for one.
    """
    parts = name.lower().split(".")
    if len(parts) < 2:
        return False
    last = parts[-1]
    if last in _PARTIAL_SUFFIXES or last.startswith("part-frag"):
        return True
    return len(parts) >= 3 and parts[-2] == "temp"


def find_main_file(work: Path, hint: str | None) -> Path | None:
    """yt-dlp reports the final path; it's trusted only inside the work folder, else the folder is scanned.

    A relative hint is relative to the work folder, which is yt-dlp's current folder.
    """
    if hint:
        candidate = work / hint
        if candidate.is_file() and candidate.resolve().is_relative_to(work.resolve()):
            return candidate
    if not work.is_dir():
        return None
    candidates = [
        p for p in work.iterdir()
        if p.is_file() and p.suffix.lower() not in _NOT_MEDIA and not _is_partial(p.name)
    ]
    return max(candidates, key=lambda p: p.stat().st_size, default=None)


# --- moving into place ---

def utf16_units(text: str) -> int:
    """Length as Windows counts it in names and paths: UTF-16 code units (an emoji is two)."""
    return len(text.encode("utf-16-le")) // 2


def _fit_stem(stem: str, prefix: str | None, marker: str, suffix_units: int, dest_dir: Path) -> str:
    """Shorten the title so "<prefix> - <title><marker><suffix>" fits a name and, if possible, MAX_PATH."""
    lead = f"{prefix} - " if prefix else ""
    fixed = utf16_units(lead) + utf16_units(marker) + suffix_units
    name_budget = MAX_NAME_UNITS - fixed
    path_budget = MAX_PATH_UNITS - utf16_units(os.path.abspath(dest_dir)) - 1 - fixed
    budget = min(name_budget, max(path_budget, MIN_STEM_UNITS))
    if utf16_units(stem) <= budget:
        return stem
    kept: list[str] = []
    used = 0
    for char in stem:
        size = utf16_units(char)
        if used + size > budget:
            break
        kept.append(char)
        used += size
    return "".join(kept).rstrip(" .") or "".join(kept)


def _extras(work: Path, main: Path) -> list[tuple[Path, str]]:
    """Sibling files belonging to main (e.g. A.en.srt for A.mp4), as (path, rest after the stem)."""
    extras = []
    for path in sorted(work.iterdir()):
        if path == main or not path.is_file() or not path.name.startswith(main.stem):
            continue
        rest = path.name[len(main.stem):]
        if rest.startswith(".") and not _is_partial(path.name):
            extras.append((path, rest))
    return extras


def _is_cross_device(error: OSError) -> bool:
    return error.errno == errno.EXDEV or getattr(error, "winerror", None) == _ERROR_NOT_SAME_DEVICE


def _claim(moves: list[tuple[Path, Path]]) -> bool:
    """Rename every (source, target) pair; on any clash undo the ones already made.

    os.rename on Windows refuses to replace an existing file, so a target that appears after the
    existence check is never overwritten.
    """
    done: list[tuple[Path, Path]] = []
    try:
        for source, target in moves:
            os.rename(source, target)
            done.append((source, target))
    except OSError as error:
        undone = True
        for source, target in reversed(done):
            try:
                os.rename(target, source)
            except OSError as undo_error:
                undone = False
                error.add_note(f"Moving {target} back to {source} also failed: {undo_error!r}")
        if isinstance(error, FileExistsError) and undone:
            return False
        raise
    return True


def _place(files: list[tuple[Path, str]], dest_dir: Path, stem: str, prefix: str | None) -> Path:
    """files[0] is the main file; each (path, suffix) moves to <prefix - stem><marker><suffix>."""
    suffix_units = max(utf16_units(suffix) for _, suffix in files)
    lead = f"{prefix} - " if prefix else ""
    number = 1
    while True:
        marker = f" ({number})" if number > 1 else ""
        base = lead + _fit_stem(stem, prefix, marker, suffix_units, dest_dir) + marker
        moves = [(path, dest_dir / f"{base}{suffix}") for path, suffix in files]
        if not any(target.exists() for _, target in moves) and _claim(moves):
            return moves[0][1]
        number += 1


def _stage_copies(files: list[tuple[Path, str]], dest_dir: Path) -> tuple[Path, list[tuple[Path, str]]]:
    """Copy files onto the destination drive so the final step is a rename.

    The copies go in <dest_dir>\\.snag-tmp\\<random job-like id>\\, so if Snag dies mid-copy the startup
    sweep (whose roots include every destination's .snag-tmp) deletes them. Returns (folder, copies).
    """
    staging = _make_dir(dest_dir / TMP_FOLDER / new_id())
    try:
        staged = []
        for index, (path, suffix) in enumerate(files):
            copy = staging / f"copy-{index}"
            shutil.copyfile(path, copy)
            staged.append((copy, suffix))
    except BaseException:
        remove_work_dir(staging)
        raise
    return staging, staged


def finalize(work: Path, dest_dir: Path, main: Path, prefix: str | None) -> Path:
    """Move the finished file and its extras (e.g. .srt subtitles) into dest_dir under a free name.

    Never overwrites: a taken name gets " (2)", " (3)"... applied to the main file and its extras
    together. Names are capped to Windows limits by shortening the title, never the extension.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    files = [(main, main.suffix), *_extras(work, main)]
    try:
        final = _place(files, dest_dir, main.stem, prefix)
    except OSError as error:
        if not _is_cross_device(error):
            raise
        staging, staged = _stage_copies(files, dest_dir)
        try:
            final = _place(staged, dest_dir, main.stem, prefix)
        finally:
            remove_work_dir(staging)
    remove_work_dir(work)
    return final
