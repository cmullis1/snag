"""Turns yt-dlp error output into plain-English messages with a next action."""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class ErrorKind(StrEnum):
    NETWORK = "network"
    SITE_CHANGED = "site_changed"
    SIGN_IN = "sign_in"
    UNAVAILABLE = "unavailable"
    UNSUPPORTED = "unsupported"
    THROTTLED = "throttled"
    FORMAT_GONE = "format_gone"
    FOLDER = "folder"
    DISK_FULL = "disk_full"
    FFMPEG_MISSING = "ffmpeg_missing"
    NOT_STARTED = "not_started"  # a premiere or live event that hasn't begun
    UNKNOWN = "unknown"


class Action(StrEnum):
    RETRY = "retry"
    CHECK_UPDATE = "check_update"
    CHANGE_FOLDER = "change_folder"
    REINSTALL_FFMPEG = "reinstall_ffmpeg"


@dataclass(frozen=True)
class ErrorInfo:
    kind: ErrorKind
    message: str
    action: Action | None
    can_retry: bool
    keep_partial: bool
    details: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "message": self.message,
            "action": self.action.value if self.action else None,
            "can_retry": self.can_retry,
            "keep_partial": self.keep_partial,
            "details": self.details,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> ErrorInfo:
        action = data.get("action")
        return cls(
            kind=ErrorKind(data["kind"]),
            message=str(data["message"]),
            action=Action(action) if action else None,
            can_retry=bool(data["can_retry"]),
            keep_partial=bool(data["keep_partial"]),
            details=str(data.get("details", "")),
        )


PAYMENT_MESSAGE = "This video requires payment to watch, so Snag can't download it."
DRM_MESSAGE = "This video is copy-protected (DRM), so it can't be downloaded."
MUSIC_PREMIUM_MESSAGE = (
    "This needs YouTube Music Premium. Sign in to YouTube in Chrome or Edge with a Premium account, then retry."
)

# Checked in order; the first rule with a matching pattern wins. A kind may appear more than once. A rule's
# message, when it has one, replaces the kind's (_OUTCOMES).
_RULES: list[tuple[ErrorKind, tuple[str, ...], str | None]] = [
    (ErrorKind.THROTTLED, (
        r"HTTP Error 429",
        r"Too Many Requests",
        r"rate-limited",
        r"content isn['’]t available, try again later",
    ), None),
    # YouTube's own refusals (the playability reason yt-dlp passes on, extractor/youtube/_video.py), which
    # can come after "Video unavailable." or before yt-dlp's cookies hint, so they beat those rules.
    (ErrorKind.UNAVAILABLE, (r"requires payment",), PAYMENT_MESSAGE),
    # "This video is DRM protected" (yt-dlp's report_drm); not its warning that some formats "are DRM protected".
    (ErrorKind.UNAVAILABLE, (r"is DRM protected",), DRM_MESSAGE),
    (ErrorKind.NOT_STARTED, (
        r"Premieres in",
        r"Premiere will begin",
        r"live event will begin",
        r"will begin in a few moments",
    ), None),
    (ErrorKind.SIGN_IN, (r"Music Premium",), MUSIC_PREMIUM_MESSAGE),
    (ErrorKind.SIGN_IN, (r"captcha challenge",), None),
    # yt-dlp tacks a cookies hint onto private-video errors, so this must beat the SIGN_IN rules.
    (ErrorKind.UNAVAILABLE, (r"Private video",), None),
    (ErrorKind.SIGN_IN, (
        r"Sign in to confirm",
        r"age[- ]restricted",
        r"members[- ]only",
        r"Join this channel",
        r"available to this channel's members",
        r"--cookies-from-browser or --cookies",
    ), None),
    (ErrorKind.UNAVAILABLE, (
        r"Video unavailable",
        r"This video is unavailable",
        r"has been removed",
        r"available in your country",
        r"no longer available",
        r"account associated with this video has been terminated",
    ), None),
    (ErrorKind.UNSUPPORTED, (r"Unsupported URL",), None),
    (ErrorKind.FORMAT_GONE, (r"Requested format is not available",), None),
    (ErrorKind.DISK_FULL, (r"No space left on device", r"not enough space on the disk", r"\[Errno 28\]"), None),
    (ErrorKind.FFMPEG_MISSING, (r"ffmpeg not found", r"ffmpeg is not installed"), None),
    (ErrorKind.SITE_CHANGED, (
        r"Signature extraction failed",
        r"nsig extraction failed",
        r"Unable to extract",
        r"HTTP Error 403",
        r"Please report this issue",
    ), None),
    (ErrorKind.NETWORK, (
        r"Unable to download webpage",
        r"getaddrinfo failed",
        r"timed out",
        r"Connection reset",
        r"Connection refused",
        r"Remote end closed connection",
        r"Unable to connect",
        r"IncompleteRead",
        r"Network is unreachable",
        r"Did not get any data blocks",
        r"bytes read, \d+ more expected",
        r"giving up after \d+ retries",
    ), None),
]

