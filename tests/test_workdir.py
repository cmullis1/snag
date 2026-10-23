import errno
import os
import shutil
from pathlib import Path

import pytest

import snag.jobs.workdir as workdir
from snag.jobs.models import is_job_id, new_id
from snag.jobs.workdir import (
    MAX_NAME_UNITS, TMP_FOLDER, clear_cookies_dir, cookies_file_path, finalize, find_main_file, make_work_dir,
    remove_work_dir, sweep_orphans, work_dir_path, work_root_for,
)


@pytest.fixture
def local_work(tmp_path) -> Path:
    return tmp_path / "Snag" / "work"


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


# --- choosing the working folder ---

def test_same_drive_uses_the_local_work_folder(tmp_path, local_work):
    assert work_root_for(tmp_path / "Videos", local_work) == local_work
    assert work_dir_path(tmp_path / "Videos", "job1", local_work) == local_work / "job1"


def test_drive_comparison_ignores_case():
    assert work_root_for(Path("c:/Users/me/Videos"), Path("C:/Snag/work")) == Path("C:/Snag/work")


def test_other_drive_uses_a_hidden_folder_beside_the_destination():
    dest = Path("Q:/Media")
    assert work_root_for(dest, Path("C:/Snag/work")) == dest / TMP_FOLDER
    assert work_dir_path(dest, "job1", Path("C:/Snag/work")) == dest / TMP_FOLDER / "job1"


def test_network_share_counts_as_another_drive():
    dest = Path("//server/share/Media")
    assert work_root_for(dest, Path("C:/Snag/work")) == dest / TMP_FOLDER


def test_make_local_work_dir_does_not_create_snag_tmp(tmp_path, local_work):
    dest = tmp_path / "Videos"
    work = make_work_dir(dest, "job1", local_work)
    assert work == local_work / "job1" and work.is_dir()
    assert not (dest / TMP_FOLDER).exists()


def test_make_and_remove_snag_tmp_work_dir(tmp_path, monkeypatch, local_work):
    monkeypatch.setattr(workdir, "work_root_for", lambda dest, local: dest / TMP_FOLDER)
    work = make_work_dir(tmp_path, "job1", local_work)
    assert work == tmp_path / TMP_FOLDER / "job1" and work.is_dir()
    if os.name == "nt":
        import ctypes
        attributes = ctypes.windll.kernel32.GetFileAttributesW(str(work.parent))
        assert attributes & 0x02  # hidden
    assert remove_work_dir(work) is True
    assert not (tmp_path / TMP_FOLDER).exists()  # removed when empty


def test_remove_keeps_shared_snag_tmp_when_other_jobs_exist(tmp_path, monkeypatch, local_work):
    monkeypatch.setattr(workdir, "work_root_for", lambda dest, local: dest / TMP_FOLDER)
    first = make_work_dir(tmp_path, "a", local_work)
    make_work_dir(tmp_path, "b", local_work)
    assert remove_work_dir(first) is True
    assert (tmp_path / TMP_FOLDER / "b").is_dir()


def test_remove_never_removes_the_local_work_folder(tmp_path, local_work):
    work = make_work_dir(tmp_path, "job1", local_work)
    (work / "A.mp4.part").write_bytes(b"x")
    assert remove_work_dir(work) is True
    assert not work.exists()
    assert local_work.is_dir()


def test_remove_missing_folder_succeeds(tmp_path):
    assert remove_work_dir(tmp_path / "nope") is True


def test_remove_retries_while_a_file_is_briefly_held(tmp_path, monkeypatch, local_work):
    work = make_work_dir(tmp_path, "job1", local_work)
    (work / "A.mp4").write_bytes(b"x")
    real_rmtree = shutil.rmtree
    calls = []

    def flaky_rmtree(path, *args, **kwargs):
        calls.append(path)
        if len(calls) < 3:
            raise PermissionError(errno.EACCES, "in use")
        real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(workdir.shutil, "rmtree", flaky_rmtree)
    monkeypatch.setattr(workdir.time, "sleep", lambda seconds: None)
    assert remove_work_dir(work) is True
    assert len(calls) == 3 and not work.exists()


