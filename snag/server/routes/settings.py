"""Settings (spec 4.7): the plain JSON of snag.store.settings.settings_to_json, plus
start_with_windows, which isn't stored: it is whether Windows will start Snag, read afresh from
Windows each time (the Startup shortcut is there and Task Manager hasn't disabled it; the
platform's starts_with_windows, snag.platform.startup). A failed read is logged and reads as false.

GET /api/settings               -> Settings
PUT /api/settings  {field: value, ...}   only the fields to change -> Settings
    Every change is validated first and saved together; an invalid one -> 400 and nothing changes
    (invalid quick_extras or default_extras -> 400 invalid_extras, worded as for a download).
    start_with_windows, when it differs from what Windows will do, adds or removes the Startup
    shortcut before anything is saved: if Windows refuses, nothing is saved and the answer is 500
    with why. max_concurrent reaches the queue at once.

Changes are made one at a time (settings_lock), PUTs and the destination folders the download routes
remember alike, and each change that alters a setting is announced to every page through the hub:
    {"type": "settings", "settings": Settings}"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter
from starlette.requests import Request

from snag.server.routes import ApiError, bad_request, invalid_extras, json_body
from snag.server.routes.lookup import core_of
from snag.server.schemas import InvalidExtras, parse_extras
from snag.store.settings import Settings, remember_dir, settings_from_json, settings_to_json, validate_changes

if TYPE_CHECKING:
    from snag.app import SnagCore
    from snag.server.app import Platform

log = logging.getLogger(__name__)

router = APIRouter()

EXTRAS_SETTINGS = ("quick_extras", "default_extras")
START_WITH_WINDOWS = "start_with_windows"


def settings_lock(request: Request) -> asyncio.Lock:
    return request.app.state.settings_lock


def starts_with_windows(platform: Platform) -> bool:
    """Whether Windows will start Snag (see the module docstring). Blocking but cheap (a file check
    and a registry read); false, logged, when it can't be read."""
    try:
        return bool(platform.starts_with_windows())
    except OSError as error:
        log.warning("Couldn't tell whether Snag starts with Windows: %s", error)
        return False


def settings_json(settings: Settings, platform: Platform) -> dict[str, Any]:
    """GET /api/settings' object: the stored settings and start_with_windows (blocking, see above)."""
    return {**settings_to_json(settings), START_WITH_WINDOWS: starts_with_windows(platform)}


def announce(core: SnagCore, settings: dict[str, Any]) -> None:
    """Tell every page the settings changed (settings: settings_json's; the hub forwards core's events)."""
    core.emit({"type": "settings", "settings": settings})


async def change_settings(request: Request, change: Callable[[], Settings]) -> Settings:
    """Run change (which saves settings and returns them) off the loop, under settings_lock, and
    announce the result if it differs from before."""
    core = core_of(request)
    platform = request.app.state.platform
    async with settings_lock(request):
        before = await asyncio.to_thread(core.settings.get)
        after = await asyncio.to_thread(change)
    if after != before:
        announce(core, await asyncio.to_thread(settings_json, after, platform))
    return after


async def remember_folder(request: Request, folder: str) -> None:
    """Put a destination folder the user picked first in remembered_dirs (remember_dir), the folders
    the startup sweep looks in for leftover working folders."""
    core = core_of(request)
    await change_settings(request, lambda: remember_dir(core.settings, folder))


@router.get("/api/settings")
async def get_settings(request: Request) -> dict[str, Any]:
    core, platform = core_of(request), request.app.state.platform
    return await asyncio.to_thread(lambda: settings_json(core.settings.get(), platform))


def _startup_wanted(body: Any) -> tuple[Any, bool | None]:
    """body without start_with_windows, and its value (None when it isn't there). Raises ApiError
    (400) when it isn't true or false."""
    if not isinstance(body, dict) or START_WITH_WINDOWS not in body:
        return body, None
    rest = dict(body)
    wanted = rest.pop(START_WITH_WINDOWS)
    if not isinstance(wanted, bool):
        raise bad_request(f"That setting isn't valid: {START_WITH_WINDOWS}: must be true or false")
    return rest, wanted


def _changes(body: dict[str, Any]) -> dict[str, Any]:
    """The body as checked changes; raises ApiError (400) for anything invalid."""
    for key in EXTRAS_SETTINGS:
        if key in body:
            try:
                parse_extras(body[key])
            except InvalidExtras as problem:
                raise invalid_extras(problem.message, f"{key}: {problem.details}") from None
    try:
        changes = settings_from_json(body)
        validate_changes(changes)
    except ValueError as error:
        raise bad_request(f"That setting isn't valid: {error}") from None
    return changes


@router.put("/api/settings")
async def put_settings(request: Request) -> dict[str, Any]:
    core = core_of(request)
    body, wanted = _startup_wanted(await json_body(request))
    changes = _changes(body)
    platform = request.app.state.platform
    async with settings_lock(request):
        before = await asyncio.to_thread(core.settings.get)
        was = await asyncio.to_thread(starts_with_windows, platform)
        switch = wanted is not None and wanted != was
        if switch:
            try:
                await asyncio.to_thread(platform.set_start_with_windows, wanted)
            except OSError as error:
                log.warning("Couldn't change the Startup shortcut: %s", error)
                action = "add Snag to" if wanted else "remove Snag from"
                raise ApiError(500, "unknown", f"Couldn't {action} the Startup folder: {error}") from None
        try:
            after = await asyncio.to_thread(lambda: core.settings.update(**changes)) if changes else before
        except Exception:
            if switch:  # put the shortcut back as it was
                try:
                    await asyncio.to_thread(platform.set_start_with_windows, was)
                except OSError as error:
                    log.warning("Couldn't put the Startup shortcut back: %s", error)
            raise
        answer = await asyncio.to_thread(settings_json, after, platform)
    if after.max_concurrent != before.max_concurrent:
        core.queue.set_max_concurrent(after.max_concurrent)
    if after != before or answer[START_WITH_WINDOWS] != was:
        announce(core, answer)
    return answer
