"""History (spec 4.6). Deleting entries never touches the downloaded files.

GET    /api/history?search=&kind=&limit=&offset=  -> {entries: [HistoryEntry], total}
       newest first; search matches titles; kind "video" or "audio"; limit 1-500 (100); total counts
       every entry the search and kind match, for paging; offset 0 to 2**63-1
DELETE /api/history/{id}                          -> {deleted: 1}; unknown id -> 404
DELETE /api/history                               -> {deleted: n}
POST   /api/history/{id}/open-folder              -> {opened: path}
       Explorer with the file selected; a file that's gone opens its folder; a folder that's gone
       (or a failed download, which has no file) -> 404

The database calls run off the event loop (asyncio.to_thread), like Settings': a slow disk must not
hold up the WebSocket and the other requests."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from starlette.requests import Request

from snag.server.routes import MAX_INT, bad_request, not_found, whole_number
from snag.server.routes.lookup import core_of
from snag.server.schemas import history_entry_json

router = APIRouter()

DEFAULT_LIMIT = 100
MAX_LIMIT = 500
MAX_SEARCH = 500
KINDS = ("video", "audio")


def _whole_number(request: Request, name: str, default: int, low: int, high: int = MAX_INT) -> int:
    raw = request.query_params.get(name)
    if raw is None or raw == "":
        return default
    value = whole_number(raw)
    if value is None or not low <= value <= high:
        raise bad_request(f"{name} must be a whole number from {low} to {high}.")
    return value


def _entry_id(text: str) -> int:
    """A History entry's id from the path; anything that can't be one (too big for SQLite too) -> 404."""
    value = whole_number(text)
    if value is None:
        raise not_found("There's no such History entry.")
    return value


@router.get("/api/history")
async def list_history(request: Request) -> dict[str, Any]:
    history = core_of(request).history
    search = request.query_params.get("search", "")
    if len(search) > MAX_SEARCH:
        raise bad_request("That search is too long.")
    kind = request.query_params.get("kind") or None
    if kind is not None and kind not in KINDS:
        raise bad_request("kind must be video or audio.")
    limit = _whole_number(request, "limit", DEFAULT_LIMIT, 1, MAX_LIMIT)
    offset = _whole_number(request, "offset", 0, 0)

    def read() -> dict[str, Any]:
        entries = history.list(search, kind, limit, offset)
        return {"entries": [history_entry_json(entry) for entry in entries], "total": history.count(search, kind)}

    return await asyncio.to_thread(read)


@router.delete("/api/history")
async def clear_history(request: Request) -> dict[str, Any]:
    return {"deleted": await asyncio.to_thread(core_of(request).history.clear)}


@router.delete("/api/history/{entry_id}")
async def delete_entry(entry_id: str, request: Request) -> dict[str, Any]:
    if not await asyncio.to_thread(core_of(request).history.delete, _entry_id(entry_id)):
        raise not_found("There's no such History entry.")
    return {"deleted": 1}


def _target(file_path: str | None) -> Path:
    """The file to select, or the folder to open when the file is gone; raises 404 otherwise."""
    if not file_path:
        raise not_found("This download has no file to show.")
    file = Path(file_path)
    if file.is_file():
        return file
    if file.parent.is_dir():
        return file.parent
    raise not_found(f"The folder {file.parent} isn't there anymore.")


@router.post("/api/history/{entry_id}/open-folder")
async def open_folder(entry_id: str, request: Request) -> dict[str, Any]:
    entry = await asyncio.to_thread(core_of(request).history.get, _entry_id(entry_id))
    if entry is None:
        raise not_found("There's no such History entry.")
    # Off the loop: the checks may touch a slow or sleeping drive, and Explorer takes a moment to start.
    target = await asyncio.to_thread(_target, entry.file_path)
    try:
        await asyncio.to_thread(request.app.state.platform.open_in_explorer, target)
    except FileNotFoundError:  # gone between the check and Explorer
        raise not_found(f"{target} isn't there anymore.") from None
    return {"opened": str(target)}
