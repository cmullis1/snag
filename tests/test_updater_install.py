import asyncio
import os
import sys
import zlib
from pathlib import Path

import httpx
import pytest

import snag.updater.install as install_module
from snag.updater.install import (
    DENO,
    FFMPEG,
    YTDLP,
    InstallError,
    clean_leftovers,
    default_smoke,
    install,
    prepare,
)
from snag.updater.releases import ReleaseError, UpdaterError
from tests.updater_env import (
    DENO_ZIP,
    FFMPEG_ZIP,
    GOOD,
    FakeGitHub,
    fake_smoke,
    ffmpeg_zip,
    publish_deno,
    publish_ffmpeg,
    publish_ytdlp,
    sha256,
    zip_of,
)

NEW_YTDLP = GOOD + b"yt-dlp 2026.09.30" + b"x" * 300_000  # big enough to arrive in several chunks


@pytest.fixture
def github() -> FakeGitHub:
    return FakeGitHub()


@pytest.fixture
def bin_dir(tmp_path: Path) -> Path:
    return tmp_path / "Snag" / "bin"


def listing(folder: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(folder.iterdir())}


def with_current(bin_dir: Path, files: dict[str, bytes]) -> dict[str, bytes]:
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (bin_dir / name).write_bytes(data)
    return listing(bin_dir)


class Calls:
    def __init__(self):
        self.progress: list[tuple[int, int | None]] = []
        self.phases: list[str] = []

    def kwargs(self):
        return dict(progress=lambda done, total: self.progress.append((done, total)), phase=self.phases.append,
                    smoke=fake_smoke)


# ----- the tool definitions follow spec 4.8 and the recorded releases -----

def test_sources_match_the_spec():
    assert (YTDLP.repo, YTDLP.asset, YTDLP.checksums) == ("yt-dlp/yt-dlp", "yt-dlp.exe", "SHA2-256SUMS")
    assert (FFMPEG.repo, FFMPEG.asset, FFMPEG.checksums) == (
        "yt-dlp/FFmpeg-Builds", "ffmpeg-master-latest-win64-gpl.zip", "checksums.sha256")
    assert dict(FFMPEG.members) == {"ffmpeg.exe": "*/bin/ffmpeg.exe", "ffprobe.exe": "*/bin/ffprobe.exe"}
    assert (DENO.repo, DENO.asset, DENO.checksums) == (
        "denoland/deno", "deno-x86_64-pc-windows-msvc.zip", "deno-x86_64-pc-windows-msvc.zip.sha256sum")
    assert dict(DENO.members) == {"deno.exe": "deno.exe"}


# ----- successful installs -----

async def test_install_ytdlp_fresh(github, bin_dir):
    publish_ytdlp(github, NEW_YTDLP)
    calls = Calls()
    async with github.client() as client:
        tag = await install(client, YTDLP, bin_dir, **calls.kwargs())
    assert tag == "2026.09.30"
    assert listing(bin_dir) == {"yt-dlp.exe": NEW_YTDLP}
    assert calls.progress[-1] == (len(NEW_YTDLP), len(NEW_YTDLP))
    assert len(calls.progress) > 1
    assert [done for done, _ in calls.progress] == sorted(done for done, _ in calls.progress)
    assert calls.phases == ["downloading", "verifying", "installing"]


async def test_install_ytdlp_replaces_the_current_one(github, bin_dir):
    with_current(bin_dir, {"yt-dlp.exe": GOOD + b"old", "ffmpeg.exe": b"untouched"})
    publish_ytdlp(github, NEW_YTDLP)
    async with github.client() as client:
        await install(client, YTDLP, bin_dir, smoke=fake_smoke)
    assert listing(bin_dir) == {"ffmpeg.exe": b"untouched", "yt-dlp.exe": NEW_YTDLP}


async def test_install_ffmpeg_takes_both_tools_from_the_zip(github, bin_dir):
    publish_ffmpeg(github, GOOD + b"new ffmpeg", GOOD + b"new ffprobe")
    calls = Calls()
    async with github.client() as client:
        tag = await install(client, FFMPEG, bin_dir, **calls.kwargs())
    assert tag == "latest"
    assert listing(bin_dir) == {"ffmpeg.exe": GOOD + b"new ffmpeg", "ffprobe.exe": GOOD + b"new ffprobe"}
    assert calls.phases == ["downloading", "verifying", "installing"]


