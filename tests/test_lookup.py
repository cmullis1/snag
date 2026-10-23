import asyncio
import json
from pathlib import Path

import pytest

from snag.errors import ErrorKind
from snag.info.lookup import LookupFailed, LookupService, classify_url, lookup, lookup_args, parse_info, site_of
from snag.info.models import PlaylistInfo, VideoInfo
from snag.proc import Completed, run_capture


def test_classify_youtube_watch():
    kind = classify_url("https://www.youtube.com/watch?v=abc")
    assert kind.mode == "video" and kind.playlist is None


def test_classify_watch_inside_playlist():
    kind = classify_url("https://www.youtube.com/watch?v=abc&list=PL9")
    assert kind.mode == "video"
    assert kind.playlist.playlist_id == "PL9"
    assert kind.playlist.url == "https://www.youtube.com/playlist?list=PL9"


def test_classify_playlist_page():
    assert classify_url("https://www.youtube.com/playlist?list=PL9").mode == "playlist"


def test_classify_short_link_and_other_sites():
    assert classify_url("https://youtu.be/abc").mode == "video"
    assert classify_url("https://vimeo.com/123").mode == "auto"


def test_lookup_args(fake_tools):
    video = lookup_args("https://youtu.be/abc", fake_tools, classify_url("https://youtu.be/abc"))
    assert video[:len(fake_tools.ytdlp)] == list(fake_tools.ytdlp)
    assert "-J" in video and "--flat-playlist" in video and "--no-playlist" in video
    assert video[-2:] == ["--", "https://youtu.be/abc"]
    assert f"deno:{fake_tools.deno}" in video
    playlist = lookup_args("https://www.youtube.com/playlist?list=P", fake_tools, classify_url("https://www.youtube.com/playlist?list=P"))
    assert "--no-playlist" not in playlist


def test_parse_video():
    data = {
        "id": "abc", "title": "T", "extractor_key": "Youtube", "webpage_url": "https://www.youtube.com/watch?v=abc",
        "uploader": "U", "duration": 10, "thumbnail": "https://i/t.jpg", "upload_date": "20240101",
        "subtitles": {"en": [], "live_chat": []}, "automatic_captions": {"de": [], "en": []},
        "chapters": [{"title": "c"}],
        "formats": [{"format_id": "18", "ext": "mp4", "protocol": "https", "vcodec": "avc1", "acodec": "mp4a", "height": 360}],
    }
    info = parse_info(data, "https://www.youtube.com/watch?v=abc&list=PL1", classify_url("https://www.youtube.com/watch?v=abc&list=PL1"))
    assert isinstance(info, VideoInfo)
    assert info.site == "youtube"
    assert info.channel == "U"
    assert info.subtitle_langs == ("en",)
    assert info.auto_subtitle_langs == ("de", "en")
    assert info.has_chapters is True
    assert info.playlist.playlist_id == "PL1"
    assert info.formats.video_simple[0].format_id == "18"


def test_parse_single_format_page():
    data = {"id": "x", "title": "Clip", "extractor_key": "Generic", "url": "https://e/x.mp4", "ext": "mp4", "format_id": "0"}
    info = parse_info(data, "https://www.example.com/page", classify_url("https://www.example.com/page"))
    assert info.site == "example"  # the generic extractor is named after the page's site
    assert [row.format_id for row in info.formats.video_simple] == ["0"]


def test_parse_playlist():
    data = {
        "_type": "playlist", "id": "PL1", "title": "List", "extractor_key": "YoutubeTab",
        "entries": [
            {"id": "v1", "url": "https://www.youtube.com/watch?v=v1", "title": "One", "duration": 61,
             "thumbnails": [{"url": "https://i/small.jpg"}, {"url": "https://i/big.jpg"}]},
            None,
            {"id": "v2", "url": "https://www.youtube.com/watch?v=v2", "title": "Two"},
        ],
    }
    info = parse_info(data, "https://www.youtube.com/playlist?list=PL1", classify_url("https://www.youtube.com/playlist?list=PL1"))
    assert isinstance(info, PlaylistInfo)
    assert info.site == "youtube"
    assert [(e.index, e.video_id) for e in info.entries] == [(1, "v1"), (2, "v2")]
    assert info.entries[0].thumbnail == "https://i/big.jpg"


