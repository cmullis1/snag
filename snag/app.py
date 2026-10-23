"""SnagCore: builds Snag's engine (database, stores, cookies, lookups, the job queue, the updater) in
the order it needs, and shuts it down cleanly. No web server and no tray here: those sit on top of it.

Events: the updater's events, and the tools' status when the checks run again, go to every listener
given to subscribe_events() (the WebSocket hub forwards them to the UI). Each is a JSON-safe dict:
  {"type": "setup" | "update", ...}                     install progress (snag.updater.service)
  {"type": "status", "updater": {...}}                  the updater's status() after every change
  {"type": "status", "tools": {...}}                    the tool checks (schemas.tool_status_json),
                                                        after they run again
  {"type": "status", "youtube_login": ISO time | None}  sent by the server when cookies arrive
  {"type": "settings", "settings": {...}}               sent by the server after the settings change
                                                        (snag.store.settings.settings_to_json)"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from snag.checks import ToolStatus, check_tools
from snag.cookies import CookieJar
from snag.info.lookup import LookupService
from snag.jobs.queue import JobQueue
from snag.paths import AppPaths, Tools
from snag.server.schemas import tool_status_json
from snag.store.db import Connection, NewerDatabaseError, open_db
from snag.store.history import HistoryStore
from snag.store.jobs import JobStore
from snag.store.settings import Settings, SettingsStore, default_settings
from snag.updater.service import UpdaterService

log = logging.getLogger(__name__)

DEFAULT_PORT = 47823
_IN_USE_WINERRORS = {32, 33}  # ERROR_SHARING_VIOLATION, ERROR_LOCK_VIOLATION (setting a corrupt file aside)

EventListener = Callable[[dict], None]


class StartupError(Exception):
    """Snag can't start; message is plain text to show the user as it is."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _database_error(error: BaseException) -> StartupError:
    in_use = StartupError("Snag's database is in use by another program. Close that program and start Snag again.")
    if isinstance(error, NewerDatabaseError):
        return StartupError("Snag's database is from a newer version of Snag. Update Snag to open it.")
    if isinstance(error, OSError) and getattr(error, "winerror", None) in _IN_USE_WINERRORS:
        return in_use
    if isinstance(error, sqlite3.Error):
        # SQLITE_BUSY is another connection holding the file; SQLITE_LOCKED (a table locked inside one
        # connection) is Snag's own doing, so it gets the plain message below.
        code = getattr(error, "sqlite_errorcode", None)
        if code is not None and code & 0xFF == sqlite3.SQLITE_BUSY:
            return in_use
        if code is None and str(error).lower().startswith("database is locked"):
            return in_use
    return StartupError(f"Snag couldn't open its database: {error}")


