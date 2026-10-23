"""Asks yt-dlp what a URL contains: a video with formats, or a playlist."""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import time
import traceback
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import parse_qs, urlparse

from snag.errors import ErrorInfo, ErrorKind, classify, make_error, missing_js_runtime
from snag.paths import Tools
from snag.proc import Completed, run_capture

from .formats import build_format_lists
from .models import PlaylistEntry, PlaylistInfo, PlaylistRef, VideoInfo

Run = Callable[[list[str]], Awaitable[Completed]]
Info = VideoInfo | PlaylistInfo
# The Netscape cookies text a browser would send to a URL, or None (e.g. snag.cookies.CookieJar.get).
CookieSource = Callable[[str], str | None]

log = logging.getLogger(__name__)

LOOKUP_TIMEOUT = 120.0
# A "multi_video" is one page split into parts; the user picks parts like playlist entries.
_LIST_TYPES = {"playlist", "multi_video"}
_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
_SECOND_LEVEL = {"ac", "co", "com", "edu", "gov", "go", "ne", "net", "or", "org"}


@dataclass(frozen=True)
class UrlKind:
    mode: str  # "video", "playlist", or "auto" (let yt-dlp decide)
    playlist: PlaylistRef | None = None


class LookupFailed(Exception):
    def __init__(self, error: ErrorInfo):
        super().__init__(error.message)
        self.error = error


def classify_url(url: str) -> UrlKind:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host not in _YOUTUBE_HOSTS:
        return UrlKind("auto")
    list_id = parse_qs(parsed.query).get("list", [None])[0]
    ref = PlaylistRef(list_id, f"https://www.youtube.com/playlist?list={list_id}") if list_id else None
    if parsed.path == "/playlist" and ref:
        return UrlKind("playlist", ref)
    return UrlKind("video", ref)


def _is_youtube(host: str) -> bool:
    return host in _YOUTUBE_HOSTS or host.endswith((".youtube.com", "youtube-nocookie.com"))


def site_of(url: str) -> str:
    """A short site name from the URL's domain alone: "youtube" for YouTube, otherwise the first label of
    the registrable domain ("player.vimeo.com" -> "vimeo", "www.bbc.co.uk" -> "bbc"). It names sites by
    domain, so it can differ from the extractor name `parse_info` reports for a known site
    ("x.com" -> "x", where the extractor is "twitter"); the two agree for YouTube, sites named after
    their domain, and every page the generic extractor handles."""
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    if _is_youtube(host):
        return "youtube"
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    labels = host.split(".")
    if len(labels) < 2:
        return host
    # Without a public-suffix list, treat "co.uk"-style pairs as the suffix.
    two_part_suffix = len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL
    return labels[-3] if two_part_suffix else labels[-2]


def lookup_args(url: str, tools: Tools, kind: UrlKind, cookies_file: Path | None = None) -> list[str]:
    args = [*tools.ytdlp, "-J", "--encoding", "utf-8", "--js-runtimes", f"deno:{tools.deno}", "--flat-playlist"]
    if kind.mode != "playlist":
        args.append("--no-playlist")
    if cookies_file is not None:
        args += ["--cookies", str(cookies_file)]
    return [*args, "--", url]


def _site(data: dict, url: str) -> str:
    key = str(data.get("extractor_key") or data.get("extractor") or "generic").lower()
    if key.startswith("youtube"):
        return "youtube"
    if key == "generic":
        # Every unknown site shares the generic extractor; name it after the page instead.
        return site_of(data.get("webpage_url") or url) or key
    return key


def _thumbnail(data: dict) -> str | None:
    if data.get("thumbnail"):
        return data["thumbnail"]
    thumbs = data.get("thumbnails") or []
    return thumbs[-1].get("url") if thumbs else None


def _raw_formats(data: dict) -> list[dict]:
    # A page with a single direct file has no "formats" list; yt-dlp puts that format at the top level.
    return data.get("formats") or ([data] if "format_id" in data else [])


