import test from "node:test";
import assert from "node:assert/strict";

import { initialState, reducer, createStore, jobList, bindHub } from "../../ui/store.js";

function job(id, createdAt, extra = {}) {
  return {
    id, state: "waiting", percent: null, speed: null, eta: null, stage: null, error: null, created_at: createdAt,
    spec: { url: `https://example.com/${id}`, title: `Video ${id}` }, ...extra,
  };
}

const QUEUE = { paused: false, held: false, throttled: false, throttled_until: null, current_limit: 5, running: 0, waiting: 2 };

function snapshot(jobs = [job("b", 20), job("a", 10)]) {
  return { type: "snapshot", jobs, queue: QUEUE, status: { version: "0.1.0", youtube_login: null } };
}

test("initial state", () => {
  const state = initialState();
  assert.equal(state.connection, "disconnected", "the same as a hub that hasn't connected yet");
  assert.equal(state.ready, false);
  assert.deepEqual(Object.keys(state.jobs), []);
  assert.equal(state.queue, null);
  assert.deepEqual(state.status, {});
  assert.deepEqual(state.setup, {});
  assert.deepEqual(state.updates, {});
  assert.equal(state.settings, null);
});

const SETTINGS = { video_dir: "C:\Videos\Snag", audio_dir: "C:\Music\Snag", max_concurrent: 3 };

test("a settings event replaces the settings, and a snapshot keeps them", () => {
  const start = reducer(initialState(), snapshot());
  const state = reducer(start, { type: "settings", settings: SETTINGS });
  assert.deepEqual(state.settings, SETTINGS);
  const moved = reducer(state, { type: "settings", settings: { ...SETTINGS, video_dir: "D:\Clips" } });
  assert.equal(moved.settings.video_dir, "D:\Clips");
  assert.deepEqual(reducer(moved, snapshot()).settings, moved.settings, "a snapshot without settings keeps them");
});

test("a snapshot's settings replace the settings", () => {
  const start = reducer(initialState(), { type: "settings", settings: SETTINGS });
  const state = reducer(start, { ...snapshot(), settings: { ...SETTINGS, max_concurrent: 7 } });
  assert.equal(state.settings.max_concurrent, 7);
});

test("a settings event without settings changes nothing", () => {
  const start = reducer(reducer(initialState(), snapshot()), { type: "settings", settings: SETTINGS });
  for (const settings of [undefined, null, "x", [1]]) assert.equal(reducer(start, { type: "settings", settings }), start);
});

test("a snapshot replaces jobs, queue and status", () => {
  const before = reducer(initialState(), { type: "job", job: job("old", 1) });
  const state = reducer(before, snapshot());
  assert.equal(state.ready, true);
  assert.deepEqual(Object.keys(state.jobs).sort(), ["a", "b"]);
  assert.deepEqual(state.queue, QUEUE);
  assert.deepEqual(state.status, { version: "0.1.0", youtube_login: null });
});

test("jobs are listed by created_at, oldest first, or newest first", () => {
  let state = reducer(initialState(), snapshot([job("c", 30), job("a", 10), job("b", 20)]));
  state = reducer(state, { type: "job", job: job("d", 15) });
  assert.deepEqual(jobList(state).map((j) => j.id), ["a", "d", "b", "c"]);
  assert.deepEqual(jobList(state, { newestFirst: true }).map((j) => j.id), ["c", "b", "d", "a"]);
});

test("jobs created at the same moment keep a stable order", () => {
  const state = reducer(initialState(), snapshot([job("y", 5), job("x", 5)]));
  assert.deepEqual(jobList(state).map((j) => j.id), ["x", "y"]);
});

test("a job event adds or replaces one job and leaves the others alone", () => {
  const start = reducer(initialState(), snapshot());
  const updated = job("a", 10, { state: "downloading", percent: 42 });
  const state = reducer(start, { type: "job", job: updated });
  assert.equal(state.jobs["a"].percent, 42);
  assert.equal(state.jobs.b, start.jobs.b);
  assert.notEqual(state.jobs, start.jobs);
  assert.equal(start.jobs.a.state, "waiting", "the old state is not changed");
});

test("a job event without a nested job is ignored", () => {
  const start = reducer(initialState(), snapshot());
  assert.equal(reducer(start, { type: "job", ...job("flat", 3, { state: "done" }) }), start);
  assert.equal(reducer(start, { type: "job", job: "a" }), start);
  assert.equal(reducer(start, { type: "job", job: { id: 7 } }), start);
});

