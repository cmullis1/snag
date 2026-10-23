"""Snag itself: cookies from the browser, status, the folder dialog, updates and setup.

POST /api/cookies  {url, cookies: [Chrome cookie objects]}  -> {retried: n}
     Keeps the cookies (in memory) for the URL's site and retries every download that failed for
     want of signing in to that site.
GET  /api/status                       -> Status (hub.status_json)
POST /api/pick-folder  {initial?}      -> {path: str | null}   null when the dialog was cancelled
POST /api/update/check                 -> Status, after asking GitHub for the latest yt-dlp
POST /api/update/install               -> 202 {started, status}
POST /api/tools/{ffmpeg|deno}/reinstall -> 202 {started, status}; any other tool -> 404
POST /api/setup/retry                  -> 202 {started, status}   first-run setup again
     These three wait for running downloads before swapping a tool, so they run in the background
     (started is false when that work is already running) and report through the hub's setup,
     update and status events."""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter
from starlette.requests import Request
from starlette.responses import JSONResponse

from snag.cookies import canonical_host
from snag.server.hub import status_json
from snag.server.routes import bad_request, json_body, not_found
from snag.server.routes.lookup import core_of, parse_cookies, store_cookies, url_of
from snag.updater.service import REINSTALLABLE

router = APIRouter()

SETUP = "setup"  # background task names (SnagCore.start_background); snag/__main__ starts setup with it
UPDATE = "update"


def _domain_matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith(f".{domain}")


def cookie_site(url: str, cookies: list[dict]) -> str:
    """The widest host the pushed cookies cover for url: a ".youtube.com" cookie pushed for
    www.youtube.com covers music.youtube.com too. The URL's own host when none is wider. The host is
    the one the cookie jar files the cookies under (youtu.be's are www.youtube.com's)."""
    host = canonical_host(url)
    domains = {str(cookie["domain"]).lower().strip(".") for cookie in cookies}
    covering = [domain for domain in domains if domain and _domain_matches(host, domain) and "." in domain]
    return min(covering, key=len) if covering else host


@router.post("/api/cookies")
async def push_cookies(request: Request) -> dict[str, Any]:
    core = core_of(request)
    body = await json_body(request)
    url = url_of(body)
    if "cookies" not in body:
        raise bad_request("cookies is missing.")
    cookies = parse_cookies(body["cookies"])
    store_cookies(core, url, cookies)
    if not cookies:
        return {"retried": 0}
    retried = core.queue.retry_signin_failures(cookie_site(url, cookies))
    return {"retried": len(retried)}


@router.get("/api/status")
async def status(request: Request) -> dict[str, Any]:
    return status_json(core_of(request))


@router.post("/api/pick-folder")
async def pick_folder(request: Request) -> dict[str, Any]:
    body = await json_body(request, required=False)
    initial = body.get("initial")
    if initial is not None and not isinstance(initial, str):
        raise bad_request("initial must be a folder path.")
    # The dialog blocks until the user answers; the picker runs it on its own thread.
    path = await asyncio.to_thread(request.app.state.platform.pick_folder, initial)
    return {"path": path}


@router.post("/api/update/check")
async def check_for_update(request: Request) -> dict[str, Any]:
    core = core_of(request)
    await core.updater.check_for_update()
    return status_json(core)


def _started(core, started: bool) -> JSONResponse:
    return JSONResponse({"started": started, "status": status_json(core)}, status_code=202)


@router.post("/api/update/install")
async def install_update(request: Request) -> JSONResponse:
    core = core_of(request)
    return _started(core, core.start_background(UPDATE, core.install_update))


@router.post("/api/tools/{tool}/reinstall")
async def reinstall(tool: str, request: Request) -> JSONResponse:
    core = core_of(request)
    if tool not in REINSTALLABLE:
        raise not_found(f"Only {' and '.join(REINSTALLABLE)} can be reinstalled.")
    return _started(core, core.start_background(f"reinstall-{tool}", lambda: core.reinstall_tool(tool)))


@router.post("/api/setup/retry")
async def retry_setup(request: Request) -> JSONResponse:
    core = core_of(request)
    return _started(core, core.start_background(SETUP, core.setup_tools))