def _entry(index: int, position: int, entry: dict, page_url: str) -> PlaylistEntry:
    entry_url = entry.get("url") or entry.get("webpage_url")
    item_index = None
    if not entry_url or entry_url == page_url:
        # A part of a page split into several videos ("multi_video") has no address of its own; it's
        # downloaded from the page with --playlist-items, which counts like yt-dlp's playlist_index.
        entry_url = page_url
        playlist_index = entry.get("playlist_index")
        valid = isinstance(playlist_index, int) and not isinstance(playlist_index, bool) and playlist_index > 0
        item_index = playlist_index if valid else position
    return PlaylistEntry(
        index=index,
        video_id=str(entry.get("id")),
        url=entry_url,
        title=entry.get("title") or "Untitled",
        duration=entry.get("duration"),
        thumbnail=_thumbnail(entry),
        item_index=item_index,
    )


def parse_info(data: dict, url: str, kind: UrlKind) -> Info:
    site = _site(data, url)
    if data.get("_type") in _LIST_TYPES:
        page_url = data.get("webpage_url") or url
        # position counts every slot yt-dlp listed (missing ones too); index numbers the entries shown.
        present = [(position, e) for position, e in enumerate(data.get("entries") or [], start=1) if e]
        entries = tuple(
            _entry(index, position, entry, page_url) for index, (position, entry) in enumerate(present, start=1)
        )
        return PlaylistInfo(
            site=site,
            playlist_id=str(data.get("id")),
            url=page_url,
            title=data.get("title") or "Playlist",
            entries=entries,
        )
    subtitles = data.get("subtitles") or {}
    return VideoInfo(
        site=site,
        video_id=str(data["id"]),
        url=data.get("webpage_url") or url,
        title=data.get("title") or "Untitled",
        channel=data.get("channel") or data.get("uploader"),
        duration=data.get("duration"),
        thumbnail=_thumbnail(data),
        upload_date=data.get("upload_date"),
        formats=build_format_lists(_raw_formats(data), data.get("duration")),
        subtitle_langs=tuple(sorted(lang for lang in subtitles if lang != "live_chat")),
        auto_subtitle_langs=tuple(sorted(data.get("automatic_captions") or {})),
        has_chapters=bool(data.get("chapters")),
        playlist=kind.playlist,
        live_status=data.get("live_status"),
    )


async def _run_with_timeout(args: list[str]) -> Completed:
    return await run_capture(args, timeout=LOOKUP_TIMEOUT)


def _write_cookies(cookies_dir: Path, text: str) -> Path:
    path = cookies_dir / f"lookup-{uuid.uuid4().hex}.txt"
    cookies_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")
    return path