test("job ids that are Object.prototype names are just ids", () => {
  let state = reducer(initialState(), snapshot([job("__proto__", 1), job("toString", 2)]));
  assert.equal(state.jobs["__proto__"].id, "__proto__");
  assert.equal(state.jobs["toString"].id, "toString");
  assert.equal(state.jobs["hasOwnProperty"], undefined);
  assert.equal(state.jobs["constructor"], undefined);
  assert.deepEqual(jobList(state).map((j) => j.id), ["__proto__", "toString"]);
  state = reducer(state, { type: "job", job: job("constructor", 3) });
  assert.equal(state.jobs["constructor"].id, "constructor");
  state = reducer(state, { type: "job_removed", id: "__proto__" });
  assert.equal(state.jobs["__proto__"], undefined);
  assert.equal(reducer(state, { type: "job_removed", id: "valueOf" }), state);
  assert.deepEqual(jobList(state).map((j) => j.id), ["toString", "constructor"]);
});

test("job_removed drops the job; an unknown id changes nothing", () => {
  const start = reducer(initialState(), snapshot());
  const state = reducer(start, { type: "job_removed", id: "a" });
  assert.equal(state.jobs["a"], undefined);
  assert.ok(state.jobs["b"]);
  assert.equal(reducer(state, { type: "job_removed", id: "nope" }), state);
});

test("queue events replace the queue state; one without a nested queue is ignored", () => {
  const paused = { ...QUEUE, paused: true };
  assert.deepEqual(reducer(initialState(), { type: "queue", queue: paused }).queue, paused);
  const start = initialState();
  assert.equal(reducer(start, { type: "queue", ...paused }), start);
});

test("status events merge into the status", () => {
  const start = reducer(initialState(), snapshot());
  const updater = { setup: { state: "done", tools: {} }, update: { available: "2026.10.01" } };
  const state = reducer(start, { type: "status", updater });
  assert.equal(state.status.version, "0.1.0");
  assert.deepEqual(state.status.updater, updater);
});

test("a status event with no fields of its own changes nothing", () => {
  const start = reducer(initialState(), snapshot());
  assert.equal(reducer(start, { type: "status" }), start);
  const store = createStore(start);
  let heard = 0;
  store.subscribe(() => { heard += 1; });
  store.dispatch({ type: "status" });
  assert.equal(heard, 0);
});

test("a status carrying the queue updates the queue too", () => {
  const paused = { ...QUEUE, paused: true };
  const state = reducer(initialState(), { type: "status", queue: paused, version: "0.1.0" });
  assert.deepEqual(state.queue, paused);
  assert.equal("queue" in state.status, false, "state.queue is the only copy of the queue");
});

test("the snapshot's queue is kept once, in state.queue", () => {
  const state = reducer(initialState(), { ...snapshot(), status: { version: "0.1.0", queue: QUEUE } });
  assert.deepEqual(state.queue, QUEUE);
  assert.equal("queue" in state.status, false);
  const fromStatus = reducer(initialState(), { type: "snapshot", jobs: [], status: { queue: QUEUE } });
  assert.deepEqual(fromStatus.queue, QUEUE, "a snapshot without a top-level queue uses the status's");
});

test("a queue event then a status event without a queue keeps the newer queue", () => {
  const start = reducer(initialState(), { ...snapshot(), status: { version: "0.1.0", queue: QUEUE } });
  const paused = { ...QUEUE, paused: true };
  let state = reducer(start, { type: "queue", queue: paused });
  state = reducer(state, { type: "status", youtube_login: "2026-10-07T12:00:00Z" });
  assert.deepEqual(state.queue, paused);
  state = reducer(state, { type: "status", updater: { setup: { state: "done", tools: {} } } });
  assert.deepEqual(state.queue, paused);
  assert.equal(state.status.youtube_login, "2026-10-07T12:00:00Z");
});

test("setup progress then a status event without setup tools keeps the progress", () => {
  const tools = { ffmpeg: { phase: null, done: 0, total: null, message: null } };
  let state = reducer(initialState(), { ...snapshot(), status: { updater: { setup: { state: "running", tools } } } });
  assert.deepEqual(state.setup, tools);
  const progress = { phase: "downloading", done: 50, total: 100, message: null };
  state = reducer(state, { type: "setup", tool: "ffmpeg", ...progress });
  state = reducer(state, { type: "status", youtube_login: null });
  assert.deepEqual(state.setup.ffmpeg, progress);
  state = reducer(state, { type: "status", tools: { ready: false } });
  assert.deepEqual(state.setup.ffmpeg, progress);
  state = reducer(state, { type: "status", updater: { update: { available: null } } });
  assert.deepEqual(state.setup.ffmpeg, progress, "an updater status without setup tools leaves them alone");
});

