"""The API's routes (one module per area) and what they share: errors as JSON, request bodies.

Every error the API returns is {"error": {...}} with a 4xx/5xx status. The object has the fields of
snag.errors.ErrorInfo.to_dict (kind, message, action, can_retry, keep_partial, details): a lookup's
or a job's own ErrorInfo where there is one (error_json), otherwise one of these kinds:
  bad_request     the request isn't valid (400, or 405 for a method a path doesn't take); message
                  says what to change
  invalid_extras  the download extras (trim times, SponsorBlock, ...) aren't valid (400), from a
                  download, a playlist or the quick_extras/default_extras settings; action is
                  "edit_extras" and message says which part to fix, e.g. "The trim end must come
                  after the start."
  forbidden       refused by the Host/Origin guard (403)
  not_found       no such job, batch, history entry or path (404)
  conflict        the job or batch can't do that now, e.g. retrying a running download (409)
  too_large       the request body is over MAX_BODY bytes (413)
  unknown         Snag failed unexpectedly (500); details is "<error type>: <message>"
message is plain text for the user. action is null except where a kind above names one."""
from __future__ import annotations

import json
import re
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from snag.errors import ErrorInfo
from snag.server.schemas import error_json


MAX_BODY = 16 * 1024 * 1024  # bytes a request body may have
MAX_INT = 2**63 - 1  # the largest whole number SQLite stores; ids and offsets above it can't be real
INVALID_EXTRAS = "invalid_extras"
EDIT_EXTRAS = "edit_extras"


def error_body(kind: str, message: str, *, details: str = "", action: str | None = None) -> dict[str, Any]:
    return {"error": {
        "kind": kind, "message": message, "action": action, "can_retry": False, "keep_partial": False,
        "details": details,
    }}


def error_response(status: int, kind: str, message: str, *, details: str = "", action: str | None = None,
                   headers=None) -> JSONResponse:
    return JSONResponse(error_body(kind, message, details=details, action=action), status_code=status,
                        headers=headers)


class ApiError(Exception):
    """Raised by a route to answer with an error (see the module docstring)."""

    def __init__(self, status: int, kind: str, message: str, *, error: ErrorInfo | None = None,
                 details: str = "", action: str | None = None):
        super().__init__(message)
        self.status = status
        self.kind = kind
        self.message = message
        self.error = error
        self.details = details
        self.action = action

    def response(self) -> JSONResponse:
        if self.error is not None:
            return JSONResponse({"error": error_json(self.error)}, status_code=self.status)
        return error_response(self.status, self.kind, self.message, details=self.details, action=self.action)


def bad_request(message: str) -> ApiError:
    return ApiError(400, "bad_request", message)


def not_found(message: str) -> ApiError:
    return ApiError(404, "not_found", message)


def conflict(message: str) -> ApiError:
    return ApiError(409, "conflict", message)


def invalid_extras(message: str, details: str = "") -> ApiError:
    return ApiError(400, INVALID_EXTRAS, message, details=details, action=EDIT_EXTRAS)


_DIGITS = re.compile(r"-?[0-9]{1,20}")


def whole_number(text: str) -> int | None:
    """text as a whole number SQLite can hold (-MAX_INT-1 to MAX_INT), or None if it isn't one. Only
    plain digits with an optional minus count (not "1_000", " 7" or "+7")."""
    if not _DIGITS.fullmatch(text):
        return None
    value = int(text)
    return value if -MAX_INT - 1 <= value <= MAX_INT else None


def sentence(text: str) -> str:
    """An engine message ("only failed ... can be retried") as a sentence for the user."""
    text = text.strip()
    if not text:
        return text
    text = text[0].upper() + text[1:]
    return text if text.endswith((".", "!", "?")) else f"{text}."


def too_large() -> ApiError:
    return ApiError(413, "too_large", f"That request is too big for Snag (over {MAX_BODY // (1024 * 1024)} MB).")


async def read_body(request: Request) -> bytes:
    """The request's body; over MAX_BODY bytes -> 413, without reading on past the limit."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_BODY:
        raise too_large()
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY:
            raise too_large()
        chunks.append(chunk)
    return b"".join(chunks)


async def json_body(request: Request, *, required: bool = True) -> dict[str, Any]:
    """The request's JSON object. Without required, an empty body is {}. A body over MAX_BODY -> 413."""
    raw = await read_body(request)
    if not raw.strip():
        if required:
            raise bad_request("The request needs a JSON body.")
        return {}
    try:
        body = json.loads(raw)
    except (ValueError, RecursionError):
        raise bad_request("The request body isn't valid JSON.") from None
    if not isinstance(body, dict):
        raise bad_request("The request body must be a JSON object.")
    return body


def text_field(body: dict[str, Any], key: str, *, required: bool = True, max_length: int = 8192) -> str | None:
    value = body.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise bad_request(f"{key} must be non-empty text.")
    if len(value) > max_length or any(ord(ch) < 32 for ch in value):
        raise bad_request(f"{key} isn't valid.")
    return value.strip()
