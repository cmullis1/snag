"""Copies ui/ to extension/ui/ exactly (spec 3.3): the extension can't load the UI modules from the
local server (Manifest V3 forbids remote code), so it ships its own copy, which is never edited by
hand. Files missing from ui/ are deleted from the copy, before anything is copied, so a rename that
only changes case (the same file on Windows) isn't copied and then deleted. A missing ui/ is an error,
never an empty source that would delete the whole copy. The tests fail while the two differ
(tests/test_extension_package.py), so run this after changing anything in ui/:

    .venv/Scripts/python scripts/sync_ui.py
"""
from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "ui"
TARGET = ROOT / "extension" / "ui"
# Text files are compared with their line endings as Git stores them (CRLF read as LF): with
# core.autocrlf a checkout writes CRLF while a freshly written file may have LF, and Git commits both
# the same. Other files are compared byte for byte.
TEXT_SUFFIXES = frozenset({".js", ".mjs", ".css", ".html", ".json", ".svg", ".md", ".txt"})
CRLF, LF = b"\r\n", b"\n"


def _files(root: Path) -> dict[str, Path]:
    """Every file under root by its path relative to root, written with forward slashes."""
    if not root.is_dir():
        return {}
    return {path.relative_to(root).as_posix(): path for path in root.rglob("*") if path.is_file()}


def _require_folder(source: Path) -> None:
    if not source.is_dir():
        raise NotADirectoryError(f"{source} isn't a folder: nothing to copy the extension's ui from")


def _remove_empty_folders(root: Path) -> None:
    """Folders under root the deletions emptied, deepest first."""
    if not root.is_dir():
        return
    for folder in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        if not any(folder.iterdir()):
            folder.rmdir()


def _content(path: Path) -> bytes:
    data = path.read_bytes()
    return data.replace(CRLF, LF) if path.suffix.lower() in TEXT_SUFFIXES else data


def differences(source: Path = SOURCE, target: Path = TARGET) -> list[str]:
    """The relative paths (sorted) that differ: missing from either side, or with other content.
    Raises NotADirectoryError when source isn't a folder."""
    _require_folder(source)
    ours, theirs = _files(source), _files(target)
    return sorted(
        name for name in ours.keys() | theirs.keys()
        if name not in ours or name not in theirs or _content(ours[name]) != _content(theirs[name])
    )


def sync(source: Path = SOURCE, target: Path = TARGET) -> list[str]:
    """Makes target an exact copy of source; returns the paths it changed. Raises NotADirectoryError
    (changing nothing) when source isn't a folder."""
    changed = differences(source, target)
    ours = _files(source)
    # Stale files and the folders they leave empty go first: with a case-only rename, the old and new
    # names are one file on Windows, and deleting the old name after copying would delete the copy.
    for name in changed:
        if name not in ours:
            (target / name).unlink()
    _remove_empty_folders(target)
    for name in changed:
        if name in ours:
            destination = target / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ours[name], destination)
    return changed


def main() -> None:
    changed = sync()
    if changed:
        print(f"Updated extension/ui: {', '.join(changed)}")
    else:
        print("extension/ui already matches ui/")


if __name__ == "__main__":
    main()
