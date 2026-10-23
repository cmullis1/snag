"""Saves the queue so downloads survive the app closing or the PC restarting.

Unfinished and failed jobs are kept across restarts; done and cancelled ones are pruned at startup
(History keeps their record). A row that can't be decoded is re-labelled CORRUPT_STATE and logged: it
is never loaded again, isn't counted by all_ids(), and prune_corrupt() deletes it after copying it to
jobs-corrupt.jsonl beside the database, so a bug that wrote bad rows can still be diagnosed.
"""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterable
from pathlib import Path

from snag.jobs.models import UNFINISHED_STATES, Job, JobState, job_from_dict, job_to_dict

from .db import Connection

log = logging.getLogger(__name__)

CORRUPT_STATE = "corrupt"
CORRUPT_LOG_NAME = "jobs-corrupt.jsonl"
CORRUPT_DATA_LIMIT = 8192  # characters of each corrupt row's data kept as evidence
_PRUNED_STATES = (JobState.DONE, JobState.CANCELLED)


class JobStore:
    def __init__(self, conn: Connection, *, corrupt_log: Path | None = None):
        """corrupt_log: where prune_corrupt() keeps evidence; by default jobs-corrupt.jsonl beside the
        database file (an in-memory database has none, so its corrupt rows are only logged)."""
        self._conn = conn
        self._lock = conn.lock
        # Found lazily, under the lock: every call on the shared connection must hold it (see db.Connection).
        self._corrupt_log = corrupt_log

    def save(self, job: Job) -> None:
        data = json.dumps(job_to_dict(job))
        with self._lock:
            self._conn.execute(
                """INSERT INTO jobs(id, state, data, created_at) VALUES(?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET state = excluded.state, data = excluded.data""",
                (job.id, job.state.value, data, job.created_at),
            )

    def load(self, job_id: str) -> Job | None:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, data FROM jobs WHERE id = ? AND state != ?", (job_id, CORRUPT_STATE),
            ).fetchall()
            jobs = self._decode(rows)
        return jobs[0] if jobs else None

    def load_unfinished(self) -> list[Job]:
        """Waiting, active and interrupted jobs, oldest first."""
        return self._load_states(UNFINISHED_STATES)

    def load_failed(self) -> list[Job]:
        """Failed jobs, oldest first; they stay until the user retries or clears them."""
        return self._load_states((JobState.FAILED,))

    def prune_finished(self) -> list[Job]:
        """Delete every done and cancelled row, and return the ones that could be decoded."""
        with self._lock:
            rows = self._select_states(_PRUNED_STATES)
            jobs = [job for job in map(self._try_decode, rows) if job is not None]
            values = [state.value for state in _PRUNED_STATES]
            self._conn.execute(f"DELETE FROM jobs WHERE state IN ({_marks(values)})", values)
        return jobs

    def prune_corrupt(self) -> list[str]:
        """Delete the rows marked corrupt and return their ids, so their work folders can be swept.

        Each row's id, state and data (cut to CORRUPT_DATA_LIMIT characters) is first appended as a JSON
        line to the evidence file. If that can't be written the rows stay (they are never loaded or
        counted anyway) and nothing is returned. Call it after loading the queue, so rows found corrupt
        while loading go too.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, state, data FROM jobs WHERE state = ? ORDER BY created_at, rowid", (CORRUPT_STATE,),
            ).fetchall()
            if not rows:
                return []
            evidence = self._corrupt_log or _beside_database(self._conn, CORRUPT_LOG_NAME)
            if evidence is not None:
                try:
                    _append_evidence(evidence, rows)
                except OSError:
                    log.warning(
                        "Couldn't save %d unreadable job(s) to %s; keeping them", len(rows), evidence, exc_info=True,
                    )
                    return []
            ids = [row["id"] for row in rows]
            self._conn.execute(f"DELETE FROM jobs WHERE id IN ({_marks(ids)})", ids)
        where = f"; copies are in {evidence}" if evidence is not None else ""
        log.warning("Deleted %d unreadable job(s): %s%s", len(ids), ", ".join(sorted(ids)), where)
        return ids

    def delete(self, job_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))

    def all_ids(self) -> set[str]:
        """The id of every readable stored job (corrupt rows left out); their work folders are in use."""
        with self._lock:
            return {
                row["id"] for row in self._conn.execute("SELECT id FROM jobs WHERE state != ?", (CORRUPT_STATE,))
            }

    def _load_states(self, states: Iterable[JobState]) -> list[Job]:
        with self._lock:
            return self._decode(self._select_states(states))

    def _select_states(self, states: Iterable[JobState]) -> list:
        values = [state.value for state in states]
        return self._conn.execute(
            f"SELECT id, data FROM jobs WHERE state IN ({_marks(values)}) ORDER BY created_at, rowid", values,
        ).fetchall()

    def _try_decode(self, row) -> Job | None:
        """The row's job, or None (logged) if it can't be read."""
        try:
            return job_from_dict(json.loads(row["data"]))
        except (ValueError, TypeError, KeyError, RecursionError) as error:  # Recursion: nested too deep
            log.warning("Job %s can't be read (%s)", row["id"], error)
            return None

    def _decode(self, rows: list) -> list[Job]:
        """Decode rows one by one; a row that fails is marked corrupt instead of breaking the rest."""
        jobs: list[Job] = []
        for row in rows:
            job = self._try_decode(row)
            if job is None:
                self._conn.execute("UPDATE jobs SET state = ? WHERE id = ?", (CORRUPT_STATE, row["id"]))
            else:
                jobs.append(job)
        return jobs


def _marks(values: list) -> str:
    return ", ".join("?" for _ in values)


def _beside_database(conn: Connection, name: str) -> Path | None:
    """name in the folder of the connection's main database file; None for an in-memory database."""
    for row in conn.execute("PRAGMA database_list"):
        if row["name"] == "main":
            return Path(row["file"]).with_name(name) if row["file"] else None
    return None


def _as_text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value if isinstance(value, str) else repr(value)


def _append_evidence(path: Path, rows: list) -> None:
    found_at = time.time()
    lines = [
        json.dumps({
            "id": _as_text(row["id"]), "state": _as_text(row["state"]),
            "data": _as_text(row["data"])[:CORRUPT_DATA_LIMIT], "found_at": found_at,
        })
        for row in rows
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as file:
        file.write("".join(f"{line}\n" for line in lines))