async def test_install_deno_verifies_against_the_powershell_checksum(github, bin_dir):
    publish_deno(github, GOOD + b"deno 2.9.7")
    async with github.client() as client:
        assert await install(client, DENO, bin_dir, smoke=fake_smoke) == "v2.9.7"
    assert listing(bin_dir) == {"deno.exe": GOOD + b"deno 2.9.7"}


async def test_downloads_follow_the_redirect(github, bin_dir):
    publish_ytdlp(github, NEW_YTDLP)
    async with github.client() as client:
        await install(client, YTDLP, bin_dir, smoke=fake_smoke)
    assert github.downloads() == [
        "https://objects.example/yt-dlp/yt-dlp/releases/download/2026.09.30/SHA2-256SUMS",
        "https://objects.example/yt-dlp/yt-dlp/releases/download/2026.09.30/yt-dlp.exe",
    ]


# ----- failures leave the current tool in place and nothing else behind -----

async def test_checksum_mismatch(github, bin_dir):
    before = with_current(bin_dir, {"yt-dlp.exe": GOOD + b"old"})
    publish_ytdlp(github, NEW_YTDLP, listed_hash=sha256(b"something else"))
    async with github.client() as client:
        with pytest.raises(InstallError, match="^The yt-dlp download didn't match its published checksum"):
            await install(client, YTDLP, bin_dir, smoke=fake_smoke)
    assert listing(bin_dir) == before


async def test_zip_checksum_mismatch(github, bin_dir):
    publish_ffmpeg(github)
    github.files[f"/yt-dlp/FFmpeg-Builds/releases/download/latest/{FFMPEG_ZIP}"] = ffmpeg_zip(GOOD + b"x", GOOD + b"y")
    async with github.client() as client:
        with pytest.raises(InstallError, match="didn't match its published checksum"):
            await install(client, FFMPEG, bin_dir, smoke=fake_smoke)
    assert listing(bin_dir) == {}


async def test_missing_asset(github, bin_dir):
    publish_ytdlp(github, NEW_YTDLP)
    release = github.releases["yt-dlp/yt-dlp"]
    release["assets"] = [a for a in release["assets"] if a["name"] != "yt-dlp.exe"]
    async with github.client() as client:
        with pytest.raises(InstallError, match=r"^The latest yt-dlp release doesn't include yt-dlp.exe.$"):
            await install(client, YTDLP, bin_dir, smoke=fake_smoke)
    assert not bin_dir.exists() or listing(bin_dir) == {}


async def test_missing_checksum_file(github, bin_dir):
    publish_deno(github)
    release = github.releases["denoland/deno"]
    release["assets"] = [a for a in release["assets"] if not a["name"].endswith(".sha256sum")]
    async with github.client() as client:
        with pytest.raises(InstallError, match=f"doesn't include {DENO_ZIP}.sha256sum"):
            await install(client, DENO, bin_dir, smoke=fake_smoke)


async def test_checksum_file_without_the_asset(github, bin_dir):
    publish_ytdlp(github, NEW_YTDLP)
    github.files["/yt-dlp/yt-dlp/releases/download/2026.09.30/SHA2-256SUMS"] = f"{sha256(b'z')}  yt-dlp_x86.exe\n".encode()
    async with github.client() as client:
        with pytest.raises(InstallError, match="^The latest yt-dlp release doesn't list a checksum for yt-dlp.exe.$"):
            await install(client, YTDLP, bin_dir, smoke=fake_smoke)
    assert github.downloads()[-1].endswith("SHA2-256SUMS")  # the big file was never fetched


async def test_interrupted_download_leaves_no_partial(github, bin_dir):
    before = with_current(bin_dir, {"yt-dlp.exe": GOOD + b"old"})
    publish_ytdlp(github, NEW_YTDLP)
    github.drop_after["yt-dlp.exe"] = 100_000
    calls = Calls()
    async with github.client() as client:
        with pytest.raises(InstallError, match="^The yt-dlp download was interrupted"):
            await install(client, YTDLP, bin_dir, **calls.kwargs())
    assert listing(bin_dir) == before
    assert calls.progress[-1][1] == len(NEW_YTDLP)