def _remove_cookies(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        # Startup clears the cookies folder, so this one goes then at the latest.
        log.warning("Couldn't delete the cookies file %s", path, exc_info=True)


async def lookup(
    url: str,
    tools: Tools,
    run: Run = _run_with_timeout,
    cookies_text: str | None = None,
    cookies_dir: Path | None = None,
) -> Info:
    """Ask yt-dlp about the URL. With cookies_text, the cookies go to a temporary file in cookies_dir
    (required then) for yt-dlp to use, and the file is deleted afterwards whatever happens."""
    if cookies_text is None:
        return await _lookup(url, tools, run, None)
    if cookies_dir is None:
        raise ValueError("a lookup with cookies needs the folder to write them in")
    try:
        cookies_file = _write_cookies(cookies_dir, cookies_text)
    except OSError:
        raise LookupFailed(make_error(
            ErrorKind.UNKNOWN, site_of(url), message=f"Can't write to Snag's cookies folder in {cookies_dir}",
            details=traceback.format_exc(),
        )) from None
    try:
        return await _lookup(url, tools, run, cookies_file)
    finally:
        # run has stopped yt-dlp by the time it returns or raises, so nothing still reads the file.
        _remove_cookies(cookies_file)


async def _lookup(url: str, tools: Tools, run: Run, cookies_file: Path | None) -> Info:
    kind = classify_url(url)
    try:
        done = await run(lookup_args(url, tools, kind, cookies_file))
    except TimeoutError:
        raise LookupFailed(
            make_error(ErrorKind.NETWORK, site_of(url), message="This page took too long to load. Try again.")
        ) from None
    if done.returncode != 0:
        raise LookupFailed(classify(done.stderr, site_of(url)))
    try:
        data = json.loads(done.stdout)
        if not isinstance(data, dict):
            raise ValueError(f"expected a JSON object, got {type(data).__name__}")
        info = parse_info(data, url, kind)
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        details = f"{done.stderr.rstrip()}\nCould not read yt-dlp's output: {type(exc).__name__}: {exc}"
        raise LookupFailed(make_error(ErrorKind.UNKNOWN, site_of(url), details=details.lstrip())) from exc
    if isinstance(info, VideoInfo) and missing_js_runtime(done.stderr):
        info = replace(info, js_runtime_missing=True)
    return info


class LookupService:
    """Caches lookups for the UI. Downloads never use cached format links; they re-run yt-dlp.

    Callers asking for the same URL while a lookup is running share it (one yt-dlp process),
    even with `fresh=True`, since a lookup still in progress is as fresh as a new one.

    Cookies are used only when needed (spec 5.2): a lookup runs without them, and one that fails because
    sign-in is required is retried once with cookies when there are any: the latest a caller brought
    (`cookies_text`) while the lookup ran, else those from the `cookies` source. The result is cached
    under the URL like any other, and under the page's own URL too (see _store)."""

    def __init__(
        self,
        tools: Tools,
        run: Run = _run_with_timeout,
        clock: Callable[[], float] = time.monotonic,
        ttl: float = 3600.0,
        *,
        cookies: CookieSource | None = None,
        cookies_dir: Path | None = None,
    ):
        if cookies is not None and cookies_dir is None:
            raise ValueError("a cookies source needs the folder to write cookies in")
        self._tools = tools
        self._run = run
        self._clock = clock
        self._ttl = ttl
        self._cookies = cookies
        self._cookies_dir = cookies_dir
        self._cache: dict[str, tuple[Info, float]] = {}
        self._in_flight: dict[str, asyncio.Task[Info]] = {}
        self._offered: dict[str, str] = {}  # url -> cookies a caller brought while its lookup runs

    async def get(self, url: str, fresh: bool = False, cookies_text: str | None = None) -> Info:
        if cookies_text is not None and self._cookies_dir is None:
            raise ValueError("cookies need a cookies folder: pass cookies_dir to LookupService")
        if not fresh:
            hit = self._cache.get(url)
            if hit is not None and self._clock() - hit[1] < self._ttl:
                return hit[0]
        if cookies_text is not None:
            self._offered[url] = cookies_text
        task = self._in_flight.get(url)
        if task is None:
            task = asyncio.ensure_future(self._fetch(url))
            self._in_flight[url] = task
            task.add_done_callback(lambda done: self._finished(url, done))
        # shield: one caller giving up must not cancel the lookup the others are waiting on.
        return await asyncio.shield(task)

    async def _fetch(self, url: str) -> Info:
        started = self._clock()
        try:
            info = await lookup(url, self._tools, self._run)
        except LookupFailed as failure:
            if failure.error.kind is not ErrorKind.SIGN_IN:
                raise
            cookies_text = self._offered.get(url) or (self._cookies(url) if self._cookies else None)
            if cookies_text is None:
                raise
            info = await lookup(url, self._tools, self._run, cookies_text, self._cookies_dir)
        self._store(url, info, started)
        return info

    def _store(self, url: str, info: Info, started: float) -> None:
        """Cache info under the URL asked for and under its page's own URL (info.url: a video's or a
        playlist's canonical address), so asking again by that address (the full tab keeps a lookup under
        it too, and a download from there asks by it) reuses this lookup. Only when the two read alike:
        watch?v=x&list=PL1 answers with x's page and "from playlist PL1", which x's own address must not
        get; so the UI's downloads ask by the address they looked up, never by info.url."""
        now = self._clock()
        self._cache = {key: hit for key, hit in self._cache.items() if now - hit[1] < self._ttl}
        self._cache[url] = (info, started)
        if info.url != url and classify_url(info.url) == classify_url(url):
            self._cache[info.url] = (info, started)

    def _finished(self, url: str, task: asyncio.Task[Info]) -> None:
        if self._in_flight.get(url) is task:
            del self._in_flight[url]
            self._offered.pop(url, None)
        if not task.cancelled():
            task.exception()  # mark it retrieved even if every caller was cancelled
