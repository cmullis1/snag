import ctypes
import sys
from pathlib import Path

import pytest

import snag.paths as paths_module
from snag.paths import AppPaths, Tools, music_folder, videos_folder


def test_app_paths_layout(tmp_path):
    paths = AppPaths(tmp_path / "Snag")
    assert paths.bin_dir == tmp_path / "Snag" / "bin"
    assert paths.db_file == tmp_path / "Snag" / "snag.db"


def test_default_uses_localappdata(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert AppPaths.default().root == tmp_path / "Snag"


def test_default_falls_back_to_home_when_localappdata_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert AppPaths.default().root == tmp_path / "AppData" / "Local" / "Snag"


def test_tools_from_bin(tmp_path):
    tools = Tools.from_bin(tmp_path)
    assert tools.ytdlp == (str(tmp_path / "yt-dlp.exe"),)
    assert tools.ffmpeg_dir == tmp_path
    assert tools.deno == tmp_path / "deno.exe"


def test_tools_from_bin_makes_a_relative_folder_absolute(tmp_path, monkeypatch):
    # yt-dlp runs with its work folder as the current folder, so a relative tool path would break.
    monkeypatch.chdir(tmp_path)
    tools = Tools.from_bin(Path("bin"))
    assert tools.ytdlp == (str(tmp_path / "bin" / "yt-dlp.exe"),)
    assert tools.ffmpeg_dir == tmp_path / "bin" and tools.ffmpeg_dir.is_absolute()
    assert tools.deno == tmp_path / "bin" / "deno.exe"


def test_work_and_cookies_dirs(tmp_path):
    paths = AppPaths(tmp_path / "Snag")
    assert paths.work_dir == tmp_path / "Snag" / "work"
    assert paths.cookies_dir == tmp_path / "Snag" / "cookies"


def test_known_folders_are_absolute_existing_paths():
    videos, music = videos_folder(), music_folder()
    assert videos.is_absolute() and music.is_absolute()
    assert videos != music


def test_known_folder_falls_back_to_home(monkeypatch, tmp_path):
    monkeypatch.setattr(paths_module, "known_folder", lambda guid: None)
    monkeypatch.setattr(paths_module.Path, "home", classmethod(lambda cls: tmp_path))
    assert videos_folder() == tmp_path / "Videos"
    assert music_folder() == tmp_path / "Music"


def test_known_folder_reads_the_windows_api():
    # FOLDERID_Videos resolves through the shell, so a redirected (e.g. OneDrive) folder is honoured.
    assert paths_module.known_folder(paths_module.FOLDERID_VIDEOS) == videos_folder()
    assert paths_module.known_folder("{00000000-0000-0000-0000-000000000000}") is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows shell API")
def test_known_folder_prototypes_are_set_once_and_kept_private():
    # Set at import on Snag's own WinDLL handles, so other code using ctypes.windll is unaffected.
    assert paths_module._SHGetKnownFolderPath.argtypes is not None
    assert paths_module._CoTaskMemFree.argtypes is not None
    paths_module.known_folder(paths_module.FOLDERID_VIDEOS)
    assert ctypes.windll.shell32.SHGetKnownFolderPath.argtypes is None