async def test_server_closing_early_is_an_interruption(github, bin_dir):
    publish_ytdlp(github, NEW_YTDLP)
    github.cut_after["yt-dlp.exe"] = 100_000
    async with github.client() as client:
        with pytest.raises(InstallError, match="^The yt-dlp download was interrupted"):
            await install(client, YTDLP, bin_dir, smoke=fake_smoke)
    assert listing(bin_dir) == {}


async def test_download_error_status(github, bin_dir):
    publish_ytdlp(github, NEW_YTDLP)
    del github.files["/yt-dlp/yt-dlp/releases/download/2026.09.30/yt-dlp.exe"]
    async with github.client() as client:
        with pytest.raises(InstallError, match=r"^Couldn't download yt-dlp \(error 404\)"):
            await install(client, YTDLP, bin_dir, smoke=fake_smoke)
    assert listing(bin_dir) == {}


async def test_smoke_failure_keeps_the_current_tool(github, bin_dir):
    before = with_current(bin_dir, {"yt-dlp.exe": GOOD + b"old"})
    publish_ytdlp(github, b"BROKEN")
    async with github.client() as client:
        with pytest.raises(InstallError, match="^The new yt-dlp didn't run, so it wasn't installed"):
            await install(client, YTDLP, bin_dir, smoke=fake_smoke)
    assert listing(bin_dir) == before


async def test_one_broken_ffmpeg_tool_installs_neither(github, bin_dir):
    before = with_current(bin_dir, {"ffmpeg.exe": GOOD + b"old ffmpeg", "ffprobe.exe": GOOD + b"old ffprobe"})
    publish_ffmpeg(github, GOOD + b"new ffmpeg", b"BROKEN ffprobe")
    async with github.client() as client:
        with pytest.raises(InstallError, match="didn't run"):
            await install(client, FFMPEG, bin_dir, smoke=fake_smoke)
    assert listing(bin_dir) == before


async def test_zip_without_the_tool(github, bin_dir):
    publish_ffmpeg(github, archive=zip_of({"ffmpeg-master-latest-win64-gpl/bin/ffmpeg.exe": GOOD}))
    async with github.client() as client:
        with pytest.raises(InstallError, match="^The ffmpeg download doesn't contain ffprobe.exe"):
            await install(client, FFMPEG, bin_dir, smoke=fake_smoke)
    assert listing(bin_dir) == {}


async def test_not_a_zip(github, bin_dir):
    publish_ffmpeg(github, archive=b"not a zip at all")
    async with github.client() as client:
        with pytest.raises(InstallError, match="^The ffmpeg download couldn't be unpacked, so it wasn't installed.$"):
            await install(client, FFMPEG, bin_dir, smoke=fake_smoke)
    assert listing(bin_dir) == {}


@pytest.mark.parametrize("error", [
    zlib.error("Error -3 while decompressing data: invalid stored block lengths"),
    NotImplementedError("That compression method is not supported"),
    EOFError("Compressed file ended before the end-of-stream marker was reached"),
    RuntimeError("File is encrypted, password required for extraction"),
    ValueError("seek of closed file"),
], ids=lambda e: type(e).__name__)
async def test_a_damaged_zip_is_an_install_error(github, bin_dir, monkeypatch, error):
    before = with_current(bin_dir, {"ffmpeg.exe": GOOD + b"old ffmpeg", "ffprobe.exe": GOOD + b"old ffprobe"})
    publish_ffmpeg(github)

    def broken_open(self, *args, **kwargs):
        raise error

    monkeypatch.setattr(install_module.zipfile.ZipFile, "open", broken_open)
    async with github.client() as client:
        with pytest.raises(InstallError, match="^The ffmpeg download couldn't be unpacked, so it wasn't installed.$"):
            await install(client, FFMPEG, bin_dir, smoke=fake_smoke)
    assert listing(bin_dir) == before