test("a status event's own setup tools replace the setup progress", () => {
  let state = reducer(initialState(), { type: "setup", tool: "ffmpeg", phase: "downloading", done: 1, total: 2, message: null });
  const tools = { ffmpeg: { phase: "done", done: 2, total: 2, message: null } };
  state = reducer(state, { type: "status", updater: { setup: { state: "done", tools } } });
  assert.deepEqual(state.setup, tools);
});

test("a snapshot replaces the setup progress with its own", () => {
  let state = reducer(initialState(), { type: "setup", tool: "ffmpeg", phase: "downloading", done: 1, total: 2, message: null });
  state = reducer(state, snapshot());
  assert.deepEqual(state.setup, {});
});

test("setup progress is kept per tool, and seeded from the updater status", () => {
  const tools = {
    "yt-dlp": { phase: "done", done: 10, total: 10, message: null },
    ffmpeg: { phase: null, done: 0, total: null, message: null },
  };
  let state = reducer(initialState(), { type: "status", updater: { setup: { state: "running", tools } } });
  assert.deepEqual(state.setup, tools);
  state = reducer(state, { type: "setup", tool: "ffmpeg", phase: "downloading", done: 5, total: 100, message: null });
  assert.deepEqual(state.setup.ffmpeg, { phase: "downloading", done: 5, total: 100, message: null });
  assert.equal(state.setup["yt-dlp"], tools["yt-dlp"]);
});

test("update progress is kept per tool", () => {
  const state = reducer(initialState(), { type: "update", tool: "yt-dlp", phase: "waiting", done: 0, total: null, message: null });
  assert.deepEqual(state.updates["yt-dlp"], { phase: "waiting", done: 0, total: null, message: null });
});

test("connection state", () => {
  const start = initialState();
  const state = reducer(start, { type: "connection", state: "connected" });
  assert.equal(state.connection, "connected");
  assert.equal(reducer(state, { type: "connection", state: "connected" }), state);
  assert.equal(reducer(start, { type: "connection", state: "open" }), start, "unknown states are ignored");
  assert.equal(reducer(start, { type: "connection", state: 1 }), start);
});

test("unknown or malformed events leave the state as it is", () => {
  const start = reducer(initialState(), snapshot());
  assert.equal(reducer(start, { type: "mystery" }), start);
  assert.equal(reducer(start, null), start);
  assert.equal(reducer(start, { type: "job", job: { title: "no id" } }), start);
  assert.equal(reducer(start, { type: "setup", phase: "done" }), start);
  assert.equal(reducer(start, { type: "setup", tool: 3, phase: "done" }), start);
  assert.equal(reducer(start, { type: "job_removed", id: { a: 1 } }), start);
});

test("the store notifies subscribers of changes only", () => {
  const store = createStore();
  const seen = [];
  const unsubscribe = store.subscribe((state, event) => seen.push([state.ready, event.type]));
  store.dispatch(snapshot());
  store.dispatch({ type: "mystery" });
  store.dispatch({ type: "job_removed", id: "a" });
  unsubscribe();
  store.dispatch({ type: "job_removed", id: "b" });
  assert.deepEqual(seen, [[true, "snapshot"], [true, "job_removed"]]);
  assert.equal(store.getState().jobs["b"], undefined);
});

test("a failing subscriber doesn't stop the others", (t) => {
  t.mock.method(console, "error", () => {});
  const store = createStore();
  let called = false;
  store.subscribe(() => { throw new Error("boom"); });
  store.subscribe(() => { called = true; });
  store.dispatch(snapshot());
  assert.equal(called, true);
});

test("bindHub feeds hub events and connection changes into the store", () => {
  const handlers = {};
  const hub = {
    connection: "connecting",
    onEvent(fn) { handlers.event = fn; return () => { handlers.event = null; }; },
    onConnection(fn) { handlers.connection = fn; return () => { handlers.connection = null; }; },
  };
  const store = createStore();
  const unbind = bindHub(store, hub);
  assert.equal(store.getState().connection, "connecting", "the hub's state when bound is copied at once");
  handlers.connection("connected");
  handlers.event(snapshot());
  assert.equal(store.getState().connection, "connected");
  assert.equal(store.getState().ready, true);
  unbind();
  assert.equal(handlers.event, null);
  assert.equal(handlers.connection, null);
});
