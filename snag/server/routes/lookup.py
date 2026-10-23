"""POST /api/lookup, and the lookup and cookie handling every download request shares.

POST /api/lookup {url, cookies?: [Chrome cookie objects]}
  -> video:    schemas.video_info_json, with duplicate (the latest successful download), live_status
               and js_runtime_missing
  -> playlist: schemas.playlist_info_json, each entry with its duplicate
  A lookup that fails -> 422 with the lookup's own error (kind sign_in, unavailable, ...)."""
from __future__ import annotations

import asyncio
import math
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from fastapi import APIRouter
from starlette.requests import Request

from snag.hook import test_hook_on
from snag.info.lookup import Info, LookupFailed
from snag.info.models import PlaylistInfo, VideoInfo
from snag.server.hub import youtube_login
from snag.server.routes import ApiError, bad_request, json_body, text_field
from snag.server.schemas import playlist_info_json, video_info_json

if TYPE_CHECKING:
    from snag.app import SnagCore

router = APIRouter()

MAX_URL = 4096
MAX_COOKIES = 2000
_COOKIE_TEXT = ("domain", "name", "value", "path")  # written to a cookies.txt line: no tabs or newlines
_COOKIE_FLAGS = ("secure", "httpOnly", "hostOnly", "session")
LIVE = ("is_live", "is_upcoming")
WEB_SCHEMES = ("http", "https")


def core_of(request: Request) -> SnagCore:
    return request.app.state.core


def url_of(body: dict[str, Any]) -> str:
    """The request's url: a web address (http:// or https://) with a host."""
    url = text_field(body, "url", max_length=MAX_URL)
    not_web = bad_request("Snag downloads from web addresses starting with http:// or https://.")
    if url.startswith("-") or any(ch.isspace() for ch in url):
        raise not_web
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:  # e.g. a malformed IPv6 host or port
        raise not_web from None
    # The tests' fake yt-dlp's fake:// addresses, only under the test hook (snag.hook).
    schemes = (*WEB_SCHEMES, "fake") if test_hook_on() else WEB_SCHEMES
    if parts.scheme.lower() not in schemes or not host:
        raise not_web
    return url


def parse_cookies(value: Any) -> list[dict]:
    """Cookies as chrome.cookies returns them; only the fields Snag writes are checked."""
    if not isinstance(value, list) or len(value) > MAX_COOKIES:
        raise bad_request("cookies must be a list of cookies.")
    for cookie in value:
        if not isinstance(cookie, dict):
            raise bad_request("Each cookie must be an object.")
        for key in _COOKIE_TEXT:
            text = cookie.get(key)
            if text is None and key == "path":
                continue
            if not isinstance(text, str) or (key == "domain" and not text.strip("."))\
                    or any(ch in text for ch in "\t\r\n"):
                raise bad_request(f"A cookie's {key} isn't valid.")
        for key in _COOKIE_FLAGS:
            if key in cookie and not isinstance(cookie[key], bool):
                raise bad_request(f"A cookie's {key} must be true or false.")
        expires = cookie.get("expirationDate")
        if expires is not None and (isinstance(expires, bool) or not isinstance(expires, (int, float))
                                    or not math.isfinite(expires)):
            raise bad_request("A cookie's expirationDate must be a number.")
    return value


def store_cookies(core: SnagCore, url: str, cookies: list[dict]) -> None:
    """Keep the cookies the browser sent for url (in memory), and tell the pages when YouTube's
    login was refreshed."""
    core.cookies.put(url, cookies)
    core.emit({"type": "status", "youtube_login": youtube_login(core)})


def take_cookies(core: SnagCore, url: str, body: dict[str, Any]) -> str | None:
    """The request's cookies, if it brought any: stored, and returned as the text a lookup passes on."""
    if body.get("cookies") is None:
        return None
    store_cookies(core, url, parse_cookies(body["cookies"]))
    return core.cookies.get(url)


async def look_up(core: SnagCore, url: str, cookies_text: str | None) -> Info:
    """The (cached) lookup; a failure is a 422 carrying the lookup's error."""
    try:
        return await core.lookups.get(url, cookies_text=cookies_text)
    except LookupFailed as failure:
        raise ApiError(422, failure.error.kind.value, failure.error.message, error=failure.error) from None


async def look_up_video(core: SnagCore, body: dict[str, Any]) -> VideoInfo:
    """The video a download request names; refuses playlists and live streams."""
    url = url_of(body)
    info = await look_up(core, url, take_cookies(core, url, body))
    if isinstance(info, PlaylistInfo):
        raise bad_request("This link is a playlist. Choose the videos to download from it.")
    if info.live_status in LIVE:
        what = "is live" if info.live_status == "is_live" else "hasn't started yet"
        raise ApiError(422, "unsupported", f"This stream {what}. Snag can't download live streams; "
                                           "try again once it has ended.")
    return info


@router.post("/api/lookup")
async def lookup(request: Request) -> dict[str, Any]:
    core = core_of(request)
    body = await json_body(request)
    url = url_of(body)
    info = await look_up(core, url, take_cookies(core, url, body))
    # History is read off the event loop, like the History routes.
    if isinstance(info, PlaylistInfo):
        ids = [entry.video_id for entry in info.entries]
        duplicates = await asyncio.to_thread(core.history.find_duplicates, info.site, ids)
        return playlist_info_json(info, duplicates)
    return video_info_json(info, await asyncio.to_thread(core.history.find_duplicate, info.site, info.video_id))