async def test_a_zip_with_an_unknown_compression_method(github, bin_dir):
    archive = bytearray(ffmpeg_zip(GOOD + b"ffmpeg", GOOD + b"ffprobe"))
    # Mark every member as compressed with method 99 (AES), which zipfile can't read.
    for signature, offset in ((b"PK\x03\x04", 8), (b"PK\x01\x02", 10)):
        start = 0
        while (start := archive.find(signature, start)) != -1:
            archive[start + offset:start + offset + 2] = (99).to_bytes(2, "little")
            start += 4
    publish_ffmpeg(github, archive=bytes(archive))
    async with github.client() as client:
        with pytest.raises(InstallError, match="couldn't be unpacked"):
            await install(client, FFMPEG, bin_dir, smoke=fake_smoke)
    assert listing(bin_dir) == {}


@pytest.mark.parametrize("url", [
    "https://evil.example/yt-dlp/yt-dlp/releases/download/2026.09.30/yt-dlp.exe",
    "http://github.com/yt-dlp/yt-dlp/releases/download/2026.09.30/yt-dlp.exe",
    "https://github.com/someone-else/yt-dlp/releases/download/2026.09.30/yt-dlp.exe",
    "https://github.com/yt-dlp/yt-dlp/archive/2026.09.30/yt-dlp.exe",
    "https://github.com:8443/yt-dlp/yt-dlp/releases/download/2026.09.30/yt-dlp.exe",
    "https://github.com@evil.example/yt-dlp/yt-dlp/releases/download/2026.09.30/yt-dlp.exe",
    "https://github.com/yt-dlp/yt-dlp/releases/download/../../../evil/yt-dlp.exe",
    "not a url at all",
])
async def test_download_urls_must_be_this_repos_releases(github, bin_dir, url):
    publish_ytdlp(github, NEW_YTDLP)
    asset = next(a for a in github.releases["yt-dlp/yt-dlp"]["assets"] if a["name"] == "yt-dlp.exe")
    asset["browser_download_url"] = url
    async with github.client() as client:
        with pytest.raises(InstallError, match="^The latest yt-dlp release points somewhere other than its GitHub releases"):
            await install(client, YTDLP, bin_dir, smoke=fake_smoke)
    assert github.downloads() == []
    assert not bin_dir.exists() or listing(bin_dir) == {}


async def test_a_redirect_to_plain_http_is_refused(github, bin_dir):
    before = with_current(bin_dir, {"yt-dlp.exe": GOOD + b"old"})
    publish_ytdlp(github, NEW_YTDLP)
    github.download_origin = "http://objects.example"
    async with github.client() as client:
        with pytest.raises(InstallError, match="^The yt-dlp download wasn't sent securely"):
            await install(client, YTDLP, bin_dir, smoke=fake_smoke)
    assert listing(bin_dir) == before


async def test_a_redirect_to_plain_http_is_refused_for_the_download_itself(github, bin_dir, monkeypatch):
    before = with_current(bin_dir, {"yt-dlp.exe": GOOD + b"old"})
    publish_ytdlp(github, NEW_YTDLP)
    real_handle = github._handle

    def handle(request):  # only the big file goes to http://
        if request.url.host == "github.com" and request.url.path.endswith("/yt-dlp.exe"):
            return httpx.Response(302, headers={"Location": f"http://objects.example{request.url.path}"})
        return real_handle(request)

    monkeypatch.setattr(github, "_handle", handle)
    async with github.client() as client:
        with pytest.raises(InstallError, match="^The yt-dlp download wasn't sent securely"):
            await install(client, YTDLP, bin_dir, smoke=fake_smoke)
    assert listing(bin_dir) == before


@pytest.mark.parametrize("asset", ["yt-dlp.exe", "SHA2-256SUMS"])
async def test_a_plain_http_hop_is_refused_even_when_the_last_one_is_https(github, bin_dir, monkeypatch, asset):
    """github.com -> http://objects.example -> https://objects.example: the file arrives over https,
    but the middle hop went in the clear (and could have been sent anywhere), so it is refused."""
    before = with_current(bin_dir, {"yt-dlp.exe": GOOD + b"old"})
    publish_ytdlp(github, NEW_YTDLP)
    real_handle = github._handle

    def handle(request):
        if request.url.host == "github.com" and request.url.path.endswith(f"/{asset}"):
            return httpx.Response(302, headers={"Location": f"http://objects.example{request.url.path}"})
        if request.url.scheme == "http":
            github.requests.append(str(request.url))
            return httpx.Response(302, headers={"Location": f"https://objects.example{request.url.path}"})
        return real_handle(request)

    monkeypatch.setattr(github, "_handle", handle)
    async with github.client() as client:
        with pytest.raises(InstallError, match="^The yt-dlp download wasn't sent securely"):
            await install(client, YTDLP, bin_dir, smoke=fake_smoke)
    assert any(url.startswith("http://objects.example") for url in github.requests)  # the hop happened
    assert listing(bin_dir) == before


