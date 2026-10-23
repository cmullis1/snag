import logging
import sqlite3
import threading

import pytest

import snag.store.db as db
from snag.store.db import SCHEMA_VERSION, Connection, open_db


def user_version(conn) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def indexes(conn) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}


def test_new_database_gets_the_latest_schema(tmp_path):
    conn = open_db(tmp_path / "snag.db")
    try:
        assert user_version(conn) == SCHEMA_VERSION
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"jobs", "history", "settings"} <= tables
        assert {"history_video", "history_finished"} <= indexes(conn)
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        conn.close()


def test_connection_carries_a_shared_lock(tmp_path):
    conn = open_db(tmp_path / "snag.db")
    try:
        assert isinstance(conn, Connection)
        assert isinstance(conn.lock, type(threading.RLock()))
    finally:
        conn.close()


def test_older_database_is_migrated_in_order(tmp_path):
    path = tmp_path / "snag.db"
    old = sqlite3.connect(path, isolation_level=None)
    for statement in db.MIGRATIONS[0]:  # build a version-1 database
        old.execute(statement)
    old.execute("PRAGMA user_version = 1")
    old.execute("INSERT INTO settings(key, value) VALUES('max_concurrent', '3')")
    old.close()
    conn = open_db(path)
    try:
        assert user_version(conn) == SCHEMA_VERSION
        assert "history_finished" in indexes(conn)
        assert conn.execute("SELECT value FROM settings").fetchone()[0] == "3"  # data kept
    finally:
        conn.close()


def test_reopening_is_harmless(tmp_path):
    open_db(tmp_path / "snag.db").close()
    conn = open_db(tmp_path / "snag.db")
    assert user_version(conn) == SCHEMA_VERSION
    conn.close()


def test_failed_migration_leaves_the_database_untouched(tmp_path, monkeypatch):
    path = tmp_path / "snag.db"
    open_db(path).close()
    monkeypatch.setattr(db, "MIGRATIONS", (*db.MIGRATIONS, ("CREATE TABLE extra(x)", "THIS IS NOT SQL")))
    monkeypatch.setattr(db, "SCHEMA_VERSION", len(db.MIGRATIONS))
    with pytest.raises(sqlite3.OperationalError):
        open_db(path)
    conn = sqlite3.connect(path)
    try:
        assert user_version(conn) == SCHEMA_VERSION  # the module's real version, unchanged
        assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'extra'").fetchone() is None
    finally:
        conn.close()
    assert path.exists() and not list(tmp_path.glob("*.corrupt-*"))


def test_database_from_a_newer_snag_is_refused(tmp_path):
    path = tmp_path / "snag.db"
    newer = sqlite3.connect(path, isolation_level=None)
    newer.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    newer.close()
    from snag.store.db import NewerDatabaseError

    with pytest.raises(NewerDatabaseError, match="newer version of Snag"):
        open_db(path)
    assert path.exists() and not list(tmp_path.glob("*.corrupt-*"))  # never set aside


def test_garbage_file_is_set_aside_and_replaced(tmp_path, caplog):
    path = tmp_path / "snag.db"
    path.write_bytes(b"this is not a database" * 500)
    with caplog.at_level(logging.WARNING, logger="snag.store.db"):
        conn = open_db(path)
    try:
        assert user_version(conn) == SCHEMA_VERSION
        assert conn.execute("SELECT COUNT(*) FROM history").fetchone()[0] == 0
    finally:
        conn.close()
    [corrupt] = tmp_path.glob("snag.db.corrupt-*")
    assert corrupt.read_bytes().startswith(b"this is not a database")
    [message] = [record.getMessage() for record in caplog.records]
    assert message.startswith("Database ") and str(corrupt) in message


def test_set_aside_takes_the_wal_files_along(tmp_path):
    path = tmp_path / "snag.db"
    for name in ("snag.db", "snag.db-wal", "snag.db-shm"):
        (tmp_path / name).write_bytes(name.encode())
    moved = db._set_aside(path)
    assert not any(tmp_path.glob("snag.db-*")) and not path.exists()
    assert moved.read_bytes() == b"snag.db"
    assert (tmp_path / f"{moved.name}-wal").read_bytes() == b"snag.db-wal"
    assert (tmp_path / f"{moved.name}-shm").read_bytes() == b"snag.db-shm"