async def test_lookup_runs_fake_ytdlp(fake_tools):
    info = await lookup("fake://video", fake_tools)
    assert isinstance(info, VideoInfo)
    assert info.video_id == "abc123"


async def test_lookup_failure_raises_with_error(fake_tools):
    with pytest.raises(LookupFailed) as caught:
        await lookup("fake://nothing", fake_tools)
    assert caught.value.error.kind is ErrorKind.UNSUPPORTED


async def test_service_caches_for_ttl(fake_tools):
    calls = []
    now = [0.0]

    async def run(args):
        calls.append(args)
        return Completed(0, '{"id": "a", "title": "A", "extractor_key": "Youtube", "formats": []}', "")

    service = LookupService(fake_tools, run=run, clock=lambda: now[0], ttl=3600)
    await service.get("https://youtu.be/a")
    await service.get("https://youtu.be/a")
    assert len(calls) == 1
    now[0] = 3601
    await service.get("https://youtu.be/a")
    assert len(calls) == 2
    await service.get("https://youtu.be/a", fresh=True)
    assert len(calls) == 3


async def test_service_reuses_a_lookup_for_its_canonical_url(fake_tools):
    calls = []

    async def run(args):
        calls.append(args[-1])
        return Completed(0, json.dumps({
            "id": "x", "title": "X", "extractor_key": "Youtube", "formats": [],
            "webpage_url": "https://www.youtube.com/watch?v=x",
        }), "")

    service = LookupService(fake_tools, run=run)
    first = await service.get("https://youtu.be/x")
    # Asking by the lookup's page URL (the full tab keeps the lookup under it too) is the same lookup.
    assert await service.get("https://www.youtube.com/watch?v=x") is first
    assert calls == ["https://youtu.be/x"]


async def test_service_reuses_a_playlist_lookup_for_its_canonical_url(fake_tools):
    calls = []

    async def run(args):
        calls.append(args[-1])
        return Completed(0, json.dumps({
            "_type": "playlist", "id": "PL1", "title": "List", "extractor_key": "YoutubeTab", "entries": [],
            "webpage_url": "https://www.youtube.com/playlist?list=PL1",
        }), "")

    service = LookupService(fake_tools, run=run)
    first = await service.get("https://m.youtube.com/playlist?list=PL1")
    assert await service.get("https://www.youtube.com/playlist?list=PL1") is first
    assert len(calls) == 1


async def test_service_does_not_reuse_a_video_in_a_playlist_for_the_bare_video(fake_tools):
    # watch?v=x&list=PL1 answers with x's page and "from playlist PL1"; x on its own isn't from a playlist.
    calls = []

    async def run(args):
        calls.append(args[-1])
        return Completed(0, json.dumps({
            "id": "x", "title": "X", "extractor_key": "Youtube", "formats": [],
            "webpage_url": "https://www.youtube.com/watch?v=x",
        }), "")

    service = LookupService(fake_tools, run=run)
    in_list = await service.get("https://www.youtube.com/watch?v=x&list=PL1")
    assert in_list.playlist is not None
    alone = await service.get("https://www.youtube.com/watch?v=x")
    assert alone.playlist is None
    assert len(calls) == 2


async def test_lookup_timeout_raises_lookup_failed(fake_tools):
    async def run(args):
        raise TimeoutError

    with pytest.raises(LookupFailed) as caught:
        await lookup("https://youtu.be/abc", fake_tools, run=run)
    assert caught.value.error.kind is ErrorKind.NETWORK
    assert caught.value.error.message == "This page took too long to load. Try again."


def test_parsed_info_lists_are_tuples():
    video = parse_info({"id": "a", "extractor_key": "Youtube", "subtitles": {"en": []}, "formats": []},
                       "https://youtu.be/a", classify_url("https://youtu.be/a"))
    assert video.subtitle_langs == ("en",) and video.auto_subtitle_langs == ()
    playlist = parse_info({"_type": "playlist", "id": "P", "extractor_key": "YoutubeTab", "entries": [{"id": "v"}]},
                          "https://www.youtube.com/playlist?list=P", classify_url("https://www.youtube.com/playlist?list=P"))
    assert isinstance(playlist.entries, tuple)


