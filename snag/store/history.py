"""Finished and failed downloads, for the History tab and duplicate warnings.
Deleting history never touches the downloaded files."""
from __future__ import annotations

import re
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass, fields
from pathlib import Path

from snag.jobs.command import result_exts
from snag.jobs.models import Job, JobSpec, JobState, gets_sponsorblock, gets_subtitles, is_trimmed, summary_base

from .db import Connection

_EXT = re.compile(r"[A-Za-z0-9]{1,5}")
_IDS_PER_QUERY = 500  # well under SQLite's limit on parameters in one statement


@dataclass(frozen=True)
class HistoryEntry:
    id: int
    site: str
    video_id: str
    url: str
    title: str
    channel: str | None
    kind: str  # "video" or "audio"
    format_summary: str
    file_path: str | None
    size: int | None
    finished_at: float
    status: str  # "done" or "failed"
    error_message: str | None


_ENTRY_FIELDS = tuple(f.name for f in fields(HistoryEntry))
# What add() writes, in the order of its values.
_ADDED_COLUMNS = (
    "job_id", "batch_id", "site", "video_id", "url", "title", "channel", "kind", "format_summary", "file_path",
    "size", "finished_at", "status", "error_message",
)


def _ext_label(spec: JobSpec, file_path: Path | None) -> str:
    """The container as shown to people ("MP4"), or "" when nothing trustworthy is known. Without a
    file (a failed download), the one the file would have had: a merge or an audio extraction can
    change it from the chosen format's own (AV1 + Opus gives WEBM), so the chosen ext is only used
    when that can't be known in advance."""
    expected = result_exts(spec.choice)
    planned = next(iter(expected)) if len(expected) == 1 else spec.choice.ext or ""
    for candidate in (file_path.suffix[1:] if file_path else "", planned):
        if _EXT.fullmatch(candidate):
            return candidate.upper()
    return ""


def format_summary(spec: JobSpec, file_path: Path | None, *, subtitles_skipped: bool = False) -> str:
    """e.g. "1080p MP4 + subtitles"; never empty. Only lists what the download really got (a trim can
    skip subtitles and SponsorBlock). subtitles_skipped: the subtitle download failed, so the file was
    saved without them (shown instead of "subtitles")."""
    choice = spec.choice
    parts = [summary_base(choice, _ext_label(spec, file_path))]
    extras = spec.extras
    if gets_subtitles(extras, choice):
        parts.append("saved without subtitles" if subtitles_skipped else "subtitles")
    if is_trimmed(extras):
        parts.append("trimmed")
    if gets_sponsorblock(extras):
        parts.append("SponsorBlock")
    return " + ".join(parts)


def _filter(search: str, kind: str | None) -> tuple[str, list[object]]:
    """The WHERE clause (and its parameters) shared by list() and count()."""
    clauses, params = ["1 = 1"], []
    search = search.strip()
    if search:
        clauses.append("instr(snag_casefold(title), ?) > 0")  # snag_casefold: see db._connect
        params.append(search.casefold())
    if kind:
        clauses.append("kind = ?")
        params.append(kind)
    return " AND ".join(clauses), params


def _entry(row: sqlite3.Row) -> HistoryEntry:
    return HistoryEntry(**{name: row[name] for name in _ENTRY_FIELDS})


