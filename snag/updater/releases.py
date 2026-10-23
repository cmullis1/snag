"""GitHub "latest release" lookups and the checksum files published with them."""
from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from pathlib import PureWindowsPath

import httpx

API = "https://api.github.com"
USER_AGENT = "Snag (personal yt-dlp front end)"
# GitHub refuses API requests without a User-Agent; the Accept header pins the API's JSON format.
API_HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
API_TIMEOUT = httpx.Timeout(20.0)


class UpdaterError(Exception):
    """An updater step failed; the message is plain English, ready to show."""


class ReleaseError(UpdaterError):
    """GitHub couldn't tell which release is the latest."""


@dataclass(frozen=True)
class Release:
    tag: str
    assets: dict[str, str]  # asset name -> download URL
    # asset name -> lower-case SHA-256, from the API's "digest" ("sha256:<hex>"); assets without one
    # (older uploads have none) or with another algorithm's are left out
    digests: dict[str, str] = field(default_factory=dict)


async def latest_release(client: httpx.AsyncClient, repo: str) -> Release:
    """The latest release of repo ("owner/name"). Raises ReleaseError with a plain message."""
    try:
        response = await client.get(f"{API}/repos/{repo}/releases/latest", headers=API_HEADERS, timeout=API_TIMEOUT)
    except httpx.HTTPError as exc:
        raise ReleaseError("Couldn't reach GitHub. Check your internet connection.") from exc
    if response.status_code in (403, 429) and _rate_limited(response):
        raise ReleaseError(_rate_limit_message(response))
    if response.status_code == 404:
        raise ReleaseError(f"Couldn't find the latest release of {repo}.")
    if response.status_code != 200:
        raise ReleaseError(f"GitHub had a problem (error {response.status_code}). Try again later.")
    try:
        return _parse_release(response.json())
    except (ValueError, TypeError, KeyError) as exc:
        raise ReleaseError("GitHub sent an unexpected reply. Try again later.") from exc


def _parse_release(data: object) -> Release:
    if not isinstance(data, dict):
        raise TypeError("release is not an object")
    tag = data["tag_name"]
    if not isinstance(tag, str) or not tag:
        raise ValueError("no tag")
    assets, digests = {}, {}
    for asset in data["assets"]:
        name, url = asset["name"], asset["browser_download_url"]
        if not isinstance(name, str) or not isinstance(url, str):
            raise TypeError("asset name or URL is not text")
        assets[name] = url
        digest = asset.get("digest")
        if isinstance(digest, str) and digest.lower().startswith("sha256:") and _SHA256.match(digest[7:]):
            digests[name] = digest[7:].lower()
    return Release(tag=tag, assets=assets, digests=digests)


def _rate_limited(response: httpx.Response) -> bool:
    if response.headers.get("x-ratelimit-remaining") == "0" or response.status_code == 429:
        return True
    try:
        message = response.json().get("message", "")
    except (ValueError, AttributeError):
        return False
    return isinstance(message, str) and "rate limit" in message.lower()


def _rate_limit_message(response: httpx.Response) -> str:
    base = "GitHub is limiting update checks right now."
    try:
        reset = int(response.headers["x-ratelimit-reset"])
    except (KeyError, ValueError):
        return f"{base} Try again later."
    minutes = max(1, math.ceil((reset - time.time()) / 60))
    return f"{base} Try again in about {minutes} minute{'s' if minutes != 1 else ''}."


_SHA256 = re.compile(r"^[0-9A-Fa-f]{64}$")
_LISTED = re.compile(r"^([0-9A-Fa-f]{64}) [ *](.+)$")  # "<hash>  <name>" (text) or "<hash> *<name>" (binary)
_FIELD = re.compile(r"^(Algorithm|Hash|Path)\s*:\s*(.*)$")


def parse_checksums(text: str, *, single_file: str | None = None) -> dict[str, str]:
    """{file name: lower-case SHA-256} from a checksum file. Understands:

    - sha256sum's "<hash>  <name>" and "<hash> *<name>" lines (yt-dlp's SHA2-256SUMS, FFmpeg-Builds'
      checksums.sha256);
    - PowerShell's `Get-FileHash | Format-List` output ("Algorithm : SHA256", "Hash : <HASH>",
      "Path : C:\\...\\<name>"), which is what Deno publishes as <asset>.sha256sum;
    - a bare "<hash>".

    A hash that comes without a file name is filed under single_file, and dropped when it isn't given.
    Lines that are none of these, and hashes by other algorithms, are ignored."""
    sums: dict[str, str] = {}
    fields: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if listed := _LISTED.match(line):
            sums[listed.group(2)] = listed.group(1).lower()
        elif _SHA256.match(line):
            if single_file is not None:
                sums[single_file] = line.lower()
        elif field := _FIELD.match(line):
            fields[field.group(1)] = field.group(2).strip()
    if fields.get("Algorithm", "").upper() == "SHA256" and _SHA256.match(fields.get("Hash", "")):
        name = PureWindowsPath(fields["Path"]).name if fields.get("Path") else single_file
        if name:
            sums[name] = fields["Hash"].lower()
    return sums