def test_multi_video_parses_as_playlist():
    data = {"_type": "multi_video", "id": "M1", "title": "Parts", "extractor_key": "Generic",
            "webpage_url": "https://example.com/show", "entries": [{"id": "p1", "url": "https://example.com/p1", "title": "Part 1"}]}
    info = parse_info(data, "https://example.com/show", classify_url("https://example.com/show"))
    assert isinstance(info, PlaylistInfo)
    assert [e.video_id for e in info.entries] == ["p1"]


def test_multi_video_parts_without_their_own_url_use_the_page_and_position():
    page = "https://example.com/show"
    data = {"_type": "multi_video", "id": "M1", "title": "Parts", "extractor_key": "Generic", "webpage_url": page,
            "entries": [
                {"id": "p1", "title": "Part 1"},
                None,
                {"id": "p3", "title": "Part 3", "webpage_url": page, "playlist_index": 3},
                {"id": "p4", "title": "Part 4", "url": "https://example.com/p4"},
            ]}
    info = parse_info(data, page, classify_url(page))
    assert [(e.index, e.video_id, e.url, e.item_index) for e in info.entries] == [
        (1, "p1", page, 1),
        (2, "p3", page, 3),          # yt-dlp's own playlist_index, which counts the missing entry
        (3, "p4", "https://example.com/p4", None),
    ]


def test_playlist_entries_with_urls_have_no_item_index():
    data = {"_type": "playlist", "id": "PL1", "extractor_key": "YoutubeTab",
            "entries": [{"id": "v1", "url": "https://www.youtube.com/watch?v=v1", "playlist_index": 1}]}
    info = parse_info(data, "https://www.youtube.com/playlist?list=PL1", classify_url("https://www.youtube.com/playlist?list=PL1"))
    assert info.entries[0].item_index is None


def test_video_exposes_live_status():
    data = {"id": "a", "extractor_key": "Youtube", "live_status": "is_live", "formats": []}
    assert parse_info(data, "https://youtu.be/a", classify_url("https://youtu.be/a")).live_status == "is_live"
    data.pop("live_status")
    assert parse_info(data, "https://youtu.be/a", classify_url("https://youtu.be/a")).live_status is None


@pytest.mark.parametrize("stdout", ["", "null", "[]", '{"title": "no id", "formats": []}'])
async def test_unparseable_output_raises_lookup_failed(fake_tools, stdout):
    async def run(args):
        return Completed(0, stdout, "WARNING: something odd")

    with pytest.raises(LookupFailed) as caught:
        await lookup("https://youtu.be/abc", fake_tools, run=run)
    error = caught.value.error
    assert error.kind is ErrorKind.UNKNOWN
    assert "WARNING: something odd" in error.details
    assert len(error.details) > len("WARNING: something odd")  # the parse error is included too


@pytest.mark.parametrize("url, site", [
    ("https://www.youtube.com/watch?v=a", "youtube"),
    ("https://youtu.be/a", "youtube"),
    ("https://music.youtube.com/watch?v=a", "youtube"),
    ("https://www.youtube-nocookie.com/embed/a", "youtube"),
    ("https://soundcloud.com/artist/track", "soundcloud"),
    ("https://on.soundcloud.com/abc", "soundcloud"),
    ("https://www.vimeo.com/123", "vimeo"),
    ("https://player.vimeo.com/video/123", "vimeo"),
    ("https://www.bbc.co.uk/iplayer/episode/x", "bbc"),
    ("https://x.com/user/status/1", "x"),  # named by domain; yt-dlp's extractor is "twitter"
    ("https://localhost:8000/v.mp4", "localhost"),
    ("http://192.168.1.5/v.mp4", "192.168.1.5"),
    ("not a url", ""),
])
def test_site_of(url, site):
    assert site_of(url) == site


@pytest.mark.parametrize("extractor, url", [
    ("Youtube", "https://youtu.be/a"),
    ("Soundcloud", "https://soundcloud.com/artist/track"),
    ("Vimeo", "https://vimeo.com/123"),
    ("Generic", "https://www.example.com/clip"),
])
def test_parsed_site_matches_site_of_for_common_sites(extractor, url):
    data = {"id": "a", "extractor_key": extractor, "webpage_url": url, "formats": []}
    assert parse_info(data, url, classify_url(url)).site == site_of(url)


def _slow_run(calls, gate, stdout='{"id": "a", "extractor_key": "Youtube", "formats": []}', returncode=0):
    async def run(args):
        calls.append(args)
        await gate.wait()
        return Completed(returncode, stdout, "ERROR: [youtube] a: Video unavailable" if returncode else "")
    return run