class SnagCore:
    """Call open() once, then close() at shutdown, both on the event loop that runs the queue.

    After open(): conn, settings, history, job_store, cookies (the in-memory CookieJar), lookups,
    queue, updater, and checks (the tools' status when Snag started, or when they last ran again)."""

    def __init__(
        self,
        paths: AppPaths,
        tools: Tools,
        *,
        port: int = DEFAULT_PORT,
        defaults: Settings | None = None,
        updater_options: Mapping[str, Any] | None = None,
    ):
        """defaults: the settings a fresh database starts from; by default the real Videos and Music
        folders (tests pass their own, so they never touch the user's folders).
        updater_options: keyword arguments for UpdaterService (client_factory, clock, sleep, smoke);
        tests pass a fake GitHub here."""
        self.paths = paths
        self.tools = tools
        self.port = port
        self._defaults = defaults
        self._updater_options = dict(updater_options or {})
        self._listeners: list[EventListener] = []
        self._background: dict[str, asyncio.Task[None]] = {}
        self._opened = False
        self._opening: asyncio.Task[None] | None = None  # open()'s work, while it runs
        self._closing: asyncio.Future[None] | None = None  # the one shutdown every close() waits for
        self._releasing: asyncio.Future[None] | None = None  # see _release()
        self.conn: Connection | None = None
        self.settings: SettingsStore
        self.history: HistoryStore
        self.job_store: JobStore
        self.cookies: CookieJar
        self.lookups: LookupService
        self.queue: JobQueue | None = None
        self.updater: UpdaterService
        self.checks: ToolStatus

    async def open(self) -> None:
        """Raises StartupError, with a message for the user, when Snag can't start, and RuntimeError when
        called a second time or once close() has been called. A close() while it runs cancels it: it
        then raises CancelledError, having released whatever it had opened."""
        if self._opened or self._closing is not None:
            raise RuntimeError("SnagCore.open() can be called only once, and not after close()")
        self._opened = True
        # A task of its own, so close() can cancel it without cancelling open()'s caller; cancelling
        # the caller cancels it too (it is awaited directly).
        self._opening = asyncio.ensure_future(self._open())
        try:
            await self._opening
        finally:
            self._opening = None

    async def _open(self) -> None:
        paths = self.paths
        try:
            for folder in (paths.root, paths.bin_dir, paths.work_dir, paths.cookies_dir):
                folder.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise StartupError(f"Snag couldn't create its folder {paths.root}: {error}") from error
        try:
            self.conn = open_db(paths.db_file)
        except (NewerDatabaseError, OSError, sqlite3.Error) as error:
            log.exception("Couldn't open the database %s", paths.db_file)
            raise _database_error(error) from error
        try:
            await self._build()
        except BaseException as error:
            # Not close(): that may be what cancelled this, and it waits for this to end.
            await asyncio.shield(self._release())
            if isinstance(error, (OSError, sqlite3.Error)):
                log.exception("Couldn't start the queue")
                raise _database_error(error) from error
            raise

    async def _build(self) -> None:
        conn = self.conn
        self.settings = SettingsStore(conn, self._defaults or default_settings())
        self.history = HistoryStore(conn)
        self.job_store = JobStore(conn)
        self.cookies = CookieJar()
        self.lookups = LookupService(self.tools, cookies=self.cookies.get, cookies_dir=self.paths.cookies_dir)
        lookups = self.lookups
        self.queue = JobQueue(
            tools=self.tools,
            paths=self.paths,
            job_store=self.job_store,
            history=self.history,
            # A download's lookup (to replace a format that's gone) must see the formats as they are now.
            lookup=lambda url: lookups.get(url, fresh=True),
            cookies=self.cookies.get,
            max_concurrent=self.settings.get().max_concurrent,
        )
        self.updater = UpdaterService(self.paths, self.tools, self.queue, self.emit, **self._updater_options)
        # Before the queue starts: jobs restored from the last session must wait while yt-dlp, ffmpeg
        # or ffprobe is missing, instead of starting (and failing) before setup installs them.
        self.updater.hold_if_missing()
        # Before any download or lookup: it restores the queue and clears leftover cookies files. The
        # video and audio folders are swept before serving when they are on a local drive; remembered
        # destinations (and network shares) in the background, so an unreachable NAS can't hold Snag up.
        settings = self.settings.get()
        main = [Path(settings.video_dir), Path(settings.audio_dir)]
        # The tool checks run meanwhile (they never raise); a failed start stops them.
        checks = asyncio.create_task(check_tools(self.tools), name="snag-check-tools")
        try:
            await self.queue.start(main, [root for root in self.extra_roots() if root not in main])
        except BaseException:
            checks.cancel()
            await asyncio.gather(checks, return_exceptions=True)
            raise
        self.checks = await checks

    def extra_roots(self) -> list[Path]:
        """Folders whose .snag-tmp may hold leftover working folders: the video and audio folders and
        every destination the user picked before. Each once, in that order."""
        settings = self.settings.get()
        folders = [settings.video_dir, settings.audio_dir, *settings.remembered_dirs]
        return list(dict.fromkeys(Path(folder) for folder in folders))

    # ----- events -----

    def subscribe_events(self, listener: EventListener) -> None:
        """Call listener, on the event loop, with every event (see the module docstring)."""
        self._listeners.append(listener)

    def unsubscribe_events(self, listener: EventListener) -> None:
        while listener in self._listeners:
            self._listeners.remove(listener)

    def emit(self, event: dict) -> None:
        """Send an event to every listener; one that raises is logged and skipped."""
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:
                log.exception("An event listener failed on a %s event", event.get("type"))

    # ----- tools -----

    async def refresh_checks(self) -> ToolStatus:
        """Run the tool checks again (an install changed the tools) and send their status."""
        self.checks = await check_tools(self.tools)
        self.emit({"type": "status", "tools": tool_status_json(self.checks)})
        return self.checks

    async def setup_tools(self) -> bool:
        """First-run setup, and its retry: install every missing tool, then check the tools again.
        True when the required tools are all there."""
        return await self._then_recheck(self.updater.ensure_installed())

    async def install_update(self) -> dict:
        """Install the latest yt-dlp (UpdaterService.install_update), then check the tools again."""
        return await self._then_recheck(self.updater.install_update())

    async def reinstall_tool(self, tool: str) -> dict:
        """Reinstall ffmpeg or Deno (UpdaterService.reinstall), then check the tools again."""
        return await self._then_recheck(self.updater.reinstall(tool))

    async def _then_recheck(self, work: Awaitable[Any]) -> Any:
        """Await work, then check the tools again, whether it worked or failed. Not when it is
        cancelled: that is Snag closing, which mustn't wait for the checks or send their event."""
        try:
            result = await work
        except asyncio.CancelledError:
            raise
        except Exception:
            await self.refresh_checks()
            raise
        await self.refresh_checks()
        return result

    # ----- background work -----

    def start_background(self, name: str, work: Callable[[], Awaitable[Any]]) -> bool:
        """Run work() in a task Snag keeps (so it isn't garbage-collected) and cancels at close(). One
        task per name at a time: while one runs, nothing starts and this returns False. An exception
        is logged."""
        if self.background_running(name):
            return False
        task = asyncio.create_task(self._run_background(name, work), name=f"snag-{name}")
        self._background[name] = task
        task.add_done_callback(lambda done: self._forget(name, done))
        return True

    def background_running(self, name: str) -> bool:
        task = self._background.get(name)
        return task is not None and not task.done()

    def _forget(self, name: str, task: asyncio.Task[None]) -> None:
        if self._background.get(name) is task:
            del self._background[name]

    @staticmethod
    async def _run_background(name: str, work: Callable[[], Awaitable[Any]]) -> None:
        try:
            await work()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Background task %r failed", name)

    # ----- shutdown -----

    async def close(self) -> None:
        """Cancel background work (so nothing holds the queue or swaps a tool meanwhile), stop the
        queue (partial downloads are kept, to resume next time), then close the database.

        Safe to call more than once, at the same time too, and after a failed or missing open(): every
        call waits for the one shutdown. Cancelling a call cancels only that wait (the CancelledError
        reaches its caller); the shutdown carries on. An open() still under way is cancelled first, and
        waited for, so nothing it started is left behind; open() can't be called afterwards."""
        if self._closing is None:
            self._closing = asyncio.ensure_future(self._shut_down())
        await asyncio.shield(self._closing)

    async def _shut_down(self) -> None:
        opening = self._opening
        if opening is not None and not opening.done():
            opening.cancel()
            await asyncio.wait((opening,))  # it releases what it opened (see _open)
        await self._release()

    async def _release(self) -> None:
        """Stop and close whatever is open, once: a later call waits for the same release (a failed
        open() and close() can both ask for it, and the queue must stop before the database closes)."""
        if self._releasing is None:
            self._releasing = asyncio.ensure_future(self._release_once())
        await asyncio.shield(self._releasing)

    async def _release_once(self) -> None:
        tasks = list(self._background.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        queue, self.queue = self.queue, None
        try:
            if queue is not None:
                await queue.shutdown()
        finally:
            conn, self.conn = self.conn, None
            if conn is not None:
                with conn.lock:
                    conn.close()
