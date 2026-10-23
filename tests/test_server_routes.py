"""The HTTP API (Plan 2a Task 6) against a real SnagCore on tmp_path with the fake yt-dlp, through
httpx's in-process ASGI transport; the platform hooks (folder dialog, Explorer, Startup shortcut) are
fakes, so nothing opens on screen and no shortcut is made."""
import asyncio
import json
import threading
import time
from pathlib import Path

import httpx
import pytest
from fastapi.routing import APIRoute
from starlette.routing import Mount, WebSocketRoute

import snag
from snag.errors import ErrorKind, make_error
from snag.jobs.models import ChoiceKind, Extras, FormatChoice, Job, JobState, extras_to_dict
from snag.jobs.naming import playlist_dest
from snag.proc import Completed
from snag.server.app import Platform, create_app
from snag.server.hub import status_json
from snag.server.schemas import job_json
from tests.helpers import make_spec, wait_for
from tests.server_env import BASE, EVIL, EXTENSION, TAB, WsSession, core, github, snag_defaults  # noqa: F401
from tests.updater_env import GOOD, publish_ytdlp


class FakePlatform:
    def __init__(self):
        self.picked: list[str | None] = []
        self.pick_threads: list[int] = []
        self.answer: str | None = None
        self.opened: list[Path] = []
        self.startup: list[bool] = []
        self.startup_error: OSError | None = None
        self.started = True  # whether "Windows" would start Snag: its Startup shortcut, as setup.ps1 makes it
        self.started_error: OSError | None = None

    def pick_folder(self, initial):
        self.picked.append(initial)
        self.pick_threads.append(threading.get_ident())
        return self.answer

    def open_in_explorer(self, path):
        self.opened.append(Path(path))

    def set_start_with_windows(self, enabled):
        if self.startup_error is not None:
            raise self.startup_error
        self.startup.append(enabled)
        self.started = enabled

    def starts_with_windows(self):
        if self.started_error is not None:
            raise self.started_error
        return self.started

    def same_user_peer(self, peer, server):
        return True  # httpx's ASGI transport has no real connection to look up

    def hooks(self) -> Platform:
        return Platform(pick_folder=self.pick_folder, open_in_explorer=self.open_in_explorer,
                        set_start_with_windows=lambda enabled: self.set_start_with_windows(enabled),
                        starts_with_windows=lambda: self.starts_with_windows(),
                        same_user_peer=lambda peer, server: self.same_user_peer(peer, server))


@pytest.fixture
def platform():
    return FakePlatform()


@pytest.fixture
def app(core, platform):
    return create_app(core, platform=platform.hooks())


@pytest.fixture
async def client(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE, headers=TAB) as client:
        yield client


def finished_job(dest: Path, *, video_id="abc123", state=JobState.DONE, finished_at=None, title="Fake Video",
                 write=True, **overrides) -> Job:
    job = Job(spec=make_spec("https://www.youtube.com/watch?v=" + video_id, dest, video_id=video_id, title=title,
                             **overrides))
    job.state = state
    job.finished_at = finished_at or time.time()
    if state is JobState.DONE:
        job.file_path = dest / f"{title}.mp4"
        if write:
            dest.mkdir(parents=True, exist_ok=True)
            job.file_path.write_bytes(b"x")
    else:
        job.error = make_error(ErrorKind.NETWORK, "youtube")
    return job


def error_of(response: httpx.Response, status: int) -> dict:
    assert response.status_code == status, response.text
    return response.json()["error"]


# ----- the guard covers every route -----

def _all_routes(routes):
    """Every route, inside included routers too (FastAPI keeps those as one nested route each)."""
    for route in routes:
        included = getattr(route, "original_router", None)
        if included is not None:
            yield from _all_routes(included.routes)
        else:
            yield route


def _http_routes(app):
    for route in _all_routes(app.routes):
        if isinstance(route, APIRoute):
            path = route.path.replace("{tool}", "ffmpeg").replace("{entry_id}", "1")
            path = path.replace("{job_id}", "0" * 32).replace("{batch_id}", "0" * 32)
            for method in sorted(route.methods):
                yield method, path
        elif isinstance(route, Mount):
            yield "GET", f"{route.path}/index.html"


async def test_every_route_is_behind_the_guard(app, client):
    routes = list(_http_routes(app))
    assert len(routes) > 25
    for method, path in routes:
        wrong_host = await client.request(method, path, headers={"host": "evil.com:47823"})
        assert wrong_host.status_code == 403, (method, path)
        foreign = await client.request(method, path, headers=EVIL)
        assert foreign.status_code == 403, (method, path)
        if method not in ("GET", "HEAD"):
            request = client.build_request(method, path)
            del request.headers["origin"]
            assert (await client.send(request)).status_code == 403, (method, path)
    sockets = [route for route in _all_routes(app.routes) if isinstance(route, WebSocketRoute)]
    assert [route.path for route in sockets] == ["/ws"]
    session = WsSession(app, headers=EVIL)
    assert await session.accepted() is False
    await session.task