class HistoryStore:
    def __init__(self, conn: Connection):
        self._conn = conn
        self._lock = conn.lock

    def add(self, job: Job) -> int:
        """Record a finished (DONE or FAILED) job and return its entry's id.

        There is one entry per download: a job that already has one (a retried download) has it
        updated in place, so a failure followed by a successful retry leaves a single "done" entry.
        A job whose entry was deleted gets a new one.
        """
        if job.state not in (JobState.DONE, JobState.FAILED):
            raise ValueError(f"only done or failed jobs go into history, not {job.state.value}")
        spec = job.spec
        size = job.file_path.stat().st_size if job.file_path and job.file_path.is_file() else None
        columns = ", ".join(_ADDED_COLUMNS)
        updates = ", ".join(f"{name} = excluded.{name}" for name in _ADDED_COLUMNS if name != "job_id")
        with self._lock:
            row = self._conn.execute(
                f"""INSERT INTO history({columns}) VALUES({", ".join("?" * len(_ADDED_COLUMNS))})
                    ON CONFLICT(job_id) DO UPDATE SET {updates}
                    RETURNING id""",
                (
                    job.id, spec.batch_id,
                    spec.site, spec.video_id, spec.url, spec.title, spec.channel,
                    "audio" if spec.choice.audio_only else "video",
                    format_summary(spec, job.file_path, subtitles_skipped=job.subtitles_skipped),
                    str(job.file_path) if job.file_path else None,
                    size,
                    time.time() if job.finished_at is None else job.finished_at,
                    job.state.value,
                    job.error.message if job.error else None,
                ),
            ).fetchone()
        return row[0]

    def find_duplicate(self, site: str, video_id: str) -> HistoryEntry | None:
        with self._lock:
            row = self._conn.execute(
                """SELECT * FROM history WHERE site = ? AND video_id = ? AND status = 'done'
                   ORDER BY finished_at DESC, id DESC LIMIT 1""",
                (site, video_id),
            ).fetchone()
        return _entry(row) if row else None

    def find_duplicates(self, site: str, video_ids: Iterable[str]) -> dict[str, HistoryEntry]:
        """find_duplicate for many videos of one site at once (a playlist's entries): video id -> its
        latest successful download, for the videos that have one."""
        wanted = list(dict.fromkeys(video_ids))
        found: dict[str, HistoryEntry] = {}
        for start in range(0, len(wanted), _IDS_PER_QUERY):
            chunk = wanted[start:start + _IDS_PER_QUERY]
            with self._lock:
                rows = self._conn.execute(
                    f"""SELECT * FROM history
                        WHERE site = ? AND status = 'done' AND video_id IN ({", ".join("?" * len(chunk))})
                        ORDER BY finished_at DESC, id DESC""",
                    (site, *chunk),
                ).fetchall()
            for row in rows:
                found.setdefault(row["video_id"], _entry(row))  # newest first: keep the first seen
        return found

    def get(self, entry_id: int) -> HistoryEntry | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM history WHERE id = ?", (entry_id,)).fetchone()
        return _entry(row) if row else None

    def list(self, search: str = "", kind: str | None = None, limit: int = 100, offset: int = 0) -> list[HistoryEntry]:
        """Newest first. search matches titles literally and case-insensitively; blank means everything."""
        where, params = _filter(search, kind)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM history WHERE {where} ORDER BY finished_at DESC, id DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        return [_entry(row) for row in rows]

    def count(self, search: str = "", kind: str | None = None) -> int:
        """How many entries list() pages through for the same search and kind."""
        where, params = _filter(search, kind)
        with self._lock:
            return self._conn.execute(f"SELECT COUNT(*) FROM history WHERE {where}", params).fetchone()[0]

    def batch_statuses(self, batch_id: str) -> dict[str, str]:
        """Job id -> "done" or "failed" for every entry of a batch, so a batch's summary can count jobs
        the queue no longer holds (finished in an earlier session, or cleared). Entries recorded before
        History kept batch ids, and deleted entries, aren't included."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT job_id, status FROM history WHERE batch_id = ? AND job_id IS NOT NULL", (batch_id,),
            ).fetchall()
        return {row["job_id"]: row["status"] for row in rows}

    def delete(self, entry_id: int) -> bool:
        """Delete one entry; False if there was no such entry."""
        with self._lock:
            return self._conn.execute("DELETE FROM history WHERE id = ?", (entry_id,)).rowcount > 0

    def clear(self) -> int:
        """Delete every entry; returns how many there were."""
        with self._lock:
            return self._conn.execute("DELETE FROM history").rowcount
