"""The single SQLite file holding the job queue, history, and settings."""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

# MIGRATIONS[n] upgrades a version-n database to version n + 1. To change the schema, append a step;
# never edit one that has shipped.
MIGRATIONS: tuple[tuple[str, ...], ...] = (
    (  # 0 -> 1: the original schema
        """CREATE TABLE jobs (
            id TEXT PRIMARY KEY,
            state TEXT NOT NULL,
            data TEXT NOT NULL,
            created_at REAL NOT NULL
        )""",
        """CREATE TABLE history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            site TEXT NOT NULL,
            video_id TEXT NOT NULL,
            url TEXT NOT NULL,
            title TEXT NOT NULL,
            channel TEXT,
            kind TEXT NOT NULL,
            format_summary TEXT NOT NULL,
            file_path TEXT,
            size INTEGER,
            finished_at REAL NOT NULL,
            status TEXT NOT NULL,
            error_message TEXT
        )""",
        "CREATE INDEX history_video ON history(site, video_id)",
        """CREATE TABLE settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )""",
    ),
    (  # 1 -> 2: the History tab lists newest first
        "CREATE INDEX history_finished ON history(finished_at, id)",
    ),
    (  # 2 -> 3: one History entry per download, keyed by its job (older entries have no job id)
        "ALTER TABLE history ADD COLUMN job_id TEXT",
        "CREATE UNIQUE INDEX history_job ON history(job_id)",
    ),
    (  # 3 -> 4: a playlist batch's summary outlives its jobs (older entries have no batch id)
        "ALTER TABLE history ADD COLUMN batch_id TEXT",
        "CREATE INDEX history_batch ON history(batch_id)",
    ),
)
SCHEMA_VERSION = len(MIGRATIONS)

_CORRUPT_CODES = {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}
_SIDECARS = ("-wal", "-shm")


class Connection(sqlite3.Connection):
    """A connection plus the lock every store using it must hold.

    The stores share one connection, so the lock is per connection rather than per store: that keeps
    one store's statements from landing inside another store's transaction.

    Every call on a shared connection, including close(), must be made while holding ``lock``. Some of
    sqlite3's calls (create_function, for one) wait for SQLite's connection mutex while holding the GIL;
    if another thread is running a query that calls back into Python (snag_casefold) it holds that
    mutex and needs the GIL, and the whole process hangs. Anything a store needs on the connection
    (like SQL functions) is therefore set up in open_db, before the connection is shared.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.lock = threading.RLock()

    def close(self) -> None:
        with self.lock:
            super().close()


def _casefold(text: str | None) -> str | None:
    return text.casefold() if text is not None else None


class _Corrupt(Exception):
    pass


class NewerDatabaseError(RuntimeError):
    """The database was written by a newer Snag, whose schema this one doesn't know."""


def _connect(path: Path) -> Connection:
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None, factory=Connection)
    conn.row_factory = sqlite3.Row
    try:
        # SQLite's own case folding only covers ASCII; this handles "É" and "ß" too (history search).
        conn.create_function("snag_casefold", 1, _casefold, deterministic=True)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            check = conn.execute("PRAGMA quick_check(1)").fetchone()[0]
        except sqlite3.DatabaseError as error:
            if (getattr(error, "sqlite_errorcode", 0) & 0xFF) in _CORRUPT_CODES:
                raise _Corrupt(str(error)) from error
            raise
        if check != "ok":
            raise _Corrupt(check)
        _migrate(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise NewerDatabaseError(
                f"This database was written by a newer version of Snag (schema {version}, this version "
                f"understands up to {SCHEMA_VERSION}). Update Snag to open it."
            )
        for step in MIGRATIONS[version:]:
            for statement in step:
                conn.execute(statement)
        if version != SCHEMA_VERSION:
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _set_aside(path: Path) -> Path:
    """Rename a corrupt database (and its WAL files) to <name>.corrupt-<timestamp> for diagnosis."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = path.with_name(f"{path.name}.corrupt-{stamp}")
    number = 2
    while any(Path(f"{target}{suffix}").exists() for suffix in ("", *_SIDECARS)):
        target = path.with_name(f"{path.name}.corrupt-{stamp}-{number}")
        number += 1
    for suffix in _SIDECARS:
        sidecar = Path(f"{path}{suffix}")
        if sidecar.exists():
            sidecar.rename(f"{target}{suffix}")
    path.rename(target)
    return target


def open_db(path: Path) -> Connection:
    """Open (creating or upgrading) the database. A corrupt file is set aside and a fresh one made.

    Raises NewerDatabaseError for a database written by a newer Snag; that file is left alone.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        return _connect(path)
    except _Corrupt as error:
        moved = _set_aside(path)
        log.warning("Database %s is corrupt (%s); moved it to %s and started a new one", path, error, moved)
        return _connect(path)
