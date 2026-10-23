"""User settings, stored as one JSON value per key on top of built-in defaults.

Each stored key is decoded and validated on its own: a bad, unknown or undecodable value is logged and
that key falls back to its default, so get() always returns valid Settings.

Start with Windows isn't stored: Windows' own Startup shortcut is the truth (see
snag.platform.startup.starts_with_windows and snag/server/routes/settings.py). The value older
versions stored under "start_with_windows" is ignored quietly.
"""
from __future__ import annotations

import json
import logging
import ntpath
from collections.abc import Callable
from dataclasses import dataclass, fields, replace
from pathlib import Path, PureWindowsPath
from typing import Any

from snag.jobs.models import Extras, FormatChoice, extras_from_dict, extras_to_dict, validate_extras
from snag.paths import music_folder, videos_folder

from .db import Connection

log = logging.getLogger(__name__)

ALLOWED_HEIGHTS = (360, 480, 720, 1080, 1440, 2160)
MAX_CONCURRENT_RANGE = range(1, 11)
MAX_REMEMBERED_DIRS = 20


@dataclass(frozen=True)
class Settings:
    video_dir: str
    audio_dir: str
    quick_max_height: int | None = None  # the best video's height cap; None: no limit, the best available
    quick_extras: Extras = Extras(subtitles=True, thumbnail_tags=True, chapters=True)
    default_extras: Extras = Extras(subtitles=True, thumbnail_tags=True, chapters=True)
    max_concurrent: int = 5
    check_updates_daily: bool = True
    # Destination folders the user picked, most recent first (see remember_dir), so that at startup the
    # queue sweeps the leftover working folders (.snag-tmp) a crash may have left in them too
    # (SnagCore.extra_roots). Nothing else uses them; the UI doesn't list them.
    remembered_dirs: tuple[str, ...] = ()


def default_settings(videos_base: Path | None = None, music_base: Path | None = None) -> Settings:
    """<Videos>\\Snag and <Music>\\Snag, using the shell's (possibly OneDrive-redirected) folders."""
    videos = videos_base or videos_folder()
    music = music_base or music_folder()
    return Settings(video_dir=str(videos / "Snag"), audio_dir=str(music / "Snag"))


def default_dest(settings: Settings, choice: FormatChoice) -> Path:
    return Path(settings.audio_dir if choice.audio_only else settings.video_dir)


# --- per-field validation ---

# Names Windows keeps for devices, with or without an extension ("NUL", "con.txt"), in any folder.
_DEVICE_NAMES = frozenset({
    "CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
    *(f"{port}{n}" for port in ("COM", "LPT") for n in (*"0123456789", "\u00b9", "\u00b2", "\u00b3")),
})
_DEVICE_PREFIXES = ("\\\\.\\", "\\\\?\\", "\\??\\")  # the device namespaces: \\.\, \\?\ and \??\
_NOT_IN_NAMES = frozenset('<>:"|?*')
FULL_PATH = "The folder must be a full path like C:\\Users\\me\\Videos or \\\\server\\share\\Videos."


DEVICE_PATH = "The folder can't be a device path (one starting \\\\.\\ or \\\\?\\). Pick an ordinary folder."


def folder_problem(value: Any) -> str | None:
    """Why value can't be a folder Snag saves to, as a sentence for the user; None when it can.

    A folder is a full path on a drive (C:\\Users\\me\\Videos) or on a network share
    (\\\\server\\share\\Videos). Refused: relative paths, device paths (\\\\.\\..., \\\\?\\... and \\??\\...,
    GLOBALROOT and \\\\?\\UNC\\ included), names Windows keeps for devices (NUL, COM1, con.txt, ...) in
    any part, names ending in a dot or a space, and characters Windows doesn't allow in names (NUL and
    other control characters, <>:"|?*)."""
    if not isinstance(value, str) or not value.strip():
        return FULL_PATH
    if any(ord(ch) < 32 for ch in value):
        return "That folder has characters Windows doesn't allow in names."
    if value.replace("/", "\\").startswith(_DEVICE_PREFIXES):
        return DEVICE_PATH
    normal = ntpath.normpath(value)
    if normal.startswith(_DEVICE_PREFIXES):
        return DEVICE_PATH
    path = PureWindowsPath(normal)
    if not path.is_absolute():
        return FULL_PATH
    for name in path.parts[1:]:
        if _NOT_IN_NAMES.intersection(name):
            return f"The folder name {name} has characters Windows doesn't allow in names."
        if name.split(".")[0].rstrip(" ").upper() in _DEVICE_NAMES:
            return f"Windows keeps the name {name} for a device. Pick another folder."
        if name.endswith((".", " ")):
            return f"The folder name \"{name}\" ends in a dot or a space, which Windows doesn't allow."
    return None


def validate_folder(value: Any) -> Path:
    """value as a folder Snag can save to (see folder_problem), normalized ("C:/Videos/" ->
    C:\\Videos, "a\\..\\b" -> b). Raises ValueError with folder_problem's sentence."""
    problem = folder_problem(value)
    if problem is not None:
        raise ValueError(problem)
    return Path(ntpath.normpath(value))


def _check_folder(value: Any) -> None:
    validate_folder(value)


def _folder_key(path: str) -> str:
    """Windows compares folder names case-insensitively (but "Straße" isn't "Strasse"), and a trailing
    separator or "." changes nothing."""
    return ntpath.normcase(ntpath.normpath(path))


def _check_folder_list(value: Any) -> None:
    if not isinstance(value, tuple):
        raise ValueError("must be a list of folders")
    if len(value) > MAX_REMEMBERED_DIRS:
        raise ValueError(f"at most {MAX_REMEMBERED_DIRS} folders")
    for folder in value:
        _check_folder(folder)
    if len({_folder_key(folder) for folder in value}) != len(value):
        raise ValueError("each folder may appear only once")


