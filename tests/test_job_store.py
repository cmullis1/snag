import json
import logging
import threading

import pytest

from snag.jobs.models import Job, JobState, job_to_dict
from snag.store.db import open_db
from snag.store.jobs import CORRUPT_DATA_LIMIT, CORRUPT_STATE, JobStore
from tests.helpers import make_spec


@pytest.fixture
def conn(tmp_path):
    conn = open_db(tmp_path / "snag.db")
    yield conn
    conn.close()


@pytest.fixture
def store(conn):
    return JobStore(conn)


def job_in(tmp_path, state, created_at):
    job = Job(spec=make_spec("fake://success", tmp_path), created_at=created_at)
    job.state = state
    return job


def put_raw(conn, job_id: str, state: str, data: str, created_at: float) -> None:
    conn.execute("INSERT INTO jobs(id, state, data, created_at) VALUES(?, ?, ?, ?)", (job_id, state, data, created_at))


def state_of(conn, job_id: str) -> str:
    return conn.execute("SELECT state FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]


def test_save_and_load(tmp_path, store):
    job = job_in(tmp_path, JobState.DOWNLOADING, 1.0)
    store.save(job)
    job.state = JobState.PROCESSING
    store.save(job)
    assert store.load(job.id) == job
    assert store.load("missing") is None


def test_load_unfinished_in_creation_order(tmp_path, store):
    late = job_in(tmp_path, JobState.WAITING, 3.0)
    early = job_in(tmp_path, JobState.DOWNLOADING, 1.0)
    interrupted = job_in(tmp_path, JobState.INTERRUPTED, 2.0)
    done = job_in(tmp_path, JobState.DONE, 2.5)
    failed = job_in(tmp_path, JobState.FAILED, 0.5)
    for job in (late, early, interrupted, done, failed):
        store.save(job)
    assert [j.id for j in store.load_unfinished()] == [early.id, interrupted.id, late.id]


def test_load_failed_in_creation_order(tmp_path, store):
    second = job_in(tmp_path, JobState.FAILED, 2.0)
    first = job_in(tmp_path, JobState.FAILED, 1.0)
    for job in (second, first, job_in(tmp_path, JobState.WAITING, 0.5), job_in(tmp_path, JobState.DONE, 0.7)):
        store.save(job)
    assert [j.id for j in store.load_failed()] == [first.id, second.id]


def test_prune_removes_only_done_and_cancelled(tmp_path, store):
    jobs = {
        state: job_in(tmp_path, state, at)
        for at, state in enumerate([JobState.DONE, JobState.FAILED, JobState.CANCELLED, JobState.WAITING, JobState.INTERRUPTED])
    }
    for job in jobs.values():
        store.save(job)
    pruned = store.prune_finished()
    assert sorted(j.id for j in pruned) == sorted([jobs[JobState.DONE].id, jobs[JobState.CANCELLED].id])
    assert store.load(jobs[JobState.DONE].id) is None and store.load(jobs[JobState.CANCELLED].id) is None
    assert [j.id for j in store.load_failed()] == [jobs[JobState.FAILED].id]  # failed jobs survive restarts
    assert {j.id for j in store.load_unfinished()} == {jobs[JobState.WAITING].id, jobs[JobState.INTERRUPTED].id}


def test_delete_and_all_ids(tmp_path, store):
    first, second = job_in(tmp_path, JobState.FAILED, 1.0), job_in(tmp_path, JobState.WAITING, 2.0)
    store.save(first)
    store.save(second)
    assert store.all_ids() == {first.id, second.id}
    store.delete(first.id)
    store.delete("missing")
    assert store.all_ids() == {second.id}
    assert store.load(first.id) is None


@pytest.mark.parametrize("data", [
    "{not json",
    json.dumps({"id": "x"}),
    json.dumps({"id": "x", "state": "waiting", "spec": {"url": 5}}),
    json.dumps([1, 2, 3]),
    pytest.param("[" * 100_000 + "]" * 100_000, id="too-deeply-nested"),
])
def test_corrupt_row_is_kept_aside_without_breaking_loading(tmp_path, store, conn, data, caplog):
    good = job_in(tmp_path, JobState.WAITING, 2.0)
    store.save(good)
    put_raw(conn, "bad", "waiting", data, 1.0)
    with caplog.at_level(logging.WARNING, logger="snag.store.jobs"):
        assert [j.id for j in store.load_unfinished()] == [good.id]
    assert "bad" in caplog.text
    assert state_of(conn, "bad") == CORRUPT_STATE  # kept for diagnosis
    assert store.load_unfinished() == [good]
    assert store.load("bad") is None
    assert "bad" not in store.all_ids()  # its work folder is not the queue's any more


def test_corrupt_failed_rows_are_marked(tmp_path, store, conn):
    put_raw(conn, "bad-failed", "failed", "{", 1.0)
    assert store.load_failed() == []
    assert state_of(conn, "bad-failed") == CORRUPT_STATE


def ids_in_table(conn) -> set[str]:
    return {row[0] for row in conn.execute("SELECT id FROM jobs")}


def test_prune_finished_deletes_undecodable_done_and_cancelled_rows(tmp_path, store, conn, caplog):
    done = job_in(tmp_path, JobState.DONE, 1.0)
    store.save(done)
    put_raw(conn, "bad-done", "done", "{", 2.0)
    put_raw(conn, "bad-cancelled", "cancelled", json.dumps({"id": "bad-cancelled"}), 3.0)
    put_raw(conn, "bad-failed", "failed", "{", 4.0)
    put_raw(conn, "already-corrupt", CORRUPT_STATE, "{", 5.0)
    with caplog.at_level(logging.WARNING, logger="snag.store.jobs"):
        assert store.prune_finished() == [done]  # only the rows it could read
    assert "bad-done" in caplog.text and "bad-cancelled" in caplog.text
    assert ids_in_table(conn) == {"bad-failed", "already-corrupt"}  # never left behind as corrupt rows


def test_prune_corrupt_deletes_corrupt_rows_and_returns_their_ids(tmp_path, store, conn, caplog):
    good = job_in(tmp_path, JobState.WAITING, 1.0)
    store.save(good)
    put_raw(conn, "found-earlier", CORRUPT_STATE, "{", 2.0)
    put_raw(conn, "found-now", "waiting", "{", 3.0)
    assert store.load_unfinished() == [good]  # marks found-now corrupt
    with caplog.at_level(logging.WARNING, logger="snag.store.jobs"):
        assert sorted(store.prune_corrupt()) == ["found-earlier", "found-now"]
    assert "found-earlier" in caplog.text and "found-now" in caplog.text
    assert ids_in_table(conn) == {good.id}
    assert store.prune_corrupt() == []


def evidence_lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_prune_corrupt_keeps_evidence_next_to_the_database(tmp_path, store, conn, caplog):
    put_raw(conn, "short", CORRUPT_STATE, "{", 1.0)
    put_raw(conn, "long", CORRUPT_STATE, "x" * 20_000, 2.0)
    with caplog.at_level(logging.WARNING, logger="snag.store.jobs"):
        assert sorted(store.prune_corrupt()) == ["long", "short"]
    evidence = tmp_path / "jobs-corrupt.jsonl"
    records = {r["id"]: r for r in evidence_lines(evidence)}
    assert records["short"]["state"] == CORRUPT_STATE and records["short"]["data"] == "{"
    assert records["long"]["data"] == "x" * CORRUPT_DATA_LIMIT
    assert str(evidence) in caplog.text


def test_prune_corrupt_appends_to_earlier_evidence(tmp_path, store, conn):
    put_raw(conn, "first", CORRUPT_STATE, "{", 1.0)
    store.prune_corrupt()
    put_raw(conn, "second", CORRUPT_STATE, "[", 2.0)
    store.prune_corrupt()
    assert [r["id"] for r in evidence_lines(tmp_path / "jobs-corrupt.jsonl")] == ["first", "second"]


def test_prune_corrupt_writes_evidence_where_told(tmp_path, conn):
    target = tmp_path / "elsewhere" / "bad-rows.jsonl"
    store = JobStore(conn, corrupt_log=target)
    put_raw(conn, "bad", CORRUPT_STATE, "{", 1.0)
    assert store.prune_corrupt() == ["bad"]
    assert [r["id"] for r in evidence_lines(target)] == ["bad"]
    assert not (tmp_path / "jobs-corrupt.jsonl").exists()


def test_prune_corrupt_keeps_the_rows_when_evidence_cant_be_written(tmp_path, conn, caplog):
    blocked = tmp_path / "a-folder"
    blocked.mkdir()  # a folder where the file should go: opening it fails
    store = JobStore(conn, corrupt_log=blocked)
    put_raw(conn, "bad", CORRUPT_STATE, "{", 1.0)
    with caplog.at_level(logging.WARNING, logger="snag.store.jobs"):
        assert store.prune_corrupt() == []
    assert ids_in_table(conn) == {"bad"}
    assert "bad" not in store.all_ids()  # still never loaded or counted
    assert str(blocked) in caplog.text


def test_prune_corrupt_without_corrupt_rows_writes_nothing(tmp_path, store):
    assert store.prune_corrupt() == []
    assert not (tmp_path / "jobs-corrupt.jsonl").exists()


def test_all_ids_leaves_out_corrupt_rows(tmp_path, store, conn):
    good = job_in(tmp_path, JobState.FAILED, 1.0)
    store.save(good)
    put_raw(conn, "bad", CORRUPT_STATE, "{", 2.0)
    assert store.all_ids() == {good.id}


def test_rows_from_a_newer_snag_still_load(tmp_path, store, conn):
    job = job_in(tmp_path, JobState.WAITING, 1.0)
    data = job_to_dict(job)
    data["priority"] = "high"
    put_raw(conn, job.id, "waiting", json.dumps(data), 1.0)
    assert store.load_unfinished() == [job]


def test_concurrent_saves(tmp_path, store):
    jobs = [job_in(tmp_path, JobState.WAITING, float(n)) for n in range(40)]
    threads = [threading.Thread(target=store.save, args=(job,)) for job in jobs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert [j.id for j in store.load_unfinished()] == [j.id for j in jobs]


def test_row_with_a_mistyped_number_is_marked_corrupt(tmp_path, store, conn):
    job = job_in(tmp_path, JobState.WAITING, 1.0)
    data = job_to_dict(job)
    data["percent"] = "50%"
    put_raw(conn, job.id, "waiting", json.dumps(data), 1.0)
    assert store.load_unfinished() == []
    assert state_of(conn, job.id) == CORRUPT_STATE


def test_log_messages_are_sentence_case(tmp_path, conn, caplog):
    blocked = tmp_path / "a-folder"
    blocked.mkdir()
    put_raw(conn, "unreadable", "waiting", "{", 1.0)
    with caplog.at_level(logging.WARNING, logger="snag.store.jobs"):
        JobStore(conn).load_unfinished()  # "can't be read"
        JobStore(conn, corrupt_log=blocked).prune_corrupt()  # "couldn't save"
        JobStore(conn, corrupt_log=tmp_path / "evidence.jsonl").prune_corrupt()  # "deleted"
    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 3
    assert all(message[0].isupper() for message in messages), messages