async def test_api_digest_that_disagrees_with_the_checksum_file(github, bin_dir):
    before = with_current(bin_dir, {"yt-dlp.exe": GOOD + b"old"})
    publish_ytdlp(github, NEW_YTDLP)
    asset = next(a for a in github.releases["yt-dlp/yt-dlp"]["assets"] if a["name"] == "yt-dlp.exe")
    asset["digest"] = f"sha256:{sha256(b'something else')}"
    async with github.client() as client:
        with pytest.raises(InstallError, match="^The yt-dlp download didn't match its published checksum"):
            await install(client, YTDLP, bin_dir, smoke=fake_smoke)
    assert not any(url.endswith("/yt-dlp.exe") for url in github.downloads())  # refused before downloading
    assert listing(bin_dir) == before


@pytest.mark.parametrize("digest", [None, "sha512:" + "ab" * 64, "garbage"])
async def test_api_digest_absent_or_unknown_is_not_needed(github, bin_dir, digest):
    publish_ytdlp(github, NEW_YTDLP)
    for asset in github.releases["yt-dlp/yt-dlp"]["assets"]:
        asset["digest"] = digest
    async with github.client() as client:
        await install(client, YTDLP, bin_dir, smoke=fake_smoke)
    assert listing(bin_dir) == {"yt-dlp.exe": NEW_YTDLP}


async def test_api_digest_in_upper_case_matches(github, bin_dir):
    publish_ytdlp(github, NEW_YTDLP)
    for asset in github.releases["yt-dlp/yt-dlp"]["assets"]:
        asset["digest"] = asset["digest"].upper().replace("SHA256:", "sha256:")
    async with github.client() as client:
        await install(client, YTDLP, bin_dir, smoke=fake_smoke)
    assert listing(bin_dir) == {"yt-dlp.exe": NEW_YTDLP}


async def test_checksum_file_not_found(github, bin_dir):
    before = with_current(bin_dir, {"yt-dlp.exe": GOOD + b"old"})
    publish_ytdlp(github, NEW_YTDLP)
    del github.files["/yt-dlp/yt-dlp/releases/download/2026.09.30/SHA2-256SUMS"]
    async with github.client() as client:
        with pytest.raises(InstallError, match=r"^Couldn't download yt-dlp \(error 404\)"):
            await install(client, YTDLP, bin_dir, smoke=fake_smoke)
    assert not any(url.endswith("/yt-dlp.exe") for url in github.downloads())
    assert listing(bin_dir) == before


async def test_an_old_file_that_cant_be_deleted_is_cleaned_at_the_next_start(github, bin_dir, monkeypatch):
    with_current(bin_dir, {"yt-dlp.exe": GOOD + b"old"})
    publish_ytdlp(github, NEW_YTDLP)
    real_unlink = Path.unlink

    def unlink(self, missing_ok=False):
        if self.name == "yt-dlp.old.exe" and self.exists():
            raise PermissionError(13, "Access is denied")  # the old yt-dlp is still running
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    async with github.client() as client:
        await install(client, YTDLP, bin_dir, smoke=fake_smoke)
    assert listing(bin_dir) == {"yt-dlp.exe": NEW_YTDLP, "yt-dlp.old.exe": GOOD + b"old"}
    monkeypatch.setattr(Path, "unlink", real_unlink)

    clean_leftovers(bin_dir)

    assert listing(bin_dir) == {"yt-dlp.exe": NEW_YTDLP}