async def test_service_shares_an_in_flight_lookup(fake_tools):
    calls, gate = [], asyncio.Event()
    service = LookupService(fake_tools, run=_slow_run(calls, gate))
    first = asyncio.create_task(service.get("https://youtu.be/a"))
    second = asyncio.create_task(service.get("https://youtu.be/a", fresh=True))
    await asyncio.sleep(0)
    gate.set()
    a, b = await asyncio.gather(first, second)
    assert len(calls) == 1
    assert a is b
    assert await service.get("https://youtu.be/a") is a
    assert len(calls) == 1


async def test_service_shares_failures_and_does_not_cache_them(fake_tools):
    calls, gate = [], asyncio.Event()
    service = LookupService(fake_tools, run=_slow_run(calls, gate, returncode=1))
    tasks = [asyncio.create_task(service.get("https://youtu.be/a")) for _ in range(2)]
    await asyncio.sleep(0)
    gate.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(r, LookupFailed) for r in results)
    assert len(calls) == 1
    with pytest.raises(LookupFailed):
        await service.get("https://youtu.be/a")
    assert len(calls) == 2


async def test_cancelled_caller_does_not_cancel_shared_lookup(fake_tools):
    calls, gate = [], asyncio.Event()
    service = LookupService(fake_tools, run=_slow_run(calls, gate))
    first = asyncio.create_task(service.get("https://youtu.be/a"))
    second = asyncio.create_task(service.get("https://youtu.be/a"))
    await asyncio.sleep(0)
    first.cancel()
    await asyncio.sleep(0)
    gate.set()
    assert (await second).video_id == "a"
    assert len(calls) == 1


async def test_service_drops_expired_entries_when_storing(fake_tools):
    now = [0.0]

    async def run(args):
        video_id = args[-1].rsplit("/", 1)[-1]
        return Completed(0, json.dumps({"id": video_id, "extractor_key": "Youtube", "formats": []}), "")

    service = LookupService(fake_tools, run=run, clock=lambda: now[0], ttl=3600)
    await service.get("https://youtu.be/a")
    now[0] = 3601
    await service.get("https://youtu.be/b")
    assert set(service._cache) == {"https://youtu.be/b"}


# --- lookups with cookies ---

COOKIES = "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tsecret\n"
SIGNIN_STDERR = "ERROR: [youtube] a: Sign in to confirm your age. This video may be inappropriate for some users."
VIDEO_JSON = '{"id": "a", "title": "A", "extractor_key": "Youtube", "formats": []}'


def test_lookup_args_pass_a_cookies_file(fake_tools, tmp_path):
    kind = classify_url("https://youtu.be/abc")
    args = lookup_args("https://youtu.be/abc", fake_tools, kind, tmp_path / "c.txt")
    assert args[args.index("--cookies") + 1] == str(tmp_path / "c.txt")
    assert args[-2:] == ["--", "https://youtu.be/abc"]
    assert "--cookies" not in lookup_args("https://youtu.be/abc", fake_tools, kind)


def _recording_run(seen, *, returncode=0, stdout=VIDEO_JSON, stderr="", raises=None):
    """Records each command and the cookies file's text while yt-dlp would be running."""
    async def run(args):
        cookies = args[args.index("--cookies") + 1] if "--cookies" in args else None
        seen.append((args, Path(cookies).read_text(encoding="utf-8") if cookies else None, cookies))
        if raises is not None:
            raise raises
        return Completed(returncode, stdout, stderr)
    return run


async def test_lookup_with_cookies_writes_them_for_yt_dlp_then_deletes_them(fake_tools, tmp_path):
    seen = []
    cookies_dir = tmp_path / "cookies"
    info = await lookup(
        "https://youtu.be/a", fake_tools, run=_recording_run(seen), cookies_text=COOKIES, cookies_dir=cookies_dir,
    )
    assert info.video_id == "a"
    (_args, text, path), = seen
    assert text == COOKIES
    assert Path(path).parent == cookies_dir
    assert list(cookies_dir.iterdir()) == []