# kind -> (message template, action, can_retry, keep_partial)
_OUTCOMES: dict[ErrorKind, tuple[str, Action | None, bool, bool]] = {
    ErrorKind.NETWORK: ("Lost connection", Action.RETRY, True, True),
    ErrorKind.SITE_CHANGED: ("{site} changed something. An update usually fixes this.", Action.CHECK_UPDATE, True, True),
    ErrorKind.SIGN_IN: ("Sign in to {site_mid} in Chrome or Edge, then retry", Action.RETRY, True, True),
    ErrorKind.UNAVAILABLE: ("This video isn't available", None, False, False),
    ErrorKind.UNSUPPORTED: ("Nothing to download on this page", None, False, False),
    ErrorKind.THROTTLED: ("{site} is slowing you down. Paused for {pause}.", None, True, True),
    ErrorKind.FORMAT_GONE: ("That format isn't offered anymore. Pick another one.", None, False, False),
    ErrorKind.FOLDER: ("Can't save to that folder", Action.CHANGE_FOLDER, True, False),
    ErrorKind.DISK_FULL: ("Not enough disk space", Action.RETRY, True, False),
    ErrorKind.FFMPEG_MISSING: ("ffmpeg is missing", Action.REINSTALL_FFMPEG, True, True),
    ErrorKind.NOT_STARTED: ("This video hasn't started yet. Try again once it has aired.", Action.RETRY, True, False),
    ErrorKind.UNKNOWN: ("Something went wrong", Action.RETRY, True, True),
}

DETAILS_LIMIT = 4000
DEFAULT_THROTTLE_MINUTES = 5


def site_label(site: str) -> str:
    """The site's name for the start of a sentence."""
    return "YouTube" if site == "youtube" else "This site"


def _site_mid_label(site: str) -> str:
    """The site's name for the middle of a sentence."""
    return "YouTube" if site == "youtube" else "this site"


def _pause_label(minutes: int | None) -> str:
    """"5 minutes", "1 minute"; a pause shorter than a minute reads as "1 minute"."""
    count = max(1, DEFAULT_THROTTLE_MINUTES if minutes is None else minutes)
    return "1 minute" if count == 1 else f"{count} minutes"


def make_error(
    kind: ErrorKind,
    site: str = "",
    *,
    message: str | None = None,
    details: str = "",
    throttle_minutes: int | None = None,
) -> ErrorInfo:
    """throttle_minutes: how long the queue pauses, named in the THROTTLED message (default 5)."""
    template, action, can_retry, keep_partial = _OUTCOMES[kind]
    text = message or template.format(
        site=site_label(site), site_mid=_site_mid_label(site), pause=_pause_label(throttle_minutes),
    )
    return ErrorInfo(kind, text, action, can_retry, keep_partial, details[-DETAILS_LIMIT:])