async def test_swap_failure_rolls_back_every_file(github, bin_dir, monkeypatch):
    before = with_current(bin_dir, {"ffmpeg.exe": GOOD + b"old ffmpeg", "ffprobe.exe": GOOD + b"old ffprobe"})
    publish_ffmpeg(github, GOOD + b"new ffmpeg", GOOD + b"new ffprobe")
    real_replace = os.replace

    def failing_replace(source, target):
        if Path(source).name == "ffprobe.new.exe":
            raise PermissionError(13, "Access is denied")
        return real_replace(source, target)

    monkeypatch.setattr(install_module.os, "replace", failing_replace)
    async with github.client() as client:
        with pytest.raises(InstallError, match="^Couldn't replace ffmpeg: Access is denied"):
            await install(client, FFMPEG, bin_dir, smoke=fake_smoke)
    assert listing(bin_dir) == before


async def test_swap_failure_on_a_first_install_leaves_nothing(github, bin_dir, monkeypatch):
    publish_ffmpeg(github)
    real_replace = os.replace

    def failing_replace(source, target):
        if Path(source).name == "ffprobe.new.exe":
            raise PermissionError(13, "Access is denied")
        return real_replace(source, target)

    monkeypatch.setattr(install_module.os, "replace", failing_replace)
    async with github.client() as client:
        with pytest.raises(InstallError):
            await install(client, FFMPEG, bin_dir, smoke=fake_smoke)
    assert listing(bin_dir) == {}


def locked(winerror: int = 32) -> PermissionError:
    """What Windows raises when antivirus or the indexer has the file open for a moment."""
    return PermissionError(13, "The process cannot access the file", None, winerror)


class FlakyReplace:
    """os.replace that fails `failures` times (or forever, with None) when moving `name`."""

    def __init__(self, name: str, failures: int | None, error=locked):
        self.name, self.failures, self.error = name, failures, error
        self.calls: list[tuple[str, str]] = []
        self.real = os.replace

    def __call__(self, source, target):
        self.calls.append((Path(source).name, Path(target).name))
        if Path(source).name == self.name and (self.failures is None or self.failures > 0):
            if self.failures is not None:
                self.failures -= 1
            raise self.error()
        return self.real(source, target)

    def attempts(self, name: str) -> int:
        return sum(source == name for source, _ in self.calls)


@pytest.fixture
def no_retry_wait(monkeypatch):
    monkeypatch.setattr(install_module, "SWAP_RETRY_DELAYS", (0.0,) * len(install_module.SWAP_RETRY_DELAYS))


def test_swap_retries_wait_about_three_seconds_in_all():
    assert 2.5 <= sum(install_module.SWAP_RETRY_DELAYS) <= 3.5


@pytest.mark.parametrize("winerror", [5, 32])
async def test_swap_retries_a_briefly_locked_file(github, bin_dir, monkeypatch, no_retry_wait, winerror):
    with_current(bin_dir, {"yt-dlp.exe": GOOD + b"old"})
    publish_ytdlp(github, NEW_YTDLP)
    flaky = FlakyReplace("yt-dlp.new.exe", failures=2, error=lambda: locked(winerror))
    monkeypatch.setattr(install_module.os, "replace", flaky)
    async with github.client() as client:
        await install(client, YTDLP, bin_dir, smoke=fake_smoke)
    assert flaky.attempts("yt-dlp.new.exe") == 3
    assert listing(bin_dir) == {"yt-dlp.exe": NEW_YTDLP}


async def test_swap_retries_moving_the_current_file_aside(github, bin_dir, monkeypatch, no_retry_wait):
    with_current(bin_dir, {"yt-dlp.exe": GOOD + b"old"})
    publish_ytdlp(github, NEW_YTDLP)
    flaky = FlakyReplace("yt-dlp.exe", failures=2)
    monkeypatch.setattr(install_module.os, "replace", flaky)
    async with github.client() as client:
        await install(client, YTDLP, bin_dir, smoke=fake_smoke)
    assert listing(bin_dir) == {"yt-dlp.exe": NEW_YTDLP}