def test_remove_reports_failure_when_the_file_stays_held(tmp_path, monkeypatch, local_work):
    work = make_work_dir(tmp_path, "job1", local_work)

    def stuck(path, *args, **kwargs):
        raise PermissionError(errno.EACCES, "in use")

    monkeypatch.setattr(workdir.shutil, "rmtree", stuck)
    monkeypatch.setattr(workdir.time, "sleep", lambda seconds: None)
    assert remove_work_dir(work) is False
    assert work.exists()


def test_cookies_file_path(tmp_path):
    assert cookies_file_path(tmp_path / "cookies", "job1") == tmp_path / "cookies" / "job1.txt"


def _backdate(path: Path) -> None:
    """Give the file an mtime from before this process started, like one a previous run left behind."""
    old = workdir.PROCESS_START - 3600
    os.utime(path, (old, old))


def test_clear_cookies_dir_deletes_job_cookie_files_and_old_lookup_files(tmp_path):
    cookies = tmp_path / "cookies"
    cookies.mkdir()
    job_file = cookies / f"{new_id()}.txt"
    old_lookup = cookies / f"lookup-{new_id()}.txt"
    for path in (job_file, old_lookup):
        path.write_text("# Netscape HTTP Cookie File")
    _backdate(old_lookup)
    assert clear_cookies_dir(cookies) is True
    assert list(cookies.iterdir()) == []


def test_clear_cookies_dir_keeps_a_running_lookups_file(tmp_path):
    cookies = tmp_path / "cookies"
    cookies.mkdir()
    fresh = cookies / f"lookup-{new_id()}.txt"
    fresh.write_text("# Netscape HTTP Cookie File")
    assert clear_cookies_dir(cookies) is True
    assert fresh.exists()


def test_clear_cookies_dir_leaves_unrelated_files(tmp_path):
    cookies = tmp_path / "cookies"
    cookies.mkdir()
    keep = ["job1.txt", "stray.tmp", f"{new_id()}.json", "lookup-notes.md", f"{new_id()}.txt.bak"]
    for name in keep:
        (cookies / name).write_text("x")
        _backdate(cookies / name)
    (cookies / "subfolder").mkdir()
    assert clear_cookies_dir(cookies) is True
    assert sorted(p.name for p in cookies.iterdir()) == sorted([*keep, "subfolder"])


def test_clear_cookies_dir_without_the_folder(tmp_path):
    assert clear_cookies_dir(tmp_path / "missing") is True


def test_clear_cookies_dir_reports_a_file_it_could_not_delete(tmp_path, monkeypatch):
    cookies = tmp_path / "cookies"
    cookies.mkdir()
    held, other = f"{new_id()}.txt", f"{new_id()}.txt"
    (cookies / held).write_text("x")
    (cookies / other).write_text("x")
    real_unlink = Path.unlink

    def unlink(self, *args, **kwargs):
        if self.name == held:
            raise PermissionError(errno.EACCES, "in use")
        real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    assert clear_cookies_dir(cookies) is False
    assert [p.name for p in cookies.iterdir()] == [held]  # the rest are still removed


# --- startup sweep ---

def test_sweep_orphans_removes_folders_of_unknown_jobs(tmp_path, local_work):
    kept, orphan, tmp_orphan = new_id(), new_id(), new_id()
    for job_id in (kept, orphan):
        (local_work / job_id).mkdir(parents=True)
    snag_tmp = tmp_path / "Videos" / TMP_FOLDER
    (snag_tmp / tmp_orphan).mkdir(parents=True)
    (snag_tmp / tmp_orphan / "A.mp4.part").write_bytes(b"x")
    removed = sweep_orphans([local_work, snag_tmp, tmp_path / "missing"], keep_ids={kept})
    assert sorted(removed) == sorted([local_work / orphan, snag_tmp / tmp_orphan])
    assert (local_work / kept).is_dir()
    assert not snag_tmp.exists()  # emptied .snag-tmp goes too
    assert local_work.is_dir()


