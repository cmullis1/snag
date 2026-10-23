"""Windows-safe file and folder names."""
from __future__ import annotations

import re
from pathlib import Path

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# Device names Windows reserves whatever the extension ("CON.txt", "con .mp4" are both the console).
_RESERVED = {
    "CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
    *(f"{port}{suffix}" for port in ("COM", "LPT") for suffix in (*"0123456789", "¹", "²", "³")),
}
MAX_NAME = 150


def safe_name(name: str, fallback: str = "Untitled") -> str:
    cleaned = _ILLEGAL.sub("_", name).strip().rstrip(". ")[:MAX_NAME].rstrip(". ")
    if not cleaned:
        return fallback
    return f"_{cleaned}" if is_reserved(cleaned) else cleaned


def is_reserved(name: str) -> bool:
    return name.split(".", 1)[0].rstrip(" ").upper() in _RESERVED


def playlist_prefix(index: int, count: int | None) -> str:
    width = max(2, len(str(count or index)))
    return f"{index:0{width}d}"


def playlist_dest(base: Path, playlist_title: str) -> Path:
    return base / safe_name(playlist_title, fallback="Playlist")
