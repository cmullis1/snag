"""A fake GitHub for the updater tests: the releases API plus release downloads (which, like the real
ones, redirect to another host), served through httpx.MockTransport. Releases are built in the shapes
recorded in tests/updater_fixtures.py, with the test's own files in place of the real ones."""
from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path

import httpx

from tests.updater_fixtures import FFMPEG_ZIP_MEMBERS, RATE_LIMITED_BODY

API = "https://api.github.com"
FFMPEG_ZIP = "ffmpeg-master-latest-win64-gpl.zip"
DENO_ZIP = "deno-x86_64-pc-windows-msvc.zip"

# What the tests' smoke check accepts: a file starting with GOOD "runs", anything else doesn't.
GOOD = b"GOOD-EXE"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def fake_smoke(exe: Path) -> bool:
    return exe.read_bytes().startswith(GOOD)


class ChunkedStream(httpx.AsyncByteStream):
    """Sends a body in 64 KiB pieces, as a network does; with `keep`, sends that many bytes and then
    fails as a dropped connection does, or with `quietly`, just stops (a server closing early)."""

    def __init__(self, data: bytes, keep: int | None = None, quietly: bool = False):
        self.data, self.keep, self.quietly = data, keep, quietly

    async def __aiter__(self):
        end = len(self.data) if self.keep is None else self.keep
        for start in range(0, end, 65536):
            yield self.data[start:min(start + 65536, end)]
        if self.keep is not None and not self.quietly:
            raise httpx.ReadError("connection reset by peer")


class FakeGitHub:
    def __init__(self):
        self.releases: dict[str, dict] = {}
        self.files: dict[str, bytes] = {}  # path on the download host -> body
        self.drop_after: dict[str, int] = {}  # asset name -> bytes sent before the connection drops
        self.cut_after: dict[str, int] = {}  # asset name -> bytes sent before the server just stops
        self.rate_limited_until: int | None = None  # epoch seconds; set to answer every API call with 403
        self.api_status: int | None = None  # set to answer every API call with this status
        self.download_origin = "https://objects.example"  # where release downloads redirect to
        self.requests: list[str] = []

    def add_release(self, repo: str, tag: str, files: dict[str, bytes]) -> None:
        assets = []
        for name, data in files.items():
            path = f"/{repo}/releases/download/{tag}/{name}"
            self.files[path] = data
            assets.append({
                "name": name, "size": len(data), "digest": f"sha256:{sha256(data)}",
                "browser_download_url": f"https://github.com{path}",
            })
        self.releases[repo] = {"tag_name": tag, "name": tag, "assets": assets}

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self._handle), follow_redirects=True)

    def downloads(self) -> list[str]:
        return [url for url in self.requests if "objects.example" in url]

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        host, path = request.url.host, request.url.path
        if host == "api.github.com":
            assert request.headers.get("user-agent", "").startswith("Snag"), "GitHub refuses requests without a User-Agent"
            if self.rate_limited_until is not None:
                return httpx.Response(
                    403, json=RATE_LIMITED_BODY,
                    headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(self.rate_limited_until)},
                )
            if self.api_status is not None:
                return httpx.Response(self.api_status, json={"message": "Server Error"})
            repo = path.removeprefix("/repos/").removesuffix("/releases/latest")
            if repo in self.releases:
                return httpx.Response(200, json=self.releases[repo])
            return httpx.Response(404, json={"message": "Not Found"})
        if host == "github.com":
            # Release downloads redirect to a storage host, as the real ones do.
            return httpx.Response(302, headers={"Location": f"{self.download_origin}{path}"})
        if host == "objects.example" and path in self.files:
            data = self.files[path]
            name = path.rsplit("/", 1)[-1]
            cut = self.cut_after.get(name)
            stream = ChunkedStream(data, cut, quietly=True) if cut is not None else ChunkedStream(data, self.drop_after.get(name))
            return httpx.Response(
                200, headers={"Content-Length": str(len(data)), "Content-Type": "application/octet-stream"},
                stream=stream,
            )
        return httpx.Response(404)


def zip_of(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def ffmpeg_zip(ffmpeg: bytes, ffprobe: bytes) -> bytes:
    """The real zip's layout (FFMPEG_ZIP_MEMBERS) with the given executables."""
    contents = {"ffmpeg.exe": ffmpeg, "ffprobe.exe": ffprobe, "ffplay.exe": GOOD + b"ffplay"}
    members = {}
    for name in FFMPEG_ZIP_MEMBERS:
        if name.endswith("/"):
            members[name] = b""
        else:
            members[name] = contents.get(name.rsplit("/", 1)[-1], b"text")
    return zip_of(members)


def publish_ytdlp(github: FakeGitHub, exe: bytes, tag: str = "2026.09.30", *, listed_hash: str | None = None) -> None:
    sums = (
        f"{sha256(b'linux build')}  yt-dlp\n"
        f"{listed_hash or sha256(exe)}  yt-dlp.exe\n"
        f"{sha256(b'x86 build')}  yt-dlp_x86.exe\n"
    )
    github.add_release("yt-dlp/yt-dlp", tag, {"SHA2-256SUMS": sums.encode(), "yt-dlp.exe": exe})


def publish_ffmpeg(github: FakeGitHub, ffmpeg: bytes = GOOD + b"ffmpeg", ffprobe: bytes = GOOD + b"ffprobe",
                   *, archive: bytes | None = None) -> None:
    archive = archive if archive is not None else ffmpeg_zip(ffmpeg, ffprobe)
    sums = f"{sha256(b'shared')}  ffmpeg-master-latest-win64-gpl-shared.zip\n{sha256(archive)}  {FFMPEG_ZIP}\n"
    github.add_release("yt-dlp/FFmpeg-Builds", "latest", {"checksums.sha256": sums.encode(), FFMPEG_ZIP: archive})


def publish_deno(github: FakeGitHub, deno: bytes = GOOD + b"deno", tag: str = "v2.9.7") -> None:
    archive = zip_of({"deno.exe": deno})
    # PowerShell's Get-FileHash | Format-List, as Deno publishes it (see DENO_ZIP_SHA256SUM).
    sums = (
        "\r\nAlgorithm : SHA256\r\n"
        f"Hash      : {sha256(archive).upper()}\r\n"
        f"Path      : C:\\a\\deno\\deno\\target\\release\\{DENO_ZIP}\r\n\r\n"
    )
    github.add_release("denoland/deno", tag, {f"{DENO_ZIP}.sha256sum": sums.encode(), DENO_ZIP: archive})


def publish_all(github: FakeGitHub, ytdlp: bytes = GOOD + b"yt-dlp", tag: str = "2026.09.30") -> None:
    publish_ytdlp(github, ytdlp, tag)
    publish_ffmpeg(github)
    publish_deno(github)