def test_sweep_only_touches_folders_named_like_job_ids(tmp_path):
    root = tmp_path / "root"
    (root / "My Videos").mkdir(parents=True)
    (root / "notes.txt").write_text("x")
    assert sweep_orphans([root], keep_ids=set()) == []
    assert (root / "My Videos").is_dir() and (root / "notes.txt").is_file()


# --- finding the downloaded file ---

def test_find_main_file_prefers_hint_inside_work_folder(tmp_path):
    main = tmp_path / "A.mp4"
    main.write_bytes(b"x")
    assert find_main_file(tmp_path, str(main)) == main


def test_find_main_file_reads_a_relative_hint_inside_the_work_folder(tmp_path):
    # yt-dlp runs inside the work folder, so a relative path is relative to it, not to Snag.
    (tmp_path / "Big.mp4").write_bytes(b"x" * 1000)
    (tmp_path / "A.mp4").write_bytes(b"x")
    assert find_main_file(tmp_path, "A.mp4") == tmp_path / "A.mp4"
    assert find_main_file(tmp_path, "..\\Other.mp4") == tmp_path / "Big.mp4"


def test_find_main_file_ignores_hint_outside_work_folder(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "A.mp4").write_bytes(b"x")
    outside = tmp_path / "Other.mp4"
    outside.write_bytes(b"x" * 1000)
    assert find_main_file(work, str(outside)) == work / "A.mp4"
    assert find_main_file(work, str(work / ".." / "Other.mp4")) == work / "A.mp4"


def test_find_main_file_falls_back_to_largest_media_file(tmp_path):
    (tmp_path / "A.mp4").write_bytes(b"x" * 100)
    (tmp_path / "A.en.srt").write_bytes(b"x" * 500)
    (tmp_path / "B.mp4.part").write_bytes(b"x" * 900)
    (tmp_path / "B.mp4.part-Frag3").write_bytes(b"x" * 900)
    (tmp_path / "A.temp.mp4").write_bytes(b"x" * 900)
    (tmp_path / "cookies.txt").write_bytes(b"x" * 900)
    assert find_main_file(tmp_path, "C:\\garbled\\path.mp4") == tmp_path / "A.mp4"


@pytest.mark.parametrize("name", ["Ep.Part.One.mp4", "Show.Temp.Cut.mkv", "Partial.Eclipse.webm", "temp.mp4", "My.ytdl.Story.m4a"])
def test_find_main_file_finds_finished_files_whose_title_looks_partial(tmp_path, name):
    (tmp_path / name).write_bytes(b"x" * 100)
    (tmp_path / "Other.mp4.part").write_bytes(b"x" * 900)
    assert find_main_file(tmp_path, None) == tmp_path / name


@pytest.mark.parametrize("name", [
    "A.mp4.part", "A.f299.mp4.part", "A.f299.mp4.part-Frag12", "A.PART", "A.ytdl", "A.mp4.ytdl", "A.temp",
    "A.temp.mp4", "A.Temp.MKV",
])
def test_find_main_file_skips_files_yt_dlp_is_still_writing(tmp_path, name):
    (tmp_path / name).write_bytes(b"x" * 900)
    assert find_main_file(tmp_path, None) is None


def test_find_main_file_none_when_empty(tmp_path):
    assert find_main_file(tmp_path, None) is None
    assert find_main_file(tmp_path / "missing", None) is None


# --- moving into place ---

def test_finalize_moves_file_and_siblings(tmp_path, local_work):
    dest = tmp_path / "out"
    work = make_work_dir(dest, "job1", local_work)
    (work / "A.mp4").write_bytes(b"video")
    (work / "A.en.srt").write_text("subs")
    final = finalize(work, dest, work / "A.mp4", prefix=None)
    assert final == dest / "A.mp4"
    assert final.read_bytes() == b"video"
    assert (dest / "A.en.srt").read_text() == "subs"
    assert not work.exists()