async def test_swap_gives_up_on_a_file_that_stays_locked_and_rolls_back(github, bin_dir, monkeypatch, no_retry_wait):
    before = with_current(bin_dir, {"ffmpeg.exe": GOOD + b"old ffmpeg", "ffprobe.exe": GOOD + b"old ffprobe"})
    publish_ffmpeg(github, GOOD + b"new ffmpeg", GOOD + b"new ffprobe")
    flaky = FlakyReplace("ffprobe.new.exe", failures=None)
    monkeypatch.setattr(install_module.os, "replace", flaky)
    async with github.client() as client:
        with pytest.raises(InstallError, match="^Couldn't replace ffmpeg: The process cannot access the file"):
            await install(client, FFMPEG, bin_dir, smoke=fake_smoke)
    assert flaky.attempts("ffprobe.new.exe") == len(install_module.SWAP_RETRY_DELAYS) + 1
    assert ("ffmpeg.old.exe", "ffmpeg.exe") in flaky.calls and ("ffprobe.old.exe", "ffprobe.exe") in flaky.calls
    assert listing(bin_dir) == before


async def test_swap_does_not_retry_other_errors(github, bin_dir, monkeypatch, no_retry_wait):
    before = with_current(bin_dir, {"yt-dlp.exe": GOOD + b"old"})
    publish_ytdlp(github, NEW_YTDLP)
    flaky = FlakyReplace("yt-dlp.new.exe", failures=None, error=lambda: PermissionError(13, "Access is denied"))
    monkeypatch.setattr(install_module.os, "replace", flaky)
    async with github.client() as client:
        with pytest.raises(InstallError):
            await install(client, YTDLP, bin_dir, smoke=fake_smoke)
    assert flaky.attempts("yt-dlp.new.exe") == 1
    assert listing(bin_dir) == before


async def test_rollback_retries_a_briefly_locked_file(github, bin_dir, monkeypatch, no_retry_wait):
    before = with_current(bin_dir, {"yt-dlp.exe": GOOD + b"old"})
    publish_ytdlp(github, NEW_YTDLP)
    stuck = FlakyReplace("yt-dlp.new.exe", failures=None, error=lambda: PermissionError(13, "Access is denied"))
    flaky_back = FlakyReplace("yt-dlp.old.exe", failures=2)
    monkeypatch.setattr(install_module.os, "replace", lambda s, t: (stuck if Path(s).name != "yt-dlp.old.exe" else flaky_back)(s, t))
    async with github.client() as client:
        with pytest.raises(InstallError):
            await install(client, YTDLP, bin_dir, smoke=fake_smoke)
    assert flaky_back.attempts("yt-dlp.old.exe") == 3
    assert listing(bin_dir) == before


async def test_swap_does_not_block_the_event_loop(github, bin_dir, monkeypatch):
    with_current(bin_dir, {"yt-dlp.exe": GOOD + b"old"})
    publish_ytdlp(github, NEW_YTDLP)
    monkeypatch.setattr(install_module, "SWAP_RETRY_DELAYS", (0.2, 0.2))
    monkeypatch.setattr(install_module.os, "replace", FlakyReplace("yt-dlp.new.exe", failures=2))
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    task = asyncio.create_task(ticker())
    try:
        async with github.client() as client:
            await install(client, YTDLP, bin_dir, smoke=fake_smoke)
    finally:
        task.cancel()
    assert ticks >= 10  # the loop kept running while the swap waited 0.4 s for the lock


async def test_rate_limit_is_a_release_error(github, bin_dir):
    github.rate_limited_until = 2_000_000_000
    async with github.client() as client:
        with pytest.raises(ReleaseError, match="GitHub is limiting"):
            await install(client, YTDLP, bin_dir, smoke=fake_smoke)


async def test_cancelled_install_leaves_nothing_behind(github, bin_dir):
    publish_ffmpeg(github)
    before = with_current(bin_dir, {"ffmpeg.exe": GOOD + b"old", "ffprobe.exe": GOOD + b"old probe"})

    async def cancelled_smoke(exe):
        raise asyncio.CancelledError()

    async with github.client() as client:
        with pytest.raises(asyncio.CancelledError):
            await install(client, FFMPEG, bin_dir, smoke=cancelled_smoke)
    assert listing(bin_dir) == before


# ----- staging (prepare) and swapping later, as an update does -----

async def test_prepare_stages_without_touching_the_current_tool(github, bin_dir):
    before = with_current(bin_dir, {"yt-dlp.exe": GOOD + b"old"})
    publish_ytdlp(github, NEW_YTDLP)
    async with github.client() as client:
        staged = await prepare(client, YTDLP, bin_dir, smoke=fake_smoke)
    assert staged.tag == "2026.09.30"
    assert (bin_dir / "yt-dlp.exe").read_bytes() == before["yt-dlp.exe"]
    assert (bin_dir / "yt-dlp.new.exe").read_bytes() == NEW_YTDLP
    await staged.swap()
    assert listing(bin_dir) == {"yt-dlp.exe": NEW_YTDLP}
    staged.discard()  # nothing left to discard
    assert listing(bin_dir) == {"yt-dlp.exe": NEW_YTDLP}