def _match(text: str) -> tuple[ErrorKind, str | None] | None:
    """(kind, the rule's own message or None) of the first rule matching text, or None."""
    for kind, patterns, message in _RULES:
        if any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns):
            return kind, message
    return None


def _error_lines(output: str) -> list[str]:
    return [line.strip() for line in output.splitlines() if line.strip().startswith("ERROR:")]


# "[youtube] OrMs0HnKU1I: " or "[generic] " at the start of an ERROR: line's text: who raised it, for what.
_SOURCE_PREFIX = re.compile(r"^\[[^\]]+\]\s*(?:[^\s:]+:\s+)?")
# What yt-dlp appends to a reason: how to pass cookies, where to report a bug, the exception behind it.
_HINT = re.compile(
    r"\s*(?:\bUse --cookies|\bAlso see\s|\bConfirm you are on the latest version|;?\s*please report this issue"
    r"|\(caused by\s)",
    re.IGNORECASE,
)
REASON_LIMIT = 200
_SENTENCE_END = (".", "!", "?", "…")


def reason_text(output: str) -> str | None:
    """The reason on yt-dlp's last ERROR: line as a sentence for the user: without the "ERROR:" and
    "[extractor] id:" in front or the hints yt-dlp appends (cookies, bug reports, the exception), at
    most about REASON_LIMIT characters (cut at a word, ending "…"). None when there is no ERROR: line
    or nothing is left of it. Sites' refusal reasons are free text (YouTube's come from its servers),
    so this is how an error Snag has no rule for can still say what happened."""
    lines = _error_lines(output)
    if not lines:
        return None
    text = _SOURCE_PREFIX.sub("", lines[-1].removeprefix("ERROR:").strip())
    hint = _HINT.search(text)
    if hint:
        text = text[:hint.start()]
    text = text.strip().rstrip(",;:").strip()
    if not text:
        return None
    text = text[0].upper() + text[1:]
    if len(text) > REASON_LIMIT:
        cut = text[:REASON_LIMIT]
        space = cut.rfind(" ")
        if space > REASON_LIMIT // 2:
            cut = cut[:space]
        return cut.rstrip(" ,;:.") + "…"
    return text if text.endswith(_SENTENCE_END) else f"{text}."


def classify(output: str, site: str = "", *, throttle_minutes: int | None = None) -> ErrorInfo:
    """Pick the kind from yt-dlp's ERROR: lines, newest first, so earlier warnings and retry
    chatter can't outvote the error that actually ended the run. Output with no ERROR: line
    at all is matched as a whole. throttle_minutes is passed on to make_error. An error no rule
    recognises says the reason yt-dlp gave (reason_text), if there is one."""
    error_lines = _error_lines(output)
    if error_lines:
        matched = next((m for line in reversed(error_lines) if (m := _match(line)) is not None), None)
    else:
        matched = _match(output)
    kind, message = matched if matched else (ErrorKind.UNKNOWN, reason_text(output))
    return make_error(kind, site, message=message, details=output, throttle_minutes=throttle_minutes)


_FORBIDDEN = re.compile(r"HTTP Error 403", re.IGNORECASE)


def http_forbidden(error: ErrorInfo | None) -> bool:
    """True for a "site changed" error that is the site answering 403 Forbidden. On YouTube that is
    usually a download link that went stale while the download ran (the links expire, and are tied
    to the session that got them); a fresh run of yt-dlp extracts new ones (snag.jobs.recovery)."""
    return error is not None and error.kind is ErrorKind.SITE_CHANGED and bool(_FORBIDDEN.search(error.details))


# yt-dlp's YouTube extractor warns with this when no JavaScript runtime (Deno) can be used; some formats
# are then missing (extractor/youtube/_video.py).
JS_RUNTIME_MISSING_TEXT = "No supported JavaScript runtime could be found"


def missing_js_runtime(output: str) -> bool:
    """True when yt-dlp's output says it had no JavaScript runtime, so Deno is missing or broken."""
    return JS_RUNTIME_MISSING_TEXT in output