def test_finalize_never_overwrites(tmp_path, local_work):
    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "A.mp4").write_bytes(b"old")
    (dest / "A (2).mp4").write_bytes(b"old2")
    work = make_work_dir(dest, "job1", local_work)
    (work / "A.mp4").write_bytes(b"new")
    final = finalize(work, dest, work / "A.mp4", prefix=None)
    assert final == dest / "A (3).mp4"
    assert (dest / "A.mp4").read_bytes() == b"old" and (dest / "A (2).mp4").read_bytes() == b"old2"
    assert final.read_bytes() == b"new"


def test_finalize_keeps_extras_paired_with_the_final_name(tmp_path, local_work):
    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "A.mp4").write_bytes(b"old")
    (dest / "A (2).en.srt").write_text("someone else's")  # A (2).mp4 is free but its subtitle name isn't
    work = make_work_dir(dest, "job1", local_work)
    (work / "A.mp4").write_bytes(b"new")
    (work / "A.en.srt").write_text("subs")
    final = finalize(work, dest, work / "A.mp4", prefix=None)
    assert final == dest / "A (3).mp4"
    assert (dest / "A (3).en.srt").read_text() == "subs"
    assert (dest / "A (2).en.srt").read_text() == "someone else's"
    assert not (dest / "A (2).mp4").exists()


def test_finalize_loses_a_race_without_overwriting(tmp_path, monkeypatch, local_work):
    """Another job grabs the name between the existence check and the rename."""
    dest = tmp_path / "out"
    work = make_work_dir(dest, "job1", local_work)
    (work / "A.mp4").write_bytes(b"mine")
    real_rename = os.rename

    def racing_rename(src, dst):
        if Path(dst) == dest / "A.mp4" and not Path(dst).exists():
            Path(dst).write_bytes(b"theirs")
        real_rename(src, dst)

    monkeypatch.setattr(workdir.os, "rename", racing_rename)
    final = finalize(work, dest, work / "A.mp4", prefix=None)
    assert final == dest / "A (2).mp4" and final.read_bytes() == b"mine"
    assert (dest / "A.mp4").read_bytes() == b"theirs"


def test_finalize_only_moves_real_siblings(tmp_path, local_work):
    dest = tmp_path / "out"
    work = make_work_dir(dest, "job1", local_work)
    for name in ["A.mp4", "A.en.srt", "AB.mp4", "A.mp4.part", "A.f299.mp4.part-Frag12", "A.ytdl", "A.temp.mp4"]:
        (work / name).write_bytes(b"x")
    finalize(work, dest, work / "A.mp4", prefix=None)
    assert sorted(p.name for p in dest.iterdir()) == ["A.en.srt", "A.mp4"]


def test_finalize_moves_extras_of_a_title_that_looks_partial(tmp_path, local_work):
    dest = tmp_path / "out"
    work = make_work_dir(dest, "job1", local_work)
    for name in ["Ep.Part.One.mp4", "Ep.Part.One.en.srt", "Ep.Part.One.en.srt.part", "Ep.Part.One.temp.mp4"]:
        (work / name).write_bytes(b"x")
    finalize(work, dest, work / "Ep.Part.One.mp4", prefix=None)
    assert sorted(p.name for p in dest.iterdir()) == ["Ep.Part.One.en.srt", "Ep.Part.One.mp4"]


def test_finalize_with_playlist_prefix(tmp_path, local_work):
    work = make_work_dir(tmp_path, "job1", local_work)
    (work / "A.mp4").write_bytes(b"v")
    (work / "A.en.srt").write_bytes(b"s")
    assert finalize(work, tmp_path, work / "A.mp4", prefix="03").name == "03 - A.mp4"
    assert (tmp_path / "03 - A.en.srt").is_file()