async def test_the_api_docs_are_not_served(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert (await client.get(path)).status_code == 404


async def test_errors_are_json(client):
    assert error_of(await client.get("/api/nothing-here"), 404)["kind"] == "not_found"
    not_allowed = error_of(await client.delete("/api/status"), 405)
    assert not_allowed["kind"] == "bad_request" and not_allowed["message"] == "That action isn't allowed here."


@pytest.mark.parametrize("headers", [TAB, EXTENSION])
async def test_api_answers_are_never_cached(client, headers):
    for response in (await client.get("/api/status", headers=headers), await client.get("/api/nothing")):
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["vary"] == "Origin"


async def test_a_body_over_the_limit_is_refused(client, core):
    from snag.server.routes import MAX_BODY

    big = b'{"url": "' + b"x" * MAX_BODY + b'"}'
    error = error_of(await client.post("/api/lookup", content=big, headers={"content-type": "application/json"}), 413)
    assert error["kind"] == "too_large" and "too big" in error["message"]

    async def chunks():  # no Content-Length: counted as it arrives
        for _ in range(17):
            yield b" " * (1024 * 1024)

    error = error_of(await client.post("/api/jobs", content=chunks()), 413)
    assert error["kind"] == "too_large"
    assert core.queue.jobs == []


# ----- static files -----

async def test_the_app_tab_is_served_with_its_policy(client):
    response = await client.get("/")
    assert response.status_code == 200
    assert "<title>Snag</title>" in response.text
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert (await client.get("/web/index.html")).text == response.text


async def test_ui_modules_are_served_as_javascript(client):
    response = await client.get("/ui/fields.js")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/javascript")
    assert response.headers["cache-control"] == "no-cache"


@pytest.mark.parametrize("path", [
    "/ui/../snag/app.py", "/ui/%2e%2e/snag/app.py", "/ui/..%2fsnag%2fapp.py", "/web/../pyproject.toml",
    "/ui/..\\snag\\app.py", "/ui/%5c..%5csnag%5capp.py",
])
async def test_static_files_never_leave_their_folders(client, path):
    response = await client.get(path)
    assert response.status_code == 404
    assert "SnagCore" not in response.text and "[project]" not in response.text


# ----- lookups -----

async def test_lookup_a_video(client, core):
    response = await client.post("/api/lookup", json={"url": "fake://video"})
    assert response.status_code == 200
    info = response.json()
    assert info["type"] == "video" and info["title"] == "Fake Video" and info["video_id"] == "abc123"
    assert info["duplicate"] is None
    assert info["js_runtime_missing"] is False and info["live_status"] is None
    assert [row["format_id"] for row in info["formats"]["video_all"]] == ["299"]


async def test_lookup_attaches_the_latest_download(client, core, tmp_path):
    core.history.add(finished_job(tmp_path / "out", finished_at=100.0))
    core.history.add(finished_job(tmp_path / "out", finished_at=200.0))
    info = (await client.post("/api/lookup", json={"url": "fake://video"})).json()
    assert info["duplicate"]["finished_at"] == 200.0 and info["duplicate"]["status"] == "done"


async def test_lookup_a_playlist_marks_downloaded_entries(client, core, tmp_path):
    core.history.add(finished_job(tmp_path / "out", video_id="v1"))
    core.history.add(finished_job(tmp_path / "out", video_id="v2", state=JobState.FAILED))
    info = (await client.post("/api/lookup", json={"url": "fake://playlist"})).json()
    assert info["type"] == "playlist" and info["title"] == "Fake Playlist"
    assert [e["video_id"] for e in info["entries"]] == ["v1", "v2"]
    assert info["entries"][0]["duplicate"]["video_id"] == "v1"
    assert info["entries"][1]["duplicate"] is None


async def test_lookup_with_cookies_stores_them_and_uses_them(client, core):
    cookies = [{"domain": "signin", "name": "SID", "value": "secret", "hostOnly": True, "path": "/"}]
    response = await client.post("/api/lookup", json={"url": "fake://signin", "cookies": cookies}, headers=EXTENSION)
    assert response.status_code == 200 and response.json()["title"] == "Fake Video"
    assert "secret" in core.cookies.get("fake://signin")
    assert list(core.paths.cookies_dir.iterdir()) == []


async def test_lookup_without_cookies_needing_sign_in_fails_plainly(client):
    error = error_of(await client.post("/api/lookup", json={"url": "fake://signin"}), 422)
    assert error["kind"] == "sign_in" and "Sign in" in error["message"]


async def test_a_failed_lookup_is_a_plain_error(client):
    error = error_of(await client.post("/api/lookup", json={"url": "fake://nothing"}), 422)
    assert error["message"] and error["details"]


@pytest.mark.parametrize("body", [
    {}, {"url": ""}, {"url": 5}, {"url": "fake://video\n--exec=calc"},
    {"url": "fake://video", "cookies": "SID=x"},
    {"url": "fake://video", "cookies": [{"domain": "x", "name": "a"}]},
    {"url": "fake://video", "cookies": [{"domain": "x", "name": "a", "value": "1\tTRUE"}]},
    {"url": "fake://video", "cookies": [{"domain": "x", "name": "a", "value": "1", "expirationDate": "soon"}]},
])
async def test_bad_lookup_requests_are_refused(client, body):
    assert error_of(await client.post("/api/lookup", json=body), 400)["kind"] == "bad_request"


@pytest.mark.parametrize("url", [
    "ftp://example.com/v.mp4", "file:///C:/Windows/win.ini", "javascript:alert(1)", "https://", "http:///path",
    "example.com/watch", "C:\\Videos\\clip.mp4", "\\\\nas\\share\\v.mp4", "data:text/html,hi", "https://[::1/",
])
async def test_only_web_addresses_are_looked_up(client, core, url):
    for path in ("/api/lookup", "/api/jobs/quick", "/api/cookies"):
        error = error_of(await client.post(path, json={"url": url, "cookies": []}), 400)
        assert "http://" in error["message"]
    assert core.queue.jobs == []


@pytest.mark.parametrize("hook", [None, "", "0", "yes"])
async def test_the_fake_scheme_needs_the_test_hook(client, monkeypatch, hook):
    """Only SNAG_TEST_HOOK=1 turns it on; the fake yt-dlp's own SNAG_FAKE_STATE (still set) doesn't."""
    if hook is None:
        monkeypatch.delenv("SNAG_TEST_HOOK")
    else:
        monkeypatch.setenv("SNAG_TEST_HOOK", hook)
    assert "http://" in error_of(await client.post("/api/lookup", json={"url": "fake://video"}), 400)["message"]


async def test_a_body_that_isnt_json_is_refused(client):
    response = await client.post("/api/lookup", content=b"url=fake://video", headers={"content-type": "text/plain"})
    assert "JSON" in error_of(response, 400)["message"]
    assert error_of(await client.post("/api/lookup", json=["fake://video"]), 400)["kind"] == "bad_request"


# ----- downloads -----

async def test_a_download_uses_the_lookup(client, core, snag_defaults):
    response = await client.post("/api/jobs", json={"url": "fake://downloadable/d1", "format_id": "299"})
    assert response.status_code == 201
    job = response.json()
    spec = job["spec"]
    assert spec["url"] == "fake://success/d1"  # the lookup's page URL
    assert (spec["title"], spec["site"], spec["video_id"], spec["channel"]) == ("Fake Video", "youtube", "d1", "Fake Channel")
    assert spec["choice"]["kind"] == "video" and spec["choice"]["format_id"] == "299" and spec["choice"]["audio_id"] == "140"
    assert spec["estimated_size"] == 5000  # the video row's size, with its paired audio
    assert spec["dest_dir"] == snag_defaults.video_dir
    assert spec["extras"] == extras_to_dict(Extras())
    queued = core.queue.get(job["id"])
    await wait_for(lambda: queued.state is JobState.DONE)
    assert queued.file_path.parent == Path(snag_defaults.video_dir)


async def test_a_download_of_a_video_looked_up_in_a_playlist_reuses_that_lookup(client, core, monkeypatch):
    """The UI sends the address it looked up (watch?v=x&list=PL1), not the lookup's page URL: x's own
    address is a different lookup (one not from a playlist), so sending it would run yt-dlp again."""
    looked_up = []

    async def run(args):
        looked_up.append(args[-1])
        return Completed(0, json.dumps({
            "id": "x", "title": "X", "extractor_key": "Youtube", "webpage_url": "https://www.youtube.com/watch?v=x",
            "formats": [{"format_id": "18", "ext": "mp4", "protocol": "https", "vcodec": "avc1", "acodec": "mp4a",
                         "height": 360, "filesize": 900}],
        }), "")

    monkeypatch.setattr(core.lookups, "_run", run)
    core.queue.pause()
    in_list = "https://www.youtube.com/watch?v=x&list=PL1"
    info = (await client.post("/api/lookup", json={"url": in_list})).json()
    assert info["playlist"]["playlist_id"] == "PL1" and info["url"] == "https://www.youtube.com/watch?v=x"
    response = await client.post("/api/jobs", json={"url": in_list, "format_id": "18"})
    assert response.status_code == 201
    assert response.json()["spec"]["url"] == "https://www.youtube.com/watch?v=x"  # queued with the page URL
    assert looked_up == [in_list], "the job reused the lookup"
    await client.post("/api/jobs", json={"url": info["url"], "format_id": "18"})
    assert looked_up == [in_list, info["url"]], "the page URL alone is a lookup of its own"


async def test_an_audio_download_goes_to_the_audio_folder(client, snag_defaults):
    job = (await client.post("/api/jobs", json={"url": "fake://downloadable/a1", "format_id": "140"})).json()
    assert job["spec"]["dest_dir"] == snag_defaults.audio_dir and job["kind"] == "audio"
    assert job["spec"]["estimated_size"] == 1000


async def test_a_download_with_another_audio_track(client):
    job = (await client.post("/api/jobs", json={
        "url": "fake://downloadable/t1", "format_id": "299", "audio_id": "140",
        "extras": {"subtitles": True, "subtitle_lang": "de"},
    })).json()
    assert job["spec"]["choice"]["audio_id"] == "140"
    assert job["spec"]["extras"]["subtitles"] is True and job["spec"]["extras"]["subtitle_lang"] == "de"
    assert job["spec"]["estimated_size"] == 5000


async def test_a_picked_folder_is_used_and_remembered(client, core, tmp_path):
    picked = tmp_path / "Picked"
    job = (await client.post("/api/jobs", json={
        "url": "fake://downloadable/p", "format_id": "299", "dest_dir": str(picked),
    })).json()
    assert job["spec"]["dest_dir"] == str(picked)
    assert core.settings.get().remembered_dirs == (str(picked),)
    assert picked in core.extra_roots()


@pytest.mark.parametrize("body, message", [
    ({"url": "fake://downloadable/x", "format_id": "nope"}, "format"),
    ({"url": "fake://downloadable/x", "format_id": "299", "dest_dir": "relative\\folder"}, "full path"),
    ({"url": "fake://downloadable/x", "format_id": "299", "audio_id": "nope"}, "audio track"),
    ({"url": "fake://downloadable/x", "format_id": "299", "extras": {"trim_start": "soon"}}, "Trim times"),
    ({"url": "fake://downloadable/x", "format_id": "299", "dest_dir": "\\\\.\\C:\\x"}, "device"),
    ({"url": "fake://downloadable/x", "format_id": "299", "dest_dir": "C:\\Videos\\NUL"}, "device"),
    ({"url": "fake://playlist", "format_id": "299"}, "playlist"),
])
async def test_bad_downloads_are_refused(client, core, body, message):
    error = error_of(await client.post("/api/jobs", json=body), 400)
    assert message in error["message"]
    assert core.queue.jobs == []


@pytest.mark.parametrize("status", ["is_live", "is_upcoming"])
async def test_live_streams_are_refused(client, core, status):
    for path, body in (("/api/jobs", {"format_id": "299"}), ("/api/jobs/quick", {})):
        error = error_of(await client.post(path, json={"url": f"fake://live/{status}", **body}), 422)
        assert "live" in error["message"].lower()
    assert core.queue.jobs == []


async def test_a_download_with_cookies_keeps_them(client, core):
    cookies = [{"domain": ".youtube.com", "name": "SID", "value": "v"}]
    response = await client.post("/api/jobs", json={"url": "fake://downloadable/c", "format_id": "299", "cookies": cookies})
    assert response.status_code == 201
    assert core.cookies.get("https://www.youtube.com/") is not None


async def test_a_quick_download_uses_the_settings(client, core, snag_defaults):
    quick_extras = Extras(subtitles=True, subtitle_lang="fr", chapters=True)
    core.settings.update(quick_max_height=720, quick_extras=quick_extras)
    response = await client.post("/api/jobs/quick", json={"url": "fake://downloadable/q"}, headers=EXTENSION)
    assert response.status_code == 201
    spec = response.json()["spec"]
    assert spec["choice"]["kind"] == ChoiceKind.QUICK.value and spec["choice"]["height"] == 720
    assert spec["extras"] == extras_to_dict(quick_extras)
    assert spec["dest_dir"] == snag_defaults.video_dir
    assert spec["url"] == "fake://success/q" and spec["video_id"] == "q"
    assert response.headers["access-control-allow-origin"] == EXTENSION["origin"]


async def test_a_quick_download_of_a_playlist_is_refused(client):
    assert "playlist" in error_of(await client.post("/api/jobs/quick", json={"url": "fake://playlist"}), 400)["message"]


# ----- playlists -----

async def test_a_batch_has_one_job_per_ticked_entry(client, core, tmp_path):
    core.queue.pause()
    picked = tmp_path / "Picked"
    response = await client.post("/api/jobs/batch", json={
        "url": "fake://fakelist", "entries": [1, 3], "quality": {"kind": "quick", "height": 1080},
        "extras": {"chapters": True}, "dest_dir": str(picked),
    })
    assert response.status_code == 201
    batch = response.json()
    jobs = batch["jobs"]
    assert [j["spec"]["title"] for j in jobs] == ["One", "Three"]
    for job, index in zip(jobs, (1, 3)):
        spec = job["spec"]
        assert spec["batch_id"] == batch["batch_id"]
        assert spec["dest_dir"] == str(playlist_dest(picked, "Fake: Playlist?"))
        assert (spec["playlist_index"], spec["playlist_count"], spec["playlist_item"]) == (index, 3, None)
        assert spec["site"] == "youtube" and spec["url"] == f"fake://success/p{index}"
        assert spec["video_id"] == f"p{index}"
        assert spec["choice"]["kind"] == "quick" and spec["choice"]["height"] == 1080
        assert spec["extras"]["chapters"] is True
    assert core.settings.get().remembered_dirs == (str(picked),)
    core.queue.resume()
    await wait_for(lambda: all(core.queue.get(j["id"]).state is JobState.DONE for j in jobs))
    summary = (await client.get(f"/api/batches/{batch['batch_id']}")).json()
    assert summary == {"total": 2, "done": 2, "failed": 0, "cancelled": 0, "pending": 0}


@pytest.mark.parametrize("quality, kind", [({"kind": "mp3"}, "mp3"), ({"kind": "best_audio"}, "best_audio")])
async def test_an_audio_batch_goes_to_the_audio_folder(client, core, snag_defaults, quality, kind):
    core.queue.pause()
    jobs = (await client.post("/api/jobs/batch", json={"url": "fake://fakelist", "entries": [2], "quality": quality})).json()["jobs"]
    assert jobs[0]["spec"]["choice"]["kind"] == kind
    assert jobs[0]["spec"]["dest_dir"] == str(playlist_dest(Path(snag_defaults.audio_dir), "Fake: Playlist?"))
    assert jobs[0]["spec"]["extras"] == extras_to_dict(Extras())


async def test_a_quick_batch_without_a_height_has_no_limit(client, core):
    core.queue.pause()
    jobs = (await client.post("/api/jobs/batch", json={
        "url": "fake://fakelist", "entries": [1], "quality": {"kind": "quick", "height": None},
    })).json()["jobs"]
    assert jobs[0]["spec"]["choice"]["height"] is None


async def test_parts_of_a_page_are_downloaded_by_item(client, core):
    core.queue.pause()
    jobs = (await client.post("/api/jobs/batch", json={
        "url": "fake://multipart", "entries": [2], "quality": {"kind": "mp3"},
    })).json()["jobs"]
    spec = jobs[0]["spec"]
    assert spec["url"] == "fake://success/multi" and spec["playlist_item"] == 2 and spec["playlist_index"] == 2
    assert spec["title"] == "Part 2" and spec["video_id"] == "m2"


@pytest.mark.parametrize("body", [
    {"url": "fake://fakelist", "entries": [], "quality": {"kind": "mp3"}},
    {"url": "fake://fakelist", "entries": [4], "quality": {"kind": "mp3"}},
    {"url": "fake://fakelist", "entries": [1, 1], "quality": {"kind": "mp3"}},
    {"url": "fake://fakelist", "entries": [True], "quality": {"kind": "mp3"}},
    {"url": "fake://fakelist", "entries": "1", "quality": {"kind": "mp3"}},
    {"url": "fake://fakelist", "entries": [1], "quality": {"kind": "video"}},
    {"url": "fake://fakelist", "entries": [1], "quality": {"kind": "quick", "height": 1000}},
    {"url": "fake://fakelist", "entries": [1]},
    {"url": "fake://fakelist", "entries": [1], "quality": {"kind": "mp3"}, "dest_dir": "rel"},
    {"url": "fake://video", "entries": [1], "quality": {"kind": "mp3"}},
])
async def test_bad_batches_are_refused(client, core, body):
    assert error_of(await client.post("/api/jobs/batch", json=body), 400)["kind"] == "bad_request"
    assert core.queue.jobs == []


# ----- jobs, batches, queue -----

async def test_list_cancel_retry_and_clear(client, core, tmp_path):
    job = core.queue.add(make_spec("fake://slow/c1", tmp_path / "out"))
    await wait_for(lambda: job.state is JobState.DOWNLOADING)
    listed = (await client.get("/api/jobs")).json()["jobs"]
    assert [j["id"] for j in listed] == [job.id]

    assert error_of(await client.post(f"/api/jobs/{job.id}/retry"), 409)["kind"] == "conflict"
    assert error_of(await client.post(f"/api/jobs/{job.id}/clear"), 409)["kind"] == "conflict"

    cancelled = await client.post(f"/api/jobs/{job.id}/cancel")
    assert cancelled.status_code == 200 and cancelled.json()["id"] == job.id
    await wait_for(lambda: job.state is JobState.CANCELLED)  # a running job settles a moment later

    elsewhere = tmp_path / "Elsewhere"
    retried = (await client.post(f"/api/jobs/{job.id}/retry", json={"dest_dir": str(elsewhere)})).json()
    assert retried["state"] == "waiting"
    assert core.settings.get().remembered_dirs == (str(elsewhere),)
    await wait_for(lambda: job.state is JobState.DOWNLOADING and job.spec.dest_dir == elsewhere)
    await client.post(f"/api/jobs/{job.id}/cancel")
    await wait_for(lambda: job.state is JobState.CANCELLED)

    cleared = await client.post(f"/api/jobs/{job.id}/clear")
    assert cleared.json() == {"cleared": [job.id]}
    assert core.queue.get(job.id) is None


async def test_retry_needs_a_full_path(client, core, tmp_path):
    job = core.queue.add(make_spec("fake://unavailable/r", tmp_path / "out"))
    await wait_for(lambda: job.state is JobState.FAILED)
    assert error_of(await client.post(f"/api/jobs/{job.id}/retry", json={"dest_dir": "rel"}), 400)
    # Unavailable: retrying can't help.
    assert "won't help" in error_of(await client.post(f"/api/jobs/{job.id}/retry"), 409)["message"]


async def test_clear_finished(client, core, tmp_path):
    done = core.queue.add(make_spec("fake://success/f1", tmp_path / "out"))
    failed = core.queue.add(make_spec("fake://unavailable/f2", tmp_path / "out"))
    await wait_for(lambda: done.state is JobState.DONE and failed.state is JobState.FAILED)
    assert (await client.post("/api/jobs/clear-finished")).json() == {"cleared": [done.id]}
    assert [j["id"] for j in (await client.get("/api/jobs")).json()["jobs"]] == [failed.id]


@pytest.mark.parametrize("action", ["cancel", "retry", "clear", "open-folder"])
async def test_unknown_jobs_are_not_found(client, action):
    assert error_of(await client.post(f"/api/jobs/{'0' * 32}/{action}"), 404)["kind"] == "not_found"


@pytest.mark.parametrize("action, method", [("cancel", "cancel"), ("clear", "clear"), ("retry", "retry")])
async def test_a_job_cleared_meanwhile_is_not_found(client, core, tmp_path, monkeypatch, action, method):
    job = core.queue.add(make_spec("fake://unavailable/gone", tmp_path / "out"))
    await wait_for(lambda: job.state is JobState.FAILED)

    def gone(job_id, *args):
        raise KeyError(job_id)

    async def gone_later(job_id, *args):
        await asyncio.sleep(0)
        raise KeyError(job_id)

    monkeypatch.setattr(core.queue, method, gone if method == "retry" else gone_later)
    assert error_of(await client.post(f"/api/jobs/{job.id}/{action}"), 404)["kind"] == "not_found"


async def test_a_done_job_can_be_cleared_and_keeps_its_file(client, core, tmp_path):
    job = core.queue.add(make_spec("fake://success/cd", tmp_path / "out"))
    await wait_for(lambda: job.state is JobState.DONE)
    listed = (await client.get("/api/jobs")).json()["jobs"]
    assert listed[0]["can_clear"] is True
    assert (await client.post(f"/api/jobs/{job.id}/clear")).json() == {"cleared": [job.id]}
    assert core.queue.get(job.id) is None and job.file_path.is_file()


async def test_open_a_done_jobs_folder_selects_its_file(client, core, platform, tmp_path):
    job = core.queue.add(make_spec("fake://success/of", tmp_path / "out"))
    await wait_for(lambda: job.state is JobState.DONE)
    assert (await client.post(f"/api/jobs/{job.id}/open-folder")).json() == {"opened": str(job.file_path)}
    job.file_path.unlink()  # moved away: its folder instead
    assert (await client.post(f"/api/jobs/{job.id}/open-folder")).json() == {"opened": str(job.file_path.parent)}
    job.file_path.parent.rename(tmp_path / "moved")
    error = error_of(await client.post(f"/api/jobs/{job.id}/open-folder"), 404)
    assert str(tmp_path / "out") in error["message"]
    assert platform.opened == [job.file_path, job.file_path.parent]


async def test_open_an_unfinished_jobs_folder(client, core, platform, tmp_path):
    core.queue.pause()
    job = core.queue.add(make_spec("fake://success/ou", tmp_path / "later"))
    assert error_of(await client.post(f"/api/jobs/{job.id}/open-folder"), 409)["kind"] == "conflict"
    (tmp_path / "later").mkdir()
    assert (await client.post(f"/api/jobs/{job.id}/open-folder")).json() == {"opened": str(tmp_path / "later")}
    assert platform.opened == [tmp_path / "later"]


async def test_open_folder_runs_off_the_loop(client, core, platform, tmp_path):
    threads = []
    platform.open_in_explorer = lambda path: threads.append(threading.get_ident())
    app = create_app(core, platform=platform.hooks())
    (tmp_path / "out").mkdir()
    core.queue.pause()
    job = core.queue.add(make_spec("fake://success/ot", tmp_path / "out"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE, headers=TAB) as other:
        assert (await other.post(f"/api/jobs/{job.id}/open-folder")).status_code == 200
    assert threads and threads[0] != threading.get_ident()


async def test_retry_refuses_a_device_folder(client, core, tmp_path):
    job = core.queue.add(make_spec("fake://fail-network/rd", tmp_path / "out"))
    await wait_for(lambda: job.state is JobState.FAILED)
    error = error_of(await client.post(f"/api/jobs/{job.id}/retry", json={"dest_dir": "\\\\?\\C:\\x"}), 400)
    assert "device" in error["message"] and job.state is JobState.FAILED


async def test_a_picked_folder_is_normalized(client, core, tmp_path):
    core.queue.pause()
    picked = tmp_path / "Picked"
    written = str(tmp_path / "Other" / ".." / "Picked").replace("\\", "/") + "/"
    job = (await client.post("/api/jobs", json={"url": "fake://downloadable/nm", "format_id": "299", "dest_dir": written})).json()
    assert job["spec"]["dest_dir"] == str(picked)
    assert core.settings.get().remembered_dirs == (str(picked),)


async def test_a_download_to_a_network_share_is_queued_and_remembered(client, core):
    core.queue.pause()
    share = "\\\\nas\\media\\Clips"
    job = (await client.post("/api/jobs", json={"url": "fake://downloadable/n", "format_id": "299", "dest_dir": share})).json()
    assert job["spec"]["dest_dir"] == share
    assert core.settings.get().remembered_dirs == (share,)


async def test_batches_retry_failed(client, core, tmp_path):
    batch_id = core.queue.new_batch_id()
    jobs = core.queue.add_batch([
        make_spec("fake://success/b1", tmp_path / "out", batch_id=batch_id),
        make_spec("fake://fail-network/b2", tmp_path / "out", batch_id=batch_id),
    ])
    await wait_for(lambda: jobs[0].state is JobState.DONE and jobs[1].state is JobState.FAILED)
    summary = (await client.get(f"/api/batches/{batch_id}")).json()
    assert summary == {"total": 2, "done": 1, "failed": 1, "cancelled": 0, "pending": 0}
    retried = (await client.post(f"/api/batches/{batch_id}/retry-failed")).json()["jobs"]
    assert [j["id"] for j in retried] == [jobs[1].id]
    assert error_of(await client.get(f"/api/batches/{'0' * 32}"), 404)
    assert error_of(await client.post(f"/api/batches/{'0' * 32}/retry-failed"), 404)


async def test_pause_and_resume(client, core):
    paused = (await client.post("/api/queue/pause")).json()
    assert paused["paused"] is True and core.queue.paused
    assert (await client.get("/api/queue")).json() == paused
    assert (await client.post("/api/queue/resume")).json()["paused"] is False


# ----- history -----

async def test_history_pages_with_a_total(client, core, tmp_path):
    for n in range(5):
        core.history.add(finished_job(tmp_path / "out", video_id=f"h{n}", title=f"Title {n}", finished_at=100.0 + n))
    core.history.add(finished_job(tmp_path / "out", video_id="audio", title="Song", finished_at=50.0,
                                  choice=FormatChoice(ChoiceKind.MP3)))
    page = (await client.get("/api/history", params={"limit": 2, "offset": 2})).json()
    assert page["total"] == 6
    assert [e["title"] for e in page["entries"]] == ["Title 2", "Title 1"]
    found = (await client.get("/api/history", params={"search": "title 4"})).json()
    assert found["total"] == 1 and found["entries"][0]["video_id"] == "h4"
    audio = (await client.get("/api/history", params={"kind": "audio"})).json()
    assert [e["title"] for e in audio["entries"]] == ["Song"] and audio["total"] == 1
    everything = (await client.get("/api/history")).json()
    assert everything["total"] == 6 and len(everything["entries"]) == 6


@pytest.mark.parametrize("params", [
    {"limit": 0}, {"limit": 501}, {"limit": "x"}, {"offset": -1}, {"kind": "movie"}, {"search": "x" * 501},
    {"offset": 2**63}, {"offset": "9" * 5000}, {"limit": 2**64}, {"offset": "1_0"}, {"offset": " 1"},
])
async def test_bad_history_queries_are_refused(client, params):
    assert error_of(await client.get("/api/history", params=params), 400)["kind"] == "bad_request"


async def test_the_largest_offset_is_an_empty_page(client):
    assert (await client.get("/api/history", params={"offset": 2**63 - 1})).json() == {"entries": [], "total": 0}


@pytest.mark.parametrize("method, path", [
    ("DELETE", "/api/history/9223372036854775808"),
    ("POST", "/api/history/99999999999999999999999/open-folder"),
    ("DELETE", "/api/history/-9223372036854775809"),
    ("DELETE", f"/api/history/{'9' * 5000}"),
    ("POST", "/api/history/1_0/open-folder"),
])
async def test_history_ids_too_big_for_the_database_are_not_found(client, method, path):
    assert error_of(await client.request(method, path), 404)["kind"] == "not_found"


async def test_history_delete_and_clear(client, core, tmp_path):
    first = core.history.add(finished_job(tmp_path / "out", video_id="d1"))
    core.history.add(finished_job(tmp_path / "out", video_id="d2"))
    assert (await client.delete(f"/api/history/{first}")).json() == {"deleted": 1}
    assert error_of(await client.delete(f"/api/history/{first}"), 404)
    assert error_of(await client.delete("/api/history/abc"), 404)
    assert (await client.delete("/api/history")).json() == {"deleted": 1}
    assert core.history.count() == 0
    assert (tmp_path / "out" / "Fake Video.mp4").is_file()  # files stay


async def test_open_folder_selects_the_file(client, core, platform, tmp_path):
    entry = core.history.add(finished_job(tmp_path / "out", video_id="o1"))
    response = await client.post(f"/api/history/{entry}/open-folder")
    assert response.status_code == 200
    assert platform.opened == [tmp_path / "out" / "Fake Video.mp4"]


async def test_open_folder_falls_back_to_the_folder(client, core, platform, tmp_path):
    (tmp_path / "out").mkdir()
    entry = core.history.add(finished_job(tmp_path / "out", video_id="o2", write=False))
    await client.post(f"/api/history/{entry}/open-folder")
    assert platform.opened == [tmp_path / "out"]


async def test_open_folder_of_a_missing_folder_is_not_found(client, core, platform, tmp_path):
    entry = core.history.add(finished_job(tmp_path / "gone", video_id="o3", write=False))
    error = error_of(await client.post(f"/api/history/{entry}/open-folder"), 404)
    assert str(tmp_path / "gone") in error["message"]
    failed = core.history.add(finished_job(tmp_path / "out", video_id="o4", state=JobState.FAILED))
    assert error_of(await client.post(f"/api/history/{failed}/open-folder"), 404)
    assert error_of(await client.post("/api/history/999999/open-folder"), 404)
    assert platform.opened == []


# ----- settings -----

async def test_get_settings(client, snag_defaults):
    settings = (await client.get("/api/settings")).json()
    assert settings["video_dir"] == snag_defaults.video_dir and settings["max_concurrent"] == 5
    assert settings["remembered_dirs"] == []


async def test_a_max_concurrent_change_reaches_the_queue(client, core, platform):
    response = await client.put("/api/settings", json={"max_concurrent": 2})
    assert response.status_code == 200 and response.json()["max_concurrent"] == 2
    assert core.queue.state.current_limit == 2
    assert platform.startup == []  # untouched


async def test_invalid_settings_are_refused_and_nothing_is_saved(client, core):
    error = error_of(await client.put("/api/settings", json={"max_concurrent": 2, "video_dir": "relative"}), 400)
    assert "video_dir" in error["message"]
    assert core.settings.get().max_concurrent == 5
    assert error_of(await client.put("/api/settings", json={"nonsense": 1}), 400)
    assert error_of(await client.put("/api/settings"), 400)


async def test_start_with_windows_adds_and_removes_the_shortcut(client, core, platform):
    assert (await client.put("/api/settings", json={"start_with_windows": False})).json()["start_with_windows"] is False
    await client.put("/api/settings", json={"start_with_windows": False})  # no change: nothing to do
    assert (await client.put("/api/settings", json={"start_with_windows": True})).json()["start_with_windows"] is True
    assert platform.startup == [False, True]


async def test_start_with_windows_is_what_windows_will_do(client, core, platform):
    """Not a stored value: the Startup shortcut, there and not disabled in Task Manager, read afresh."""
    assert (await client.get("/api/settings")).json()["start_with_windows"] is True
    platform.started = False  # the user deleted the shortcut, or disabled it in Task Manager
    assert (await client.get("/api/settings")).json()["start_with_windows"] is False
    with core.conn.lock:  # what an older Snag stored is ignored
        core.conn.execute("INSERT INTO settings(key, value) VALUES('start_with_windows', 'true')")
    assert (await client.get("/api/settings")).json()["start_with_windows"] is False


async def test_turning_on_a_shortcut_windows_skips_makes_it_again(client, platform):
    """Disabled in Task Manager (or deleted) while the setting looked on: the toggle reads off, and
    turning it on makes the shortcut again, clearing Task Manager's mark (set_start_with_windows)."""
    platform.started = False
    assert (await client.put("/api/settings", json={"start_with_windows": True})).json()["start_with_windows"] is True
    assert platform.startup == [True]


async def test_start_with_windows_must_be_true_or_false(client, platform):
    error = error_of(await client.put("/api/settings", json={"start_with_windows": "yes", "max_concurrent": 2}), 400)
    assert "start_with_windows" in error["message"]
    assert platform.startup == []


async def test_an_unreadable_startup_state_reads_as_off(client, platform, caplog):
    platform.started_error = OSError("registry says no")
    with caplog.at_level("WARNING", logger="snag.server.routes.settings"):
        assert (await client.get("/api/settings")).json()["start_with_windows"] is False
    assert "registry says no" in caplog.text


async def test_a_shortcut_failure_saves_nothing(client, core, platform):
    events = []
    core.subscribe_events(events.append)
    platform.startup_error = OSError("access denied")
    error = error_of(await client.put("/api/settings", json={"start_with_windows": False, "max_concurrent": 3}), 500)
    assert error["message"] == "Couldn't remove Snag from the Startup folder: access denied"
    assert error["details"] == ""
    settings = core.settings.get()
    assert platform.started is True and settings.max_concurrent == 5
    assert (await client.get("/api/settings")).json()["start_with_windows"] is True
    assert core.queue.state.current_limit == 5
    assert [e for e in events if e["type"] == "settings"] == []


async def test_a_change_is_announced_to_every_page(client, core, app, tmp_path):
    session = WsSession(app)
    assert await session.accepted()
    assert (await session.receive_json())["type"] == "snapshot"
    await client.put("/api/settings", json={"max_concurrent": 2})
    while (event := await session.receive_json())["type"] != "settings":
        assert event["type"] == "queue"  # the new limit
    assert event == {"type": "settings", "settings": (await client.get("/api/settings")).json()}
    assert event["settings"]["max_concurrent"] == 2
    await client.put("/api/settings", json={"max_concurrent": 2})  # no change: nothing to announce
    picked = tmp_path / "Picked"
    core.queue.pause()
    await client.post("/api/jobs", json={"url": "fake://downloadable/a", "format_id": "299", "dest_dir": str(picked)})
    while (event := await session.receive_json())["type"] != "settings":
        pass
    assert event["settings"]["remembered_dirs"] == [str(picked)] and event["settings"]["max_concurrent"] == 2
    assert event["settings"]["start_with_windows"] is True
    await client.put("/api/settings", json={"start_with_windows": False})  # only the shortcut changes
    while (event := await session.receive_json())["type"] != "settings":
        pass
    assert event["settings"]["start_with_windows"] is False
    await session.disconnect()


async def test_the_snapshot_says_whether_windows_will_start_snag(app, platform):
    platform.started = False
    session = WsSession(app)
    assert await session.accepted()
    snapshot = await session.receive_json()
    assert snapshot["type"] == "snapshot" and snapshot["settings"]["start_with_windows"] is False
    await session.disconnect()


async def test_settings_changes_wait_for_each_other(client, core, platform):
    entered, release = threading.Event(), threading.Event()

    def slow_shortcut(enabled):
        entered.set()
        release.wait(10)
        platform.startup.append(enabled)
        platform.started = enabled

    platform.set_start_with_windows = slow_shortcut
    app = create_app(core, platform=platform.hooks())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE, headers=TAB) as slow:
        first = asyncio.create_task(slow.put("/api/settings", json={"start_with_windows": False}))
        try:
            await wait_for(entered.is_set)
            second = asyncio.create_task(slow.put("/api/settings", json={"max_concurrent": 2}))
            await asyncio.sleep(0.3)
            assert not second.done() and core.settings.get().max_concurrent == 5
        finally:
            release.set()
        assert (await first).status_code == 200 and (await second).status_code == 200
    assert platform.started is False and core.settings.get().max_concurrent == 2


async def test_folder_settings_take_network_shares_and_refuse_devices(client, core):
    share = "\\\\nas\\media\\Videos"
    assert (await client.put("/api/settings", json={"video_dir": share})).json()["video_dir"] == share
    error = error_of(await client.put("/api/settings", json={"audio_dir": "\\\\.\\C:\\Music"}), 400)
    assert "device" in error["message"]
    assert core.settings.get().audio_dir != "\\\\.\\C:\\Music"


@pytest.mark.parametrize("extras, message", [
    ({"trim_start": "5", "trim_end": "4"}, "The trim end must come after the start."),
    ({"trim_start": "soon"}, "Trim times look like 1:30 or 1:02:03."),
    ({"sponsorblock": True, "sponsorblock_categories": []}, "Pick at least one SponsorBlock category."),
    ({"chapters": True, "loudness": 1}, "Snag doesn't know these extras: loudness."),
])
async def test_invalid_extras_read_the_same_everywhere(client, core, extras, message):
    requests = [
        ("POST", "/api/jobs", {"url": "fake://downloadable/x", "format_id": "299", "extras": extras}),
        ("POST", "/api/jobs/batch", {"url": "fake://fakelist", "entries": [1], "quality": {"kind": "mp3"},
                                     "extras": extras}),
        ("PUT", "/api/settings", {"quick_extras": extras}),
        ("PUT", "/api/settings", {"max_concurrent": 2, "default_extras": extras}),
    ]
    for method, path, body in requests:
        error = error_of(await client.request(method, path, json=body), 400)
        assert (error["kind"], error["action"], error["message"]) == ("invalid_extras", "edit_extras", message)
        assert error["details"]
    assert core.queue.jobs == [] and core.settings.get().max_concurrent == 5


# ----- cookies, status, folders -----

async def test_pushed_cookies_retry_sign_in_failures(client, core, tmp_path):
    job = core.queue.add(make_spec("fake://signin/s1", tmp_path / "out"))
    other = core.queue.add(make_spec("fake://unavailable/s2", tmp_path / "out"))
    await wait_for(lambda: job.state is JobState.FAILED and other.state is JobState.FAILED)
    assert job.error.kind is ErrorKind.SIGN_IN
    response = await client.post("/api/cookies", headers=EXTENSION, json={
        "url": "fake://signin/s1", "cookies": [{"domain": "signin", "name": "SID", "value": "v", "hostOnly": True}],
    })
    assert response.json() == {"retried": 1}
    await wait_for(lambda: job.state is JobState.DONE)
    assert other.state is JobState.FAILED


async def test_youtube_cookies_cover_the_whole_site(client, core, tmp_path):
    job = core.queue.add(make_spec("https://music.youtube.com/watch?v=x", tmp_path / "out"))
    await wait_for(lambda: job.state is JobState.FAILED)
    job.error = make_error(ErrorKind.SIGN_IN, "youtube")  # as if it had needed signing in
    core.queue.pause()  # keep the retried job from running against the fake
    response = await client.post("/api/cookies", json={
        "url": "https://www.youtube.com/", "cookies": [{"domain": ".youtube.com", "name": "SID", "value": "v"}],
    })
    assert response.json() == {"retried": 1}


async def test_cookies_pushed_for_a_short_link_cover_the_site(client, core, tmp_path):
    job = core.queue.add(make_spec("https://www.youtube.com/watch?v=y", tmp_path / "out"))
    await wait_for(lambda: job.state is JobState.FAILED)
    job.error = make_error(ErrorKind.SIGN_IN, "youtube")
    core.queue.pause()
    response = await client.post("/api/cookies", json={
        "url": "https://youtu.be/y", "cookies": [{"domain": ".youtube.com", "name": "SID", "value": "v"}],
    })
    assert response.json() == {"retried": 1}


async def test_youtube_cookies_update_the_login_status(client, core, app):
    session = WsSession(app)
    assert await session.accepted()
    assert (await session.receive_json())["status"]["youtube_login"] is None
    await client.post("/api/cookies", json={
        "url": "https://www.youtube.com/", "cookies": [{"domain": ".youtube.com", "name": "SID", "value": "v"}],
    })
    event = await session.receive_json()
    assert event["type"] == "status" and event["youtube_login"].endswith("Z")
    assert (await client.get("/api/status")).json()["youtube_login"] == event["youtube_login"]
    await session.disconnect()


@pytest.mark.parametrize("body", [{"url": "fake://x"}, {"cookies": []}, {"url": "fake://x", "cookies": {}}])
async def test_bad_cookie_pushes_are_refused(client, body):
    assert error_of(await client.post("/api/cookies", json=body), 400)


async def test_status(client, core):
    status = (await client.get("/api/status")).json()
    assert status == status_json(core)
    assert status["version"] == snag.__version__
    assert status["tools"]["ytdlp"]["present"] is True and status["tools"]["ffmpeg"]["message"] == "ffmpeg is missing"
    assert status["updater"]["setup"]["state"] == "idle"
    assert status["queue"]["paused"] is False and status["youtube_login"] is None


async def test_pick_folder_runs_the_dialog_off_the_loop(client, platform, tmp_path):
    platform.answer = str(tmp_path)
    response = await client.post("/api/pick-folder", json={"initial": "C:\\Videos"})
    assert response.json() == {"path": str(tmp_path)}
    assert platform.picked == ["C:\\Videos"]
    assert platform.pick_threads[0] != threading.get_ident()
    platform.answer = None
    assert (await client.post("/api/pick-folder")).json() == {"path": None}
    assert platform.picked[-1] is None
    assert error_of(await client.post("/api/pick-folder", json={"initial": 5}), 400)


# ----- updates and setup -----

async def test_check_for_update(client, github):
    publish_ytdlp(github, GOOD + b"2026.09.30 build")
    status = (await client.post("/api/update/check")).json()
    assert status["updater"]["update"]["available"] == "2026.09.30"
    assert status["updater"]["update"]["current"] == "2026.08.19"


def block(core, monkeypatch, name: str) -> tuple[asyncio.Event, asyncio.Event, list]:
    """Make core.updater.<name> wait until released: (started, release, calls)."""
    started, release, calls = asyncio.Event(), asyncio.Event(), []

    async def blocked(*args):
        calls.append(args)
        started.set()
        await release.wait()
        return {}

    monkeypatch.setattr(core.updater, name, blocked)
    return started, release, calls


@pytest.mark.parametrize("path, method, args", [
    ("/api/update/install", "install_update", ()),
    ("/api/tools/ffmpeg/reinstall", "reinstall", ("ffmpeg",)),
    ("/api/tools/deno/reinstall", "reinstall", ("deno",)),
    ("/api/setup/retry", "ensure_installed", ()),
])
async def test_long_updater_work_starts_in_the_background(client, core, monkeypatch, path, method, args):
    started, release, calls = block(core, monkeypatch, method)
    response = await asyncio.wait_for(client.post(path), 2)
    assert response.status_code == 202
    body = response.json()
    assert body["started"] is True and body["status"] == status_json(core)
    await asyncio.wait_for(started.wait(), 2)
    again = await asyncio.wait_for(client.post(path), 2)
    assert again.status_code == 202 and again.json()["started"] is False  # already running
    assert calls == [args]
    events = []
    core.subscribe_events(events.append)
    release.set()
    await wait_for(lambda: any("tools" in e for e in events))  # the checks ran again afterwards


async def test_only_ffmpeg_and_deno_can_be_reinstalled(client):
    assert error_of(await client.post("/api/tools/yt-dlp/reinstall"), 404)["kind"] == "not_found"


async def test_a_running_install_is_cancelled_at_shutdown(client, core, monkeypatch):
    started, release, _ = block(core, monkeypatch, "install_update")
    await client.post("/api/update/install")
    await asyncio.wait_for(started.wait(), 2)
    await asyncio.wait_for(core.close(), 5)  # doesn't wait for release


# ----- the WebSocket, in process -----

async def test_the_websocket_sends_a_snapshot_then_job_events(app, core, tmp_path):
    session = WsSession(app, headers=EXTENSION)
    assert await session.accepted()
    snapshot = await session.receive_json()
    assert snapshot["type"] == "snapshot" and snapshot["jobs"] == [] and snapshot["status"] == status_json(core)
    job = core.queue.add(make_spec("fake://success/w1", tmp_path / "out"))
    seen = []
    while not seen or seen[-1].get("job", {}).get("state") != "done":
        event = await session.receive_json()
        if event["type"] == "job":
            seen.append(event)
    assert seen[-1]["job"] == job_json(job)
    await session.disconnect()


# ----- wiring -----

def test_the_default_platform_is_windows():
    from snag.platform import folders, startup

    hooks = Platform.windows()
    assert hooks.pick_folder is folders.pick_folder
    assert hooks.open_in_explorer is folders.open_in_explorer
    assert hooks.set_start_with_windows is startup.set_start_with_windows
    assert hooks.starts_with_windows is startup.starts_with_windows
    from snag.platform import peer

    assert hooks.same_user_peer is peer.same_user_peer


async def test_every_route_refuses_another_windows_users_programs(core, platform):
    platform.same_user_peer = lambda peer, server: False
    app = create_app(core, platform=platform.hooks())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE, headers=TAB) as other:
        for path in ("/api/status", "/api/settings", "/"):
            answer = await other.get(path)
            assert answer.status_code == 403 and answer.json()["error"]["kind"] == "forbidden", path


async def test_an_unexpected_failure_is_a_plain_500_with_the_guards_headers(client, core, monkeypatch, caplog):
    import logging

    def broken(*args, **kwargs):
        raise RuntimeError("a bug")

    monkeypatch.setattr(core.history, "count", broken)
    with caplog.at_level(logging.ERROR):
        response = await client.get("/api/history", headers=EXTENSION)
    error = error_of(response, 500)
    assert error["kind"] == "unknown" and error["message"] == "Something went wrong in Snag."
    assert error["details"] == "RuntimeError: a bug"
    assert response.headers["access-control-allow-origin"] == EXTENSION["origin"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert len([record for record in caplog.records if record.exc_info]) == 1
    assert "Traceback" in caplog.text and "a bug" in caplog.text
