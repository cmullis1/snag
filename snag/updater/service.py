"""UpdaterService: first-run setup, the daily yt-dlp update check, installing an update, and
reinstalling ffmpeg or Deno (spec 4.8).

Everything it reports goes through notify(event) as JSON-safe dicts:
  {"type": "setup",  "tool", "phase", "done", "total", "message"}  first-run progress
  {"type": "update", "tool", "phase", "done", "total", "message"}  update / reinstall progress
  {"type": "status", "updater": status()}                          after every state change
Phases: downloading, verifying, installing, done, error; an update or reinstall also has "waiting"
(for running downloads to finish). Download progress is sent at most once per percent.

Every change to bin_dir goes through one lock, so setup, an update and a reinstall never overlap. An
update check takes the same lock, so its version reading can't straddle a swap; a check asked for while
one of those (or another check) is under way doesn't check, it answers with the current status.

Queue holds:
- Setup holds the queue while a required tool (yt-dlp, ffmpeg + ffprobe) is missing, and keeps
  holding it if installing one fails: jobs can't run without them, so they wait for a retry
  (ensure_installed again, or reinstall) instead of failing. Call hold_if_missing() before
  queue.start() so jobs restored at startup wait too. If setup is cancelled or fails unexpectedly the
  hold is released.
- An update or reinstall stages the new tool first, then holds the queue, waits for running jobs,
  swaps and releases, on every path out."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import httpx

from snag.paths import AppPaths, Tools
from snag.proc import run_capture
from snag.updater.install import (
    DENO,
    FFMPEG,
    YTDLP,
    Smoke,
    ToolSource,
    clean_leftovers,
    default_smoke,
    executables,
    install,
    prepare,
)
from snag.updater.releases import USER_AGENT, UpdaterError, latest_release

log = logging.getLogger(__name__)

CHECK_INTERVAL = 24 * 3600.0  # seconds between daily update checks
RETRY_AFTER = 3600.0  # after a failed check, wait this long before the next try
LOOP_STEP = 900.0  # run_daily never sleeps longer than this, so a setting change or a PC waking up counts soon
CLOCK_SLACK = 24 * 3600.0  # a saved last_check further in the future than this is nonsense, not clock skew
VERSION_TIMEOUT = 30.0

SOURCES: dict[str, ToolSource] = {"yt-dlp": YTDLP, "ffmpeg": FFMPEG, "deno": DENO}
REQUIRED = ("yt-dlp", "ffmpeg")  # ffmpeg stands for ffmpeg.exe and ffprobe.exe; Deno is optional
REINSTALLABLE = ("ffmpeg", "deno")

Notify = Callable[[dict], None]


class _Queue(Protocol):
    def hold(self) -> None: ...
    def release(self) -> None: ...
    async def wait_running_done(self) -> None: ...


class _SettingsSource(Protocol):
    def get(self) -> Any: ...  # anything with check_updates_daily: bool


def default_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=30.0)


TEST_MODE_MESSAGE = "Updates aren't available in test mode."


def _refuse(request: httpx.Request) -> httpx.Response:
    raise UpdaterError(TEST_MODE_MESSAGE)


def no_network_client() -> httpx.AsyncClient:
    """`python -m snag` under the test hook (snag.hook): a client that sends nothing anywhere. Every
    request fails with TEST_MODE_MESSAGE, which the updater reports like any failure (an update check's
    error, setup's or a reinstall's message), so no test can reach the real GitHub."""
    return httpx.AsyncClient(transport=httpx.MockTransport(_refuse), headers={"User-Agent": USER_AGENT})


def is_newer(latest: str, current: str | None) -> bool:
    """True when the latest release is newer than the installed version (None: unknown or broken).

    yt-dlp versions are dates, "2026.08.19", and nightly builds add a part, "2026.08.19.233512";
    they compare part by part as numbers. Anything else counts as newer when it differs."""
    if current is None:
        return True
    latest_key, current_key = _version_key(latest), _version_key(current)
    if latest_key is None or current_key is None:
        return latest != current
    return latest_key > current_key


def _version_key(version: str) -> tuple[int, ...] | None:
    parts = version.strip().split(".")
    return tuple(int(p) for p in parts) if all(p.isascii() and p.isdigit() for p in parts) else None


@dataclass
class _Progress:
    phase: str | None = None
    done: int = 0
    total: int | None = None
    message: str | None = None


@dataclass
class _UpdateState:
    current: str | None = None  # installed yt-dlp version at the last check or update
    latest: str | None = None
    available: str | None = None  # the version to offer, when newer than current
    last_check: float | None = None  # wall-clock time of the last successful check
    state: str = "idle"  # idle, checking, installing
    message: str | None = None  # result of the last update or reinstall, for the user
    error: str | None = None  # why the last check or update failed

    PERSISTED = ("current", "latest", "available", "last_check")


class UpdaterService:
    def __init__(
        self,
        paths: AppPaths,
        tools: Tools,
        queue: _Queue,
        notify: Notify,
        *,
        client_factory: Callable[[], httpx.AsyncClient] = default_client,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        smoke: Smoke = default_smoke,
    ):
        """tools runs the installed yt-dlp for its version; installs go to paths.bin_dir.
        clock is wall-clock time (it is saved, so it must survive restarts); sleep is how run_daily waits."""
        self._paths = paths
        self._tools = tools
        self._queue = queue
        self._notify_fn = notify
        self._client_factory = client_factory
        self._clock = clock
        self._sleep = sleep
        self._smoke = smoke
        self._lock = asyncio.Lock()
        self._setup_hold = False
        self._setup_state = "idle"  # idle, running, done, error
        self._setup_message: str | None = None
        self._setup_tools: dict[str, _Progress] = {name: _Progress() for name in SOURCES}
        self._update = self._load()
        self._last_attempt: float | None = None  # wall-clock time of the last check, successful or not

    # ----- state -----

    @property
    def state_file(self) -> Path:
        return self._paths.root / "updater.json"

    def missing_tools(self) -> list[str]:
        """Tools whose executables aren't all in bin_dir, in install order."""
        bin_dir = self._paths.bin_dir
        return [name for name, source in SOURCES.items()
                if not all((bin_dir / exe).is_file() for exe in executables(source))]

    def status(self) -> dict:
        """JSON-safe: {"setup": {...}, "update": {...}}."""
        update = asdict(self._update)
        update["last_check"] = _iso(self._update.last_check)
        return {
            "setup": {
                "state": self._setup_state,
                "message": self._setup_message,
                "can_retry": self._setup_state == "error",
                "tools": {name: asdict(progress) for name, progress in self._setup_tools.items()},
            },
            "update": update,
        }

    # ----- first run -----

    def hold_if_missing(self) -> bool:
        """Hold the queue if a required tool is missing (once; later calls don't add holds). True while
        setup holds the queue."""
        if not self._setup_hold and any(name in REQUIRED for name in self.missing_tools()):
            self._queue.hold()
            self._setup_hold = True
        return self._setup_hold

    async def ensure_installed(self) -> bool:
        """Install every missing tool. True when the required ones are all present afterwards (Deno
        may still have failed; its error is in status()). Also the retry after a failed setup."""
        async with self._lock:
            succeeded = False
            try:
                clean_leftovers(self._paths.bin_dir)
                self.hold_if_missing()
                missing = self.missing_tools()
                if missing:
                    self._setup_state, self._setup_message = "running", None
                    self._send_status()
                    async with self._client_factory() as client:
                        for name in missing:
                            await self._install_for_setup(client, name)
                succeeded = True
            finally:
                if not succeeded:  # cancelled, or a bug: never leave the queue held by accident
                    self._end_setup_hold()
                    self._setup_state, self._setup_message = "error", "Snag couldn't finish setting up."
                    self._send_status()
            return self._after_tools_changed()

    async def _install_for_setup(self, client: httpx.AsyncClient, name: str) -> None:
        progress = self._setup_tools[name] = _Progress()
        reporter = _Reporter(lambda: self._send("setup", name, progress))

        def on_phase(phase: str) -> None:
            progress.phase, progress.message = phase, None
            reporter.phase()

        def on_bytes(done: int, total: int | None) -> None:
            progress.done, progress.total = done, total
            reporter.bytes(done, total)

        try:
            await install(client, SOURCES[name], self._paths.bin_dir, progress=on_bytes, phase=on_phase,
                          smoke=self._smoke)
        except (UpdaterError, OSError) as exc:
            progress.phase, progress.message = "error", _plain(exc, SOURCES[name])
            log.warning("Setup couldn't install %s: %s", name, progress.message)
        else:
            progress.phase = "done"
        self._send("setup", name, progress)

    def _after_tools_changed(self) -> bool:
        """Update the setup state from what is in bin_dir; end the setup hold once nothing required is
        missing. True when nothing required is missing."""
        missing_required = [name for name in self.missing_tools() if name in REQUIRED]
        if missing_required:
            self._setup_state = "error"
            failures = [self._setup_tools[name].message for name in missing_required if self._setup_tools[name].message]
            self._setup_message = " ".join(["Snag couldn't finish setting up.", *failures[:1]])
        else:
            self._end_setup_hold()
            self._setup_state, self._setup_message = "done", None
        self._send_status()
        return not missing_required

    def _end_setup_hold(self) -> None:
        if self._setup_hold:
            self._setup_hold = False
            self._queue.release()

    # ----- checking for updates -----

    async def check_for_update(self) -> dict:
        """Compare the installed yt-dlp with the latest release; returns status()["update"]. A failure
        is reported in its "error", never raised. While setup, an update, a reinstall or another check
        is under way it doesn't check (that could take hours, or race the swap): it returns the current
        status, and run_daily tries again after RETRY_AFTER."""
        self._last_attempt = self._clock()
        if self._lock.locked():
            return self.status()["update"]
        async with self._lock:
            self._update.state = "checking"
            self._send_status()
            try:
                current = await self.installed_version()
                async with self._client_factory() as client:
                    release = await latest_release(client, YTDLP.repo)
            except UpdaterError as exc:
                self._update.error = str(exc)
            else:
                self._update = replace(
                    self._update, current=current, latest=release.tag, last_check=self._clock(), error=None,
                    available=release.tag if is_newer(release.tag, current) else None,
                )
                self._save()
            finally:
                self._update.state = "idle"
            self._send_status()
            return self.status()["update"]

    async def installed_version(self) -> str | None:
        """What the installed yt-dlp says for --version; None when it is missing or doesn't run."""
        try:
            result = await run_capture([*self._tools.ytdlp, "--version"], timeout=VERSION_TIMEOUT)
        except (OSError, TimeoutError):
            return None
        lines = result.stdout.strip().splitlines()
        return lines[0].strip() if result.returncode == 0 and lines else None

    async def run_daily(self, settings: _SettingsSource) -> None:
        """Check for an update whenever check_updates_daily is on and the last check is a day old
        (a failed check is retried after RETRY_AFTER). Runs until cancelled."""
        while True:
            wait = self._until_due()
            if wait <= 0 and settings.get().check_updates_daily:
                try:
                    await self.check_for_update()
                except Exception:  # a bug here must not end the daily checks for good
                    log.exception("The update check failed")
                    self._last_attempt = self._clock()
                continue
            await self._sleep(min(LOOP_STEP, wait) if wait > 0 else LOOP_STEP)

    def _until_due(self) -> float:
        """Seconds until the next check is due. A time in the future (the clock was moved back) or one
        that isn't a usable time counts as due now."""
        now = self._clock()
        due = 0.0
        for since, interval in ((self._update.last_check, CHECK_INTERVAL), (self._last_attempt, RETRY_AFTER)):
            if _usable_time(since) and since <= now:
                due = max(due, since + interval - now)
        return due

    # ----- installing an update, reinstalling -----

    async def install_update(self) -> dict:
        """Install the latest yt-dlp (spec 4.8): download, verify and test it, then hold the queue,
        wait for running downloads, swap and release. Returns status()["update"], whose "message"
        is "Updated to X" or "Update didn't work: still on version X" (": yt-dlp needs
        reinstalling" when there is no yt-dlp left; setup's retry installs it). A cancel that
        arrives once the swap has finished still records X."""
        async with self._lock:
            current = await self.installed_version() or self._update.current
            try:
                await self._replace("yt-dlp")  # records "Updated to X" once the new one is in place
            except (UpdaterError, OSError) as exc:
                if "yt-dlp" in self.missing_tools():  # a failed rollback lost it: setup's retry repairs it
                    version = "yt-dlp needs reinstalling"
                elif current:
                    version = f"still on version {current}"
                else:
                    version = "the current version is still in place"
                self._update.message = f"Update didn't work: {version}"
                self._update.error = _plain(exc, YTDLP)
                log.warning("Updating yt-dlp failed: %s", self._update.error)
                self.hold_if_missing()  # jobs wait for that repair instead of failing
            self._after_tools_changed()
            return self.status()["update"]

    async def reinstall(self, tool: str) -> dict:
        """Reinstall ffmpeg (with ffprobe) or Deno, as install_update does for yt-dlp. Returns
        status()["update"] with "Reinstalled <tool>" or "Couldn't reinstall <tool>: <why>" as message.
        Raises ValueError for any other tool."""
        if tool not in REINSTALLABLE:
            raise ValueError(f"Only {' and '.join(REINSTALLABLE)} can be reinstalled, not {tool!r}")
        source = SOURCES[tool]
        async with self._lock:
            try:
                await self._replace(tool)  # records "Reinstalled <tool>" once the new one is in place
            except (UpdaterError, OSError) as exc:
                self._update.message = f"Couldn't reinstall {source.tool}: {_plain(exc, source)}"
                self.hold_if_missing()  # a failed rollback can lose ffmpeg or ffprobe: jobs wait for a repair
            self._after_tools_changed()
            return self.status()["update"]

    async def _replace(self, tool: str) -> str:
        """Stage the latest release of tool, then hold the queue, wait for running jobs, swap, release.
        Sends "update" events; returns the tag installed."""
        source = SOURCES[tool]
        progress = _Progress()
        reporter = _Reporter(lambda: self._send("update", tool, progress))

        def on_phase(phase: str) -> None:
            progress.phase, progress.message = phase, None
            reporter.phase()

        def on_bytes(done: int, total: int | None) -> None:
            progress.done, progress.total = done, total
            reporter.bytes(done, total)

        self._update.state = "installing"
        self._update.message = None
        self._send_status()
        try:
            async with self._client_factory() as client:
                staged = await prepare(client, source, self._paths.bin_dir, progress=on_bytes, phase=on_phase,
                                       smoke=self._smoke)
            try:
                self._queue.hold()
                try:
                    on_phase("waiting")
                    await self._queue.wait_running_done()
                    on_phase("installing")
                    await staged.swap()
                finally:
                    self._queue.release()
                    if staged.swapped:  # also when cancelled after the swap finished: it happened
                        self._record_swap(tool, staged.tag)
            finally:
                staged.discard()
        except (UpdaterError, OSError) as exc:
            progress.phase, progress.message = "error", _plain(exc, source)
            self._send("update", tool, progress)
            raise
        finally:
            self._update.state = "idle"
        progress.phase = "done"
        self._send("update", tool, progress)
        return staged.tag

    def _record_swap(self, tool: str, tag: str) -> None:
        """A new tool is in place: remember it (saved for the next start) and say so."""
        if tool == "yt-dlp":
            self._update = replace(self._update, current=tag, available=None, latest=tag,
                                   message=f"Updated to {tag}", error=None)
            self._save()
        else:
            self._update.message = f"Reinstalled {SOURCES[tool].tool}"
            self._setup_tools[tool] = _Progress(phase="done")
        self._send_status()

    # ----- persistence and events -----

    def _load(self) -> _UpdateState:
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("not an object")
            values = {key: data.get(key) for key in _UpdateState.PERSISTED}
            for key in ("current", "latest", "available"):
                if values[key] is not None and not isinstance(values[key], str):
                    raise ValueError(f"{key} is not text")
            last_check = values["last_check"]
            if last_check is not None:
                if not _usable_time(last_check) or last_check > self._clock() + CLOCK_SLACK:
                    raise ValueError(f"last_check {last_check!r} is not a sensible time")
                values["last_check"] = float(last_check)
            return _UpdateState(**values)
        except FileNotFoundError:
            return _UpdateState()
        except (OSError, ValueError, RecursionError) as exc:
            log.warning("Ignoring %s: %s", self.state_file, exc)
            return _UpdateState()

    def _save(self) -> None:
        data = {key: getattr(self._update, key) for key in _UpdateState.PERSISTED}
        temp = self.state_file.with_name(f"{self.state_file.name}.tmp")
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            temp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(temp, self.state_file)
        except OSError as exc:
            log.warning("Couldn't save %s: %s", self.state_file, exc)

    def _send(self, kind: str, tool: str, progress: _Progress) -> None:
        self._notify(lambda: {"type": kind, "tool": tool, **asdict(progress)})

    def _send_status(self) -> None:
        self._notify(lambda: {"type": "status", "updater": self.status()})

    def _notify(self, build: Callable[[], dict]) -> None:
        """Build an event and send it. Neither a bug building it nor a failing listener may break setup,
        a check or an install, so both are logged and swallowed."""
        try:
            self._notify_fn(build())
        except Exception:
            log.exception("Couldn't send an updater event")


@dataclass
class _Reporter:
    """Sends progress at most once per percent (or per MiB when the size is unknown), and on every
    phase change."""

    send: Callable[[], None]
    _last: int | None = field(default=None)

    def phase(self) -> None:
        self._last = None
        self.send()

    def bytes(self, done: int, total: int | None) -> None:
        step = done * 100 // total if total else done // (1 << 20)
        if step != self._last:
            self._last = step
            self.send()


def _plain(exc: BaseException, source: ToolSource) -> str:
    if isinstance(exc, UpdaterError):
        return str(exc)
    if isinstance(exc, OSError):
        return f"Couldn't save {source.tool}: {exc.strerror or exc}"
    return str(exc)


def _usable_time(value: object) -> bool:
    """A real, positive epoch time that datetime can show (bools and NaN/infinity are not)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value) and 0 < value < _MAX_TIME
    except OverflowError:  # an int too big for a float
        return False


_MAX_TIME = datetime(9999, 1, 1, tzinfo=UTC).timestamp()


def _iso(timestamp: float | None) -> str | None:
    """The time as ISO 8601 UTC; None when there is none or it can't be shown (never raises)."""
    if not _usable_time(timestamp):
        return None
    try:
        return datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, ValueError, OSError):
        return None