@pytest.mark.parametrize("outcome", [
    dict(returncode=1, stderr="ERROR: [youtube] a: Video unavailable"),
    dict(stdout="not json"),
    dict(raises=TimeoutError()),
    dict(raises=RuntimeError("boom")),
])
async def test_lookup_cookies_file_is_deleted_whatever_happens(fake_tools, tmp_path, outcome):
    seen = []
    cookies_dir = tmp_path / "cookies"
    with pytest.raises((LookupFailed, RuntimeError)):
        await lookup(
            "https://youtu.be/a", fake_tools, run=_recording_run(seen, **outcome), cookies_text=COOKIES,
            cookies_dir=cookies_dir,
        )
    assert seen[0][1] == COOKIES
    assert list(cookies_dir.iterdir()) == []


async def test_lookup_cookies_file_is_deleted_when_cancelled(fake_tools, tmp_path):
    cookies_dir = tmp_path / "cookies"
    started = asyncio.Event()

    async def run(args):
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(lookup("https://youtu.be/a", fake_tools, run=run, cookies_text=COOKIES, cookies_dir=cookies_dir))
    await started.wait()
    assert len(list(cookies_dir.iterdir())) == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert list(cookies_dir.iterdir()) == []


async def test_lookup_with_cookies_needs_a_cookies_folder(fake_tools):
    with pytest.raises(ValueError):
        await lookup("https://youtu.be/a", fake_tools, run=_recording_run([]), cookies_text=COOKIES)


async def test_lookup_cookies_that_cannot_be_written_fail_the_lookup(fake_tools, tmp_path):
    blocked = tmp_path / "cookies"
    blocked.write_text("a file where the folder should be")
    seen = []
    with pytest.raises(LookupFailed) as caught:
        await lookup("https://youtu.be/a", fake_tools, run=_recording_run(seen), cookies_text=COOKIES, cookies_dir=blocked)
    assert str(blocked) in caught.value.error.message
    assert caught.value.error.can_retry
    assert seen == []


async def test_fake_ytdlp_lookup_needs_cookies_for_a_sign_in_video(fake_tools, tmp_path):
    with pytest.raises(LookupFailed) as caught:
        await lookup("fake://signin", fake_tools)
    assert caught.value.error.kind is ErrorKind.SIGN_IN
    info = await lookup("fake://signin", fake_tools, cookies_text=COOKIES, cookies_dir=tmp_path / "cookies")
    assert info.video_id == "abc123"


def _counting(run, calls):
    async def counted(args):
        calls.append(args)
        return await run(args)
    return counted


async def test_service_retries_a_sign_in_lookup_once_with_cookies(fake_tools, tmp_path):
    calls, asked = [], []
    cookies_dir = tmp_path / "cookies"

    def cookies(url):
        asked.append(url)
        return COOKIES

    service = LookupService(
        fake_tools, run=_counting(run_capture, calls), cookies=cookies, cookies_dir=cookies_dir,
    )
    info = await service.get("fake://signin")
    assert info.video_id == "abc123"
    assert ["--cookies" in args for args in calls] == [False, True]  # cookies only once they're needed
    assert asked == ["fake://signin"]
    assert list(cookies_dir.iterdir()) == []
    # The result is cached like any other, so it isn't looked up again.
    assert await service.get("fake://signin") is info
    assert len(calls) == 2


async def test_service_does_not_ask_for_cookies_when_the_lookup_works(fake_tools, tmp_path):
    asked = []
    service = LookupService(fake_tools, cookies=lambda url: asked.append(url) or COOKIES, cookies_dir=tmp_path)
    assert (await service.get("fake://video")).video_id == "abc123"
    assert asked == []


async def test_service_keeps_the_sign_in_error_without_cookies(fake_tools, tmp_path):
    calls = []
    service = LookupService(fake_tools, run=_counting(run_capture, calls), cookies=lambda url: None, cookies_dir=tmp_path)
    with pytest.raises(LookupFailed) as caught:
        await service.get("fake://signin")
    assert caught.value.error.kind is ErrorKind.SIGN_IN
    assert len(calls) == 1
    no_provider = LookupService(fake_tools, run=_counting(run_capture, calls))
    with pytest.raises(LookupFailed):
        await no_provider.get("fake://signin")
    assert len(calls) == 2


async def test_service_retries_with_cookies_only_once(fake_tools, tmp_path):
    seen = []
    service = LookupService(
        fake_tools, run=_recording_run(seen, returncode=1, stderr=SIGNIN_STDERR), cookies=lambda url: COOKIES,
        cookies_dir=tmp_path / "cookies",
    )
    with pytest.raises(LookupFailed) as caught:
        await service.get("https://youtu.be/a")
    assert caught.value.error.kind is ErrorKind.SIGN_IN
    assert [text for _args, text, _path in seen] == [None, COOKIES]


