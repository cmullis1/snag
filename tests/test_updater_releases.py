import time

import httpx
import pytest

from snag.updater.releases import Release, ReleaseError, UpdaterError, latest_release, parse_checksums
from tests.updater_env import FakeGitHub
from tests.updater_fixtures import (
    DENO_RELEASE,
    DENO_ZIP_SHA256SUM,
    FFMPEG_CHECKSUMS_SHA256,
    FFMPEG_RELEASE,
    YTDLP_RELEASE,
    YTDLP_SHA2_256SUMS,
)


def digest(release: dict, name: str) -> str:
    asset = next(a for a in release["assets"] if a["name"] == name)
    return asset["digest"].removeprefix("sha256:")


# ----- parse_checksums, against the recorded files -----

def test_ytdlp_sums_two_space_format():
    sums = parse_checksums(YTDLP_SHA2_256SUMS)
    assert len(sums) == 19
    assert sums["yt-dlp.exe"] == digest(YTDLP_RELEASE, "yt-dlp.exe")
    assert sums["yt-dlp_x86.exe"] == digest(YTDLP_RELEASE, "yt-dlp_x86.exe")


def test_ffmpeg_sums_two_space_format():
    sums = parse_checksums(FFMPEG_CHECKSUMS_SHA256)
    name = "ffmpeg-master-latest-win64-gpl.zip"
    assert sums[name] == digest(FFMPEG_RELEASE, name)
    assert "ffmpeg-master-latest-win64-gpl-shared.zip" in sums


def test_deno_powershell_format_is_lowercased_and_named_by_its_path():
    name = "deno-x86_64-pc-windows-msvc.zip"
    assert parse_checksums(DENO_ZIP_SHA256SUM) == {name: digest(DENO_RELEASE, name)}


def test_binary_marker_format():
    hash_ = "ab" * 32
    assert parse_checksums(f"{hash_} *yt-dlp.exe\n") == {"yt-dlp.exe": hash_}


def test_bare_hash_is_filed_under_the_given_name():
    hash_ = "CD" * 32
    assert parse_checksums(f"{hash_}\n", single_file="deno.zip") == {"deno.zip": hash_.lower()}
    assert parse_checksums(f"{hash_}\n") == {}


def test_powershell_format_without_a_path_uses_the_given_name():
    text = f"Algorithm : SHA256\r\nHash      : {'EF' * 32}\r\n"
    assert parse_checksums(text, single_file="x.zip") == {"x.zip": "ef" * 32}


def test_other_algorithms_and_junk_are_ignored():
    text = (
        "Algorithm : SHA512\r\nHash      : " + "AA" * 64 + "\r\nPath : C:\\x\\a.zip\r\n"
        "not a checksum line\n"
        "1234  short.exe\n"
        "\n"
    )
    assert parse_checksums(text, single_file="a.zip") == {}


def test_names_with_spaces_and_crlf():
    hash_ = "01" * 32
    assert parse_checksums(f"{hash_}  my file.exe\r\n") == {"my file.exe": hash_}


# ----- latest_release -----

async def test_latest_release_reads_tag_and_assets():
    github = FakeGitHub()
    github.releases["yt-dlp/yt-dlp"] = YTDLP_RELEASE
    async with github.client() as client:
        release = await latest_release(client, "yt-dlp/yt-dlp")
    assert release == Release(
        tag="2026.08.19",
        assets={a["name"]: a["browser_download_url"] for a in YTDLP_RELEASE["assets"]},
        digests={a["name"]: digest(YTDLP_RELEASE, a["name"]) for a in YTDLP_RELEASE["assets"]},
    )
    assert github.requests == ["https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest"]


@pytest.mark.parametrize(("value", "kept"), [
    ("sha256:" + "AB" * 32, "ab" * 32),
    (None, None),
    ("sha512:" + "ab" * 64, None),
    ("sha256:not-hex", None),
    (17, None),
])
async def test_asset_digests_keep_only_sha256(value, kept):
    release = {"tag_name": "v1", "assets": [{"name": "a.zip", "browser_download_url": "https://x/a.zip", "digest": value}]}
    github = FakeGitHub()
    github.releases["o/r"] = release
    async with github.client() as client:
        result = await latest_release(client, "o/r")
    assert result.digests == ({} if kept is None else {"a.zip": kept})


async def test_asset_without_a_digest_field():
    github = FakeGitHub()
    github.releases["o/r"] = {"tag_name": "v1", "assets": [{"name": "a.zip", "browser_download_url": "https://x/a.zip"}]}
    async with github.client() as client:
        assert (await latest_release(client, "o/r")).digests == {}


async def test_rate_limit_gives_a_plain_message_with_the_wait():
    github = FakeGitHub()
    github.rate_limited_until = int(time.time()) + 600
    async with github.client() as client:
        with pytest.raises(ReleaseError) as caught:
            await latest_release(client, "yt-dlp/yt-dlp")
    assert str(caught.value) == "GitHub is limiting update checks right now. Try again in about 10 minutes."
    assert isinstance(caught.value, UpdaterError)


async def test_rate_limit_without_reset_time():
    def handler(request):
        return httpx.Response(429, json={"message": "You have exceeded a secondary rate limit."})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ReleaseError, match=r"^GitHub is limiting update checks right now. Try again later.$"):
            await latest_release(client, "yt-dlp/yt-dlp")


async def test_missing_repo():
    async with FakeGitHub().client() as client:
        with pytest.raises(ReleaseError, match="Couldn't find the latest release of nobody/nothing"):
            await latest_release(client, "nobody/nothing")


async def test_server_error():
    github = FakeGitHub()
    github.api_status = 502
    async with github.client() as client:
        with pytest.raises(ReleaseError, match=r"GitHub had a problem \(error 502\)"):
            await latest_release(client, "yt-dlp/yt-dlp")


async def test_unreachable():
    def handler(request):
        raise httpx.ConnectError("getaddrinfo failed")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ReleaseError, match="Couldn't reach GitHub. Check your internet connection."):
            await latest_release(client, "yt-dlp/yt-dlp")


@pytest.mark.parametrize("body", [b"<html>", b"[]", b'{"assets": []}', b'{"tag_name": "x", "assets": [{"name": 1}]}'])
async def test_unexpected_reply(body):
    def handler(request):
        return httpx.Response(200, content=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ReleaseError, match="GitHub sent an unexpected reply"):
            await latest_release(client, "yt-dlp/yt-dlp")


async def test_request_has_timeout_and_api_headers():
    seen = {}

    def handler(request):
        seen.update(request.headers)
        seen["timeout"] = request.extensions["timeout"]
        return httpx.Response(200, json=DENO_RELEASE)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=None) as client:
        await latest_release(client, "denoland/deno")
    assert seen["user-agent"].startswith("Snag")
    assert seen["accept"] == "application/vnd.github+json"
    assert all(value is not None for value in seen["timeout"].values())