def _check_height(value: Any) -> None:
    if value is not None and (type(value) is not int or value not in ALLOWED_HEIGHTS):
        raise ValueError(f"quick_max_height must be null (no limit) or one of {ALLOWED_HEIGHTS}")


def _check_concurrent(value: Any) -> None:
    if type(value) is not int or value not in MAX_CONCURRENT_RANGE:
        raise ValueError("max_concurrent must be a whole number from 1 to 10")


def _check_bool(value: Any) -> None:
    if not isinstance(value, bool):
        raise ValueError("must be true or false")


_VALIDATORS: dict[str, Callable[[Any], None]] = {
    "video_dir": _check_folder,
    "audio_dir": _check_folder,
    "quick_max_height": _check_height,
    "quick_extras": validate_extras,
    "default_extras": validate_extras,
    "max_concurrent": _check_concurrent,
    "check_updates_daily": _check_bool,
    "remembered_dirs": _check_folder_list,
}
_RETIRED_KEYS = frozenset({"start_with_windows"})  # stored by older versions; ignored without a word
_EXTRAS_KEYS = {"quick_extras", "default_extras"}
_LIST_KEYS = {"remembered_dirs"}  # tuples in Settings, lists in JSON


def _validate(key: str, value: Any) -> None:
    try:
        _VALIDATORS[key](value)
    except ValueError as error:
        raise ValueError(f"{key}: {error}") from error


def _from_json_value(key: str, value: Any) -> Any:
    """A setting's plain JSON value -> its Settings type. Raises ValueError for one that can't be."""
    if key in _EXTRAS_KEYS:
        return extras_from_dict(value)
    if key in _LIST_KEYS:
        if not isinstance(value, list):
            raise ValueError("must be a list of folders")
        return tuple(value)
    return value


def _to_json_value(key: str, value: Any) -> Any:
    if key in _EXTRAS_KEYS:
        return extras_to_dict(value)
    if key in _LIST_KEYS:
        return list(value)
    return value


def _decode(key: str, raw: str) -> Any:
    return _from_json_value(key, json.loads(raw))


def _encode(key: str, value: Any) -> str:
    return json.dumps(_to_json_value(key, value))


def validate_changes(changes: dict[str, Any]) -> None:
    """Check settings changes (SettingsStore.update's keyword arguments) without saving anything: raises
    ValueError naming the first one that is unknown or invalid."""
    unknown = set(changes) - set(_VALIDATORS)
    if unknown:
        raise ValueError(f"unknown settings: {sorted(unknown)}")
    for key, value in changes.items():
        _validate(key, value)


def settings_from_json(data: Any) -> dict[str, Any]:
    """Settings changes as plain JSON (what the UI sends) -> keyword arguments for SettingsStore.update:
    extras objects become Extras and folder lists tuples; everything else is passed on as it is, for
    update to validate."""
    if not isinstance(data, dict):
        raise ValueError(f"settings must be an object, not {type(data).__name__}")
    changes: dict[str, Any] = {}
    for key, value in data.items():
        try:
            changes[key] = _from_json_value(key, value)
        except ValueError as error:
            raise ValueError(f"{key}: {error}") from error
    return changes


def settings_to_json(settings: Settings) -> dict[str, Any]:
    """Settings as plain JSON values, the shape settings_from_json takes back."""
    return {f.name: _to_json_value(f.name, getattr(settings, f.name)) for f in fields(settings)}


class SettingsStore:
    def __init__(self, conn: Connection, defaults: Settings):
        self._conn = conn
        self._lock = conn.lock
        self._defaults = defaults

    def get(self) -> Settings:
        with self._lock:
            rows = self._conn.execute("SELECT key, value FROM settings").fetchall()
        values: dict[str, Any] = {}
        for row in rows:
            key = row["key"]
            if key in _RETIRED_KEYS:
                continue
            if key not in _VALIDATORS:
                log.warning("Ignoring unknown stored setting %r", key)
                continue
            try:
                value = _decode(key, row["value"])
                _validate(key, value)
            except (ValueError, RecursionError) as error:  # bad JSON; JSON nested too deep
                log.warning("Stored setting %r is invalid (%s); using the default", key, error)
                continue
            values[key] = value
        return replace(self._defaults, **values)

    def update(self, **changes: Any) -> Settings:
        """Validate and save the given settings together; raises ValueError and saves nothing if any is bad."""
        validate_changes(changes)
        encoded = [(key, _encode(key, value)) for key, value in changes.items()]
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.executemany(
                    "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    encoded,
                )
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            return self.get()

    def update_from_json(self, data: Any) -> Settings:
        """update() with the plain JSON the UI sends (see settings_from_json)."""
        return self.update(**settings_from_json(data))


def remember_dir(settings_store: SettingsStore, path: str | Path) -> Settings:
    """Put a destination folder the user picked first in remembered_dirs: a folder already there (in any
    case, with or without a trailing separator) moves to the front, and only the newest
    MAX_REMEMBERED_DIRS are kept. Returns the new settings; raises ValueError for a path that can't be a
    folder (validate_folder)."""
    folder = str(validate_folder(str(path)))
    key = _folder_key(folder)
    with settings_store._lock:  # the read and the write together, so concurrent calls lose nothing
        current = settings_store.get().remembered_dirs
        kept = [known for known in current if _folder_key(known) != key]
        return settings_store.update(remembered_dirs=(folder, *kept)[:MAX_REMEMBERED_DIRS])
