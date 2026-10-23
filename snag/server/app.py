"""create_app(core): the FastAPI app Snag serves on 127.0.0.1:<port>: the JSON API (snag/server/routes),
the WebSocket hub at /ws, the full app tab's files (/ -> web/index.html, /web/*, /ui/*), all behind
the guard (same Windows user, Host, Origin), which also answers unexpected errors (snag.server.guard).

Call it after `await core.open()`, on the loop that runs core: the hub subscribes to core's queue and
events at once. The platform hooks (folder dialog, Explorer, Startup shortcut, the caller's Windows
user) are injectable so tests never open a window or make a shortcut."""
from __future__ import annotations

import asyncio
import mimetypes
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, WebSocket
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.staticfiles import StaticFiles

from snag.server.guard import Guard
from snag.server.hub import Hub
from snag.server.routes import ApiError, error_response
from snag.server.routes import history, jobs, lookup, settings, system

if TYPE_CHECKING:
    from snag.app import SnagCore

REPO_ROOT = Path(__file__).resolve().parents[2]

HTTP_MESSAGES = {404: "There's nothing here.", 405: "That action isn't allowed here."}

# Windows takes these from the registry, where another program may have set .js to text/plain; with
# nosniff a browser then refuses the UI's modules. Fix them for this process.
_TYPES = {".js": "text/javascript", ".mjs": "text/javascript", ".css": "text/css", ".html": "text/html",
          ".json": "application/json", ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon"}


@dataclass(frozen=True)
class Platform:
    """What the API asks of Windows. Each runs off the event loop (asyncio.to_thread)."""

    pick_folder: Callable[[str | None], str | None]  # the native folder dialog; None if cancelled
    open_in_explorer: Callable[[Path], None]  # a file selected in its folder, or a folder
    set_start_with_windows: Callable[[bool], None]  # add or remove the Startup shortcut
    # Whether Windows will start Snag: the Startup shortcut is there and not disabled in Task
    # Manager. Cheap; the WebSocket snapshot reads it on the loop.
    starts_with_windows: Callable[[], bool]
    # (client, server) -> whether the caller's process runs as Snag's Windows user; OSError when it
    # can't be told. The guard asks it, on the loop (cheap, and cached per client address).
    same_user_peer: Callable[[tuple[str, int], tuple[str, int]], bool]

    @classmethod
    def windows(cls) -> Platform:
        from snag.platform.folders import open_in_explorer, pick_folder
        from snag.platform.peer import same_user_peer
        from snag.platform.startup import set_start_with_windows, starts_with_windows

        return cls(pick_folder=pick_folder, open_in_explorer=open_in_explorer,
                   set_start_with_windows=set_start_with_windows, starts_with_windows=starts_with_windows,
                   same_user_peer=same_user_peer)


class _Files(StaticFiles):
    """Static files the browser revalidates every time, so a new UI shows after Snag restarts."""

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


def create_app(core: SnagCore, *, platform: Platform | None = None, root: Path = REPO_ROOT,
               hub: Hub | None = None) -> FastAPI:
    """root: the folder holding web/ and ui/. hub: by default Hub.for_core(core), its snapshot's
    settings including start_with_windows (routes/settings.py)."""
    for suffix, media_type in _TYPES.items():
        mimetypes.add_type(media_type, suffix)
    platform = platform or Platform.windows()
    hub = hub or Hub.for_core(core, settings_json=lambda stored: settings.settings_json(stored, platform))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        await hub.close()

    app = FastAPI(title="Snag", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.core = core
    app.state.hub = hub
    app.state.platform = platform
    app.state.settings_lock = asyncio.Lock()  # settings changes, one at a time (routes/settings.py)

    @app.exception_handler(ApiError)
    async def api_error(request: Request, error: ApiError) -> JSONResponse:
        return error.response()

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException) -> JSONResponse:
        kind = "not_found" if error.status_code == 404 else "bad_request"
        message = HTTP_MESSAGES.get(error.status_code, str(error.detail))
        return error_response(error.status_code, kind, message, headers=getattr(error, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError) -> JSONResponse:
        return error_response(400, "bad_request", "The request isn't valid.", details=str(error.errors()))

    # Every other exception goes on to the guard, which logs it and answers the JSON 500 inside itself,
    # so that answer carries its headers (CORS for the extension, nosniff, no-store).

    for module in (lookup, jobs, history, settings, system):
        app.include_router(module.router)

    @app.websocket("/ws")
    async def events(websocket: WebSocket) -> None:
        await hub.serve(websocket)

    index = root / "web" / "index.html"

    @app.get("/", include_in_schema=False)
    async def app_tab() -> FileResponse:
        return FileResponse(index, media_type="text/html", headers={"Cache-Control": "no-cache"})

    app.mount("/web", _Files(directory=root / "web", check_dir=True), name="web")
    app.mount("/ui", _Files(directory=root / "ui", check_dir=True), name="ui")
    app.add_middleware(Guard, port=core.port, same_user=platform.same_user_peer)
    return app
