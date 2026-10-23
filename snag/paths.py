"""Where Snag keeps its data and which tool executables it runs."""
from __future__ import annotations

import ctypes
import os
import sys
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

FOLDERID_VIDEOS = "{18989B1D-99B5-455B-841C-AB7C74E4DDFC}"
FOLDERID_MUSIC = "{4BD8D571-6D19-48D3-BE97-422220080E43}"


class _GUID(ctypes.Structure):
    _fields_ = [("data", ctypes.c_ubyte * 16)]


if sys.platform == "win32":
    # Private WinDLL handles so these prototypes are set once and don't leak into ctypes.windll.
    _SHGetKnownFolderPath = ctypes.WinDLL("shell32").SHGetKnownFolderPath
    _SHGetKnownFolderPath.argtypes = [
        ctypes.POINTER(_GUID), ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p),
    ]
    _SHGetKnownFolderPath.restype = ctypes.c_long
    _CoTaskMemFree = ctypes.WinDLL("ole32").CoTaskMemFree
    _CoTaskMemFree.argtypes = [ctypes.c_void_p]
    _CoTaskMemFree.restype = None


def known_folder(folder_id: str) -> Path | None:
    """Asks the Windows shell for a Known Folder, which follows redirection (e.g. to OneDrive)."""
    if sys.platform != "win32":
        return None
    guid = _GUID((ctypes.c_ubyte * 16)(*uuid.UUID(folder_id).bytes_le))
    result = ctypes.c_wchar_p()
    status = _SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(result))
    try:
        if status != 0 or not result.value:
            return None
        return Path(result.value)
    finally:
        # The shell allocates the string even on some failures; it must always be released.
        _CoTaskMemFree(ctypes.cast(result, ctypes.c_void_p))


def videos_folder() -> Path:
    return known_folder(FOLDERID_VIDEOS) or Path.home() / "Videos"


def music_folder() -> Path:
    return known_folder(FOLDERID_MUSIC) or Path.home() / "Music"


@dataclass(frozen=True)
class AppPaths:
    root: Path

    @classmethod
    def default(cls, environ: Mapping[str, str] | None = None) -> AppPaths:
        """%LOCALAPPDATA%\\Snag: the binaries and database are machine-specific, so they don't roam.
        environ: where %LOCALAPPDATA% is read (os.environ by default)."""
        local = (os.environ if environ is None else environ).get("LOCALAPPDATA")
        base = Path(local) if local else Path.home() / "AppData" / "Local"
        return cls(base / "Snag")

    @property
    def bin_dir(self) -> Path:
        return self.root / "bin"

    @property
    def db_file(self) -> Path:
        return self.root / "snag.db"

    @property
    def work_dir(self) -> Path:
        """Partial downloads, when the destination is on the same drive (see snag.jobs.workdir)."""
        return self.root / "work"

    @property
    def cookies_dir(self) -> Path:
        """Per-job cookie files; never written next to downloads."""
        return self.root / "cookies"


@dataclass(frozen=True)
class Tools:
    """ytdlp is a command prefix so tests can run a Python stand-in instead of yt-dlp.exe."""

    ytdlp: tuple[str, ...]
    ffmpeg_dir: Path
    deno: Path

    @classmethod
    def from_bin(cls, bin_dir: Path) -> Tools:
        """Tools in bin_dir, as absolute paths: yt-dlp runs in a job's work folder, so a relative one would break."""
        bin_dir = Path(os.path.abspath(bin_dir))
        return cls(
            ytdlp=(str(bin_dir / "yt-dlp.exe"),),
            ffmpeg_dir=bin_dir,
            deno=bin_dir / "deno.exe",
        )