def test_finalize_caps_long_names_keeping_extension(tmp_path, monkeypatch, local_work):
    monkeypatch.setattr(workdir, "MAX_NAME_UNITS", 30)  # small limit so the test files stay short
    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "003 - abcdefghijklmnopq.mp4").write_bytes(b"old")  # forces a " (2)" on the capped name
    work = make_work_dir(dest, "job1", local_work)
    main = work / ("abcdefghijklmnopqrstuvwxyz" + ".mp4")
    main.write_bytes(b"v")
    (work / "abcdefghijklmnopqrstuvwxyz.en.srt").write_bytes(b"s")
    final = finalize(work, dest, main, prefix="003")
    # 30 units - "003 - " - " (2)" - ".en.srt" (the longest suffix) leaves 13 for the title.
    assert final.name == "003 - abcdefghijklm (2).mp4"
    assert (dest / "003 - abcdefghijklm (2).en.srt").is_file()


def test_name_budget_reserves_room_for_number_and_longest_extra(tmp_path, monkeypatch):
    monkeypatch.setattr(workdir, "MAX_PATH_UNITS", 10_000)
    stem = workdir._fit_stem("x" * 400, prefix="01", marker=" (12)", suffix_units=len(".en-US.srt"), dest_dir=tmp_path)
    assert utf16_len(f"01 - {stem} (12).en-US.srt") == MAX_NAME_UNITS


def test_name_budget_never_splits_a_surrogate_pair(tmp_path, monkeypatch):
    monkeypatch.setattr(workdir, "MAX_PATH_UNITS", 10_000)
    stem = workdir._fit_stem("😀" * 200, prefix=None, marker="", suffix_units=4, dest_dir=tmp_path)
    assert stem == "😀" * 125  # 250 units + ".mp4" = 254, a 126th emoji would need 256


# _fit_stem only does arithmetic on the path, so made-up short destinations keep these tests
# independent of how long tmp_path happens to be.
SHORT_DEST = Path("C:/v")


def test_name_budget_keeps_the_full_path_workable():
    dest = SHORT_DEST / ("d" * 100)
    stem = workdir._fit_stem("x" * 400, prefix=None, marker="", suffix_units=4, dest_dir=dest)
    assert utf16_len(str(dest / f"{stem}.mp4")) <= workdir.MAX_PATH_UNITS


def test_name_budget_keeps_a_readable_stem_in_very_deep_folders():
    dest = SHORT_DEST / ("d" * 240)
    stem = workdir._fit_stem("x" * 400, prefix=None, marker="", suffix_units=4, dest_dir=dest)
    assert len(stem) == workdir.MIN_STEM_UNITS


def test_finalize_copies_across_drives_without_overwriting(tmp_path, monkeypatch, local_work):
    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "A.mp4").write_bytes(b"old")
    work = make_work_dir(dest, "job1", local_work)
    (work / "A.mp4").write_bytes(b"new")
    (work / "A.en.srt").write_text("subs")
    real_rename = os.rename

    def cross_drive_rename(src, dst):
        if Path(src).is_relative_to(work):
            raise OSError(errno.EXDEV, "Not same device")
        real_rename(src, dst)

    monkeypatch.setattr(workdir.os, "rename", cross_drive_rename)
    final = finalize(work, dest, work / "A.mp4", prefix=None)
    assert final == dest / "A (2).mp4" and final.read_bytes() == b"new"
    assert (dest / "A (2).en.srt").read_text() == "subs"
    assert sorted(p.name for p in dest.iterdir()) == ["A (2).en.srt", "A (2).mp4", "A.mp4"]  # no temp copies left
    assert not work.exists()


def test_cross_drive_copies_are_staged_where_the_startup_sweep_finds_them(tmp_path, monkeypatch, local_work):
    """If Snag dies mid-copy, the copies sit in a job-id-named folder under <dest>/.snag-tmp, which
    sweep_orphans removes at the next start."""
    dest = tmp_path / "out"
    work = make_work_dir(dest, "job1", local_work)
    (work / "A.mp4").write_bytes(b"new")
    real_rename, real_copy = os.rename, shutil.copyfile
    copies: list[Path] = []

    def cross_drive_rename(src, dst):
        if Path(src).is_relative_to(work):
            raise OSError(errno.EXDEV, "Not same device")
        real_rename(src, dst)

    def recording_copy(src, dst):
        copies.append(Path(dst))
        return real_copy(src, dst)

    monkeypatch.setattr(workdir.os, "rename", cross_drive_rename)
    monkeypatch.setattr(workdir.shutil, "copyfile", recording_copy)
    assert finalize(work, dest, work / "A.mp4", prefix=None) == dest / "A.mp4"
    staging = copies[0].parent
    assert staging.parent == dest / TMP_FOLDER and is_job_id(staging.name)
    assert not (dest / TMP_FOLDER).exists()  # cleaned up once the move is done