async def test_service_does_not_retry_other_failures_with_cookies(fake_tools, tmp_path):
    seen = []
    service = LookupService(
        fake_tools, run=_recording_run(seen, returncode=1, stderr="ERROR: [youtube] a: Video unavailable"),
        cookies=lambda url: COOKIES, cookies_dir=tmp_path / "cookies",
    )
    with pytest.raises(LookupFailed):
        await service.get("https://youtu.be/a")
    assert len(seen) == 1


async def test_service_uses_cookies_the_caller_brought(fake_tools, tmp_path):
    calls = []
    service = LookupService(fake_tools, run=_counting(run_capture, calls), cookies_dir=tmp_path / "cookies")
    info = await service.get("fake://signin", cookies_text=COOKIES)
    assert info.video_id == "abc123"
    assert ["--cookies" in args for args in calls] == [False, True]


async def test_service_prefers_the_callers_cookies_over_the_provider(fake_tools, tmp_path):
    seen = []
    results = iter([Completed(1, "", SIGNIN_STDERR), Completed(0, VIDEO_JSON, "")])

    async def run(args):
        await _recording_run(seen)(args)
        return next(results)

    service = LookupService(fake_tools, run=run, cookies=lambda url: "# stale\n", cookies_dir=tmp_path / "cookies")
    await service.get("https://youtu.be/a", cookies_text=COOKIES)
    assert [text for _args, text, _path in seen] == [None, COOKIES]


async def test_service_uses_cookies_brought_by_a_caller_that_joins_a_running_lookup(fake_tools, tmp_path):
    seen, gate = [], asyncio.Event()
    results = iter([Completed(1, "", SIGNIN_STDERR), Completed(0, VIDEO_JSON, "")])

    async def run(args):
        await _recording_run(seen)(args)
        await gate.wait()
        return next(results)

    service = LookupService(fake_tools, run=run, cookies_dir=tmp_path / "cookies")
    first = asyncio.create_task(service.get("https://youtu.be/a"))
    await asyncio.sleep(0)
    second = asyncio.create_task(service.get("https://youtu.be/a", cookies_text=COOKIES))
    await asyncio.sleep(0)
    gate.set()
    a, b = await asyncio.gather(first, second)
    assert a is b
    assert [text for _args, text, _path in seen] == [None, COOKIES]


async def test_service_forgets_offered_cookies_once_a_lookup_ends(fake_tools, tmp_path):
    seen = []
    service = LookupService(
        fake_tools, run=_recording_run(seen, returncode=1, stderr=SIGNIN_STDERR), cookies_dir=tmp_path / "cookies",
    )
    with pytest.raises(LookupFailed):
        await service.get("https://youtu.be/a", cookies_text=COOKIES)
    with pytest.raises(LookupFailed):
        await service.get("https://youtu.be/a")
    assert [text for _args, text, _path in seen] == [None, COOKIES, None]


def test_service_with_cookies_needs_a_cookies_folder(fake_tools):
    with pytest.raises(ValueError):
        LookupService(fake_tools, cookies=lambda url: None)


async def test_service_with_offered_cookies_needs_a_cookies_folder(fake_tools):
    service = LookupService(fake_tools, run=_recording_run([]))
    with pytest.raises(ValueError):
        await service.get("https://youtu.be/a", cookies_text=COOKIES)


# --- a missing JavaScript runtime (Deno) ---

JS_WARNING = (
    "WARNING: [youtube] No supported JavaScript runtime could be found. Only deno is enabled by default; to use "
    "another runtime add  --js-runtimes RUNTIME[:PATH]  to your command/config. YouTube extraction without a JS "
    "runtime has been deprecated, and some formats may be missing."
)


async def test_lookup_reports_a_missing_javascript_runtime(fake_tools):
    info = await lookup("https://youtu.be/a", fake_tools, run=_recording_run([], stderr=JS_WARNING))
    assert info.js_runtime_missing is True
    fine = await lookup("https://youtu.be/a", fake_tools, run=_recording_run([], stderr="WARNING: something else"))
    assert fine.js_runtime_missing is False