def test_damaged_pages_are_detected_at_open(tmp_path):
    path = tmp_path / "snag.db"
    conn = open_db(path)
    conn.executemany(
        "INSERT INTO settings(key, value) VALUES(?, ?)", [(f"k{i}", "x" * 200) for i in range(500)],
    )
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    data = bytearray(path.read_bytes())
    data[4096 * 3:4096 * 6] = b"\xff" * (4096 * 3)
    path.write_bytes(bytes(data))
    conn = open_db(path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 0
    finally:
        conn.close()
    assert list(tmp_path.glob("snag.db.corrupt-*"))


def test_set_aside_names_never_collide(tmp_path, monkeypatch):
    monkeypatch.setattr(db.time, "strftime", lambda fmt: "20261006-120000")
    for _ in range(2):
        (tmp_path / "snag.db").write_bytes(b"garbage" * 500)
        open_db(tmp_path / "snag.db").close()
    assert sorted(p.name for p in tmp_path.glob("snag.db.corrupt-*")) == [
        "snag.db.corrupt-20261006-120000", "snag.db.corrupt-20261006-120000-2",
    ]


def test_casefold_function_is_ready_as_soon_as_the_database_opens(tmp_path):
    conn = open_db(tmp_path / "snag.db")
    try:
        assert conn.execute("SELECT snag_casefold('ÉCOLE Straße')").fetchone()[0] == "école strasse"
        assert conn.execute("SELECT snag_casefold(NULL)").fetchone()[0] is None
    finally:
        conn.close()


def test_close_waits_for_the_connection_lock(tmp_path):
    conn = open_db(tmp_path / "snag.db")
    holding, release = threading.Event(), threading.Event()

    def hold_lock():
        with conn.lock:
            holding.set()
            release.wait(10)

    holder = threading.Thread(target=hold_lock)
    holder.start()
    holding.wait(10)
    closer = threading.Thread(target=conn.close)
    closer.start()
    closer.join(0.3)
    assert closer.is_alive()  # blocked behind the holder
    conn.execute("SELECT 1")  # still open
    release.set()
    holder.join(10)
    closer.join(10)
    assert not closer.is_alive()
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def columns(conn, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def test_history_entries_are_keyed_by_their_job(tmp_path):
    conn = open_db(tmp_path / "snag.db")
    try:
        assert "job_id" in columns(conn, "history")
        assert "history_job" in indexes(conn)
    finally:
        conn.close()


def test_version_2_history_is_kept_without_job_ids(tmp_path):
    path = tmp_path / "snag.db"
    old = sqlite3.connect(path, isolation_level=None)
    for step in db.MIGRATIONS[:2]:  # build a version-2 database
        for statement in step:
            old.execute(statement)
    old.execute("PRAGMA user_version = 2")
    for title in ("Old one", "Old two"):
        old.execute(
            "INSERT INTO history(site, video_id, url, title, kind, format_summary, finished_at, status)"
            " VALUES('youtube', 'v', 'u', ?, 'video', 'MP4', 1, 'done')",
            (title,),
        )
    old.close()
    conn = open_db(path)
    try:
        assert user_version(conn) == SCHEMA_VERSION
        rows = conn.execute("SELECT title, job_id FROM history ORDER BY id").fetchall()
        assert [tuple(row) for row in rows] == [("Old one", None), ("Old two", None)]
    finally:
        conn.close()


def test_history_entries_remember_their_batch(tmp_path):
    conn = open_db(tmp_path / "snag.db")
    try:
        assert SCHEMA_VERSION == 4
        assert "batch_id" in columns(conn, "history")
        assert "history_batch" in indexes(conn)
    finally:
        conn.close()


def test_version_3_history_is_kept_without_batch_ids(tmp_path):
    path = tmp_path / "snag.db"
    old = sqlite3.connect(path, isolation_level=None)
    for step in db.MIGRATIONS[:3]:  # build a version-3 database
        for statement in step:
            old.execute(statement)
    old.execute("PRAGMA user_version = 3")
    old.execute(
        "INSERT INTO history(job_id, site, video_id, url, title, kind, format_summary, finished_at, status)"
        " VALUES('j1', 'youtube', 'v', 'u', 'Old one', 'video', 'MP4', 1, 'done')"
    )
    old.close()
    conn = open_db(path)
    try:
        assert user_version(conn) == 4
        rows = conn.execute("SELECT job_id, title, batch_id FROM history").fetchall()
        assert [tuple(row) for row in rows] == [("j1", "Old one", None)]
    finally:
        conn.close()