def test_failed_cross_drive_copy_leaves_nothing_behind(tmp_path, monkeypatch, local_work):
    dest = tmp_path / "out"
    work = make_work_dir(dest, "job1", local_work)
    (work / "A.mp4").write_bytes(b"new")
    (work / "A.en.srt").write_text("subs")
    real_copy = shutil.copyfile

    def cross_drive_rename(src, dst):
        raise OSError(errno.EXDEV, "Not same device")

    def disk_full_on_second(src, dst):
        if Path(src).name == "A.en.srt":
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_copy(src, dst)

    monkeypatch.setattr(workdir.os, "rename", cross_drive_rename)
    monkeypatch.setattr(workdir.shutil, "copyfile", disk_full_on_second)
    with pytest.raises(OSError):
        finalize(work, dest, work / "A.mp4", prefix=None)
    assert list(dest.iterdir()) == []
    assert (work / "A.mp4").read_bytes() == b"new"  # the download itself is untouched


def test_claim_keeps_the_original_error_when_undoing_fails(tmp_path, monkeypatch):
    first, second = tmp_path / "a", tmp_path / "b"
    first.write_bytes(b"a")
    second.write_bytes(b"b")
    real_rename = os.rename

    def rename(src, dst):
        if Path(src) == second:
            raise PermissionError(errno.EACCES, "held by antivirus")
        if Path(src) == tmp_path / "A2":
            raise OSError(errno.EIO, "undo failed")
        real_rename(src, dst)

    monkeypatch.setattr(workdir.os, "rename", rename)
    with pytest.raises(PermissionError, match="held by antivirus") as caught:
        workdir._claim([(first, tmp_path / "A2"), (second, tmp_path / "B2")])
    assert any("undo failed" in note for note in caught.value.__notes__)


def test_claim_raises_a_clash_it_cannot_undo_instead_of_trying_another_name(tmp_path, monkeypatch):
    first, second = tmp_path / "a", tmp_path / "b"
    first.write_bytes(b"a")
    second.write_bytes(b"b")
    real_rename = os.rename

    def rename(src, dst):
        if Path(src) == second:
            raise FileExistsError(errno.EEXIST, "taken")
        if Path(src) == tmp_path / "A2":
            raise OSError(errno.EIO, "undo failed")
        real_rename(src, dst)

    monkeypatch.setattr(workdir.os, "rename", rename)
    with pytest.raises(FileExistsError) as caught:
        workdir._claim([(first, tmp_path / "A2"), (second, tmp_path / "B2")])
    assert any("undo failed" in note for note in caught.value.__notes__)


def test_finalize_rolls_back_when_an_extra_name_is_taken_mid_move(tmp_path, monkeypatch, local_work):
    dest = tmp_path / "out"
    work = make_work_dir(dest, "job1", local_work)
    (work / "A.mp4").write_bytes(b"mine")
    (work / "A.en.srt").write_text("subs")
    real_rename = os.rename

    def racing_rename(src, dst):
        if Path(dst) == dest / "A.en.srt" and not Path(dst).exists():
            Path(dst).write_text("theirs")
        real_rename(src, dst)

    monkeypatch.setattr(workdir.os, "rename", racing_rename)
    final = finalize(work, dest, work / "A.mp4", prefix=None)
    assert final == dest / "A (2).mp4"
    assert (dest / "A (2).en.srt").read_text() == "subs"
    assert not (dest / "A.mp4").exists()  # the first claim was undone
    assert (dest / "A.en.srt").read_text() == "theirs"