async def test_discard_removes_the_staged_files(github, bin_dir):
    before = with_current(bin_dir, {"yt-dlp.exe": GOOD + b"old"})
    publish_ytdlp(github, NEW_YTDLP)
    async with github.client() as client:
        staged = await prepare(client, YTDLP, bin_dir, smoke=fake_smoke)
    staged.discard()
    assert listing(bin_dir) == before


async def test_prepare_with_a_known_release_skips_the_lookup(github, bin_dir):
    from snag.updater.releases import latest_release

    publish_ytdlp(github, NEW_YTDLP)
    async with github.client() as client:
        release = await latest_release(client, "yt-dlp/yt-dlp")
        github.requests.clear()
        staged = await prepare(client, YTDLP, bin_dir, release=release, smoke=fake_smoke)
    staged.discard()
    assert not any("api.github.com" in url for url in github.requests)


# ----- leftovers of a crash mid-install -----

def test_clean_leftovers(bin_dir):
    bin_dir.mkdir(parents=True)
    (bin_dir / "yt-dlp.old.exe").write_bytes(b"old yt-dlp")  # current missing: crashed mid-swap
    (bin_dir / "ffmpeg.exe").write_bytes(b"ffmpeg")
    (bin_dir / "ffmpeg.old.exe").write_bytes(b"older ffmpeg")  # swap finished, delete failed
    (bin_dir / "ffprobe.new.exe").write_bytes(b"staged")
    (bin_dir / f"{FFMPEG_ZIP}.part").write_bytes(b"partial")
    (bin_dir / "unrelated.txt").write_bytes(b"keep")
    clean_leftovers(bin_dir)
    assert listing(bin_dir) == {"ffmpeg.exe": b"ffmpeg", "unrelated.txt": b"keep", "yt-dlp.exe": b"old yt-dlp"}


def test_clean_leftovers_after_a_crash_halfway_through_swapping_a_pair(bin_dir):
    """ffmpeg was swapped in, ffprobe wasn't yet: the new ffmpeg stays, the old ffprobe comes back. Both
    are standalone builds that work apart; the next reinstall makes them a pair again."""
    bin_dir.mkdir(parents=True)
    (bin_dir / "ffmpeg.exe").write_bytes(b"new ffmpeg")
    (bin_dir / "ffmpeg.old.exe").write_bytes(b"old ffmpeg")
    (bin_dir / "ffprobe.old.exe").write_bytes(b"old ffprobe")
    (bin_dir / "ffprobe.new.exe").write_bytes(b"new ffprobe")
    clean_leftovers(bin_dir)
    assert listing(bin_dir) == {"ffmpeg.exe": b"new ffmpeg", "ffprobe.exe": b"old ffprobe"}


def test_clean_leftovers_without_a_bin_folder(tmp_path):
    clean_leftovers(tmp_path / "missing")


# ----- the default smoke check runs the real file -----

async def test_default_smoke_runs_an_executable():
    assert await default_smoke(Path(sys.executable))  # python --version succeeds


async def test_default_smoke_rejects_a_non_executable(tmp_path):
    fake = tmp_path / "yt-dlp.new.exe"
    fake.write_bytes(b"this is not a program")
    assert not await default_smoke(fake)


async def test_default_smoke_rejects_a_missing_file(tmp_path):
    assert not await default_smoke(tmp_path / "deno.new.exe")


def test_default_smoke_flag_per_tool():
    assert install_module.version_args(Path("ffmpeg.new.exe")) == ["-version"]
    assert install_module.version_args(Path("ffprobe.exe")) == ["-version"]
    assert install_module.version_args(Path("yt-dlp.new.exe")) == ["--version"]
    assert install_module.version_args(Path("deno.exe")) == ["--version"]


def test_errors_are_updater_errors():
    assert issubclass(InstallError, UpdaterError)
