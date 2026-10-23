import test from "node:test";
import assert from "node:assert/strict";

import {
  JOB_GROUP, ACTION, groupOf, queueCounts, combinedProgress, queueSummaryText, groupJobs, batchCounts,
  batchSummaryText, batchTitle, jobActions, jobStatusText, jobProgress, queueBanners, loadExpanded, saveExpanded,
  updateCheckMessage, reinstallMessage,
} from "../../ui/components/queue-model.js";

const MB = 1024 ** 2;
let clock = 1000;

function job(state, fields = {}) {
  clock += 1;
  const { spec = {}, ...rest } = fields;
  return {
    id: `job${clock}`, state, percent: null, speed: null, eta: null, stage: null, error: null, file_path: null,
    subtitles_skipped: false, created_at: clock, finished_at: null, kind: "video", summary: "1080p MP4",
    can_retry: state === "failed" || state === "cancelled", can_clear: state === "failed" || state === "cancelled",
    spec: { url: "https://x", title: `Video ${clock}`, dest_dir: "C:\\Videos\\Snag", estimated_size: null,
      batch_id: null, playlist_index: null, playlist_count: null, ...spec },
    ...rest,
  };
}

const error = (fields = {}) => ({ kind: "network", message: "Lost connection", action: "retry", can_retry: true,
  keep_partial: true, details: "ERROR: timed out", ...fields });

// --- groups and counts ---

test("states fall into running, waiting, failed and finished groups", () => {
  assert.equal(groupOf("fetching"), JOB_GROUP.RUNNING);
  assert.equal(groupOf("downloading"), JOB_GROUP.RUNNING);
  assert.equal(groupOf("processing"), JOB_GROUP.RUNNING);
  assert.equal(groupOf("waiting"), JOB_GROUP.WAITING);
  assert.equal(groupOf("interrupted"), JOB_GROUP.WAITING);
  assert.equal(groupOf("failed"), JOB_GROUP.FAILED);
  assert.equal(groupOf("done"), JOB_GROUP.FINISHED);
  assert.equal(groupOf("cancelled"), JOB_GROUP.FINISHED);
});

test("counts by state", () => {
  const jobs = [job("downloading"), job("processing"), job("waiting"), job("interrupted"), job("failed"),
    job("done"), job("done"), job("cancelled")];
  assert.deepEqual(queueCounts(jobs), { downloading: 2, waiting: 2, failed: 1, done: 2, cancelled: 1, total: 8 });
});

test("the summary line names what is happening, else what happened", () => {
  assert.equal(queueSummaryText(queueCounts([job("downloading"), job("downloading"), job("waiting")])), "2 downloading · 1 waiting");
  assert.equal(queueSummaryText(queueCounts([job("waiting")])), "1 waiting");
  assert.equal(queueSummaryText(queueCounts([job("downloading"), job("failed")])), "1 downloading · 1 failed");
  assert.equal(queueSummaryText(queueCounts([job("done"), job("done"), job("failed")])), "2 done · 1 failed");
  assert.equal(queueSummaryText(queueCounts([job("cancelled")])), "1 cancelled");
  assert.equal(queueSummaryText(queueCounts([])), "No downloads");
});

test("combined progress covers unfinished jobs, weighted by size when every size is known", () => {
  assert.equal(combinedProgress([]), null);
  assert.equal(combinedProgress([job("done"), job("failed")]), null);
  // Equal weights: 50, 0 (waiting), 100 (processing) → 50.
  assert.equal(combinedProgress([job("downloading", { percent: 50 }), job("waiting"), job("processing", { percent: null })]), 50);
  // By size: 100 MB at 10% and 300 MB at 50% → 40%.
  const sized = [
    job("downloading", { percent: 10, spec: { estimated_size: 100 * MB } }),
    job("downloading", { percent: 50, spec: { estimated_size: 300 * MB } }),
  ];
  assert.equal(combinedProgress(sized), 40);
  // One size unknown: equal weights.
  assert.equal(combinedProgress([sized[0], job("downloading", { percent: 50 })]), 30);
});

// --- grouping with batches ---

test("jobs are grouped in state order, newest first within a group", () => {
  const old = job("waiting");
  const failed = job("failed");
  const running = job("downloading");
  const fresh = job("waiting");
  const done = job("done");
  const groups = groupJobs([old, failed, running, fresh, done]);
  assert.deepEqual(groups.map((group) => group.group), ["running", "waiting", "failed", "finished"]);
  assert.deepEqual(groups.map((group) => group.label), ["Downloading", "Waiting", "Failed", "Finished"]);
  assert.deepEqual(groups[1].items.map((item) => item.job.id), [fresh.id, old.id]);
  assert.ok(groups.every((group) => group.items.every((item) => item.type === "job")));
});

test("a batch is one item, in the group of its most active job, its jobs in playlist order", () => {
  const spec = (index) => ({ batch_id: "b1", playlist_index: index, playlist_count: 3, dest_dir: "C:\\Videos\\Snag\\My list" });
  const second = job("done", { spec: spec(2) });
  const first = job("downloading", { spec: spec(1) });
  const third = job("failed", { spec: spec(3) });
  const single = job("done");
  const groups = groupJobs([second, first, third, single]);
  assert.equal(groups[0].group, JOB_GROUP.RUNNING);
  const [item] = groups[0].items;
  assert.equal(item.type, "batch");
  assert.equal(item.batch.id, "b1");
  assert.equal(item.batch.title, "My list");
  assert.deepEqual(item.batch.jobs.map((entry) => entry.id), [first.id, second.id, third.id]);
  assert.deepEqual(item.batch.counts, { total: 3, done: 1, failed: 1, cancelled: 0, running: 1, waiting: 0 });
  assert.equal(groups[1].group, JOB_GROUP.FINISHED);
  assert.equal(groups[1].items[0].job.id, single.id);
});

test("batch summaries", () => {
  assert.equal(batchSummaryText({ total: 42, done: 40, failed: 2, cancelled: 0, running: 0, waiting: 0 }), "40 of 42 done, 2 failed");
  assert.equal(batchSummaryText({ total: 42, done: 42, failed: 0, cancelled: 0, running: 0, waiting: 0 }), "42 of 42 done");
  assert.equal(batchSummaryText({ total: 10, done: 3, failed: 1, cancelled: 0, running: 2, waiting: 4 }), "3 of 10 done, 1 failed · 2 downloading · 4 waiting");
  assert.equal(batchSummaryText({ total: 5, done: 4, failed: 0, cancelled: 1, running: 0, waiting: 0 }), "4 of 5 done, 1 cancelled");
  assert.deepEqual(batchCounts([job("done"), job("interrupted"), job("cancelled")]),
    { total: 3, done: 1, failed: 0, cancelled: 1, running: 0, waiting: 1 });
});

test("a batch is named after its folder, or its first video", () => {
  assert.equal(batchTitle([job("done", { spec: { dest_dir: "D:\\Music\\Snag\\Road trip\\" } })]), "Road trip");
  assert.equal(batchTitle([job("done", { spec: { dest_dir: "/home/me/Mix" } })]), "Mix");
  assert.equal(batchTitle([job("done", { spec: { dest_dir: "", title: "First" } }), job("done")]), "First and 1 more");
});

// --- actions ---

test("running and waiting jobs can be cancelled", () => {
  for (const state of ["fetching", "downloading", "processing", "waiting", "interrupted"]) {
    assert.deepEqual(jobActions(job(state)), [ACTION.CANCEL], state);
  }
});

test("failed jobs: Retry, the error's own action, Clear and Details", () => {
  assert.deepEqual(jobActions(job("failed", { error: error() })), [ACTION.RETRY, ACTION.CLEAR, ACTION.DETAILS]);
  assert.deepEqual(
    jobActions(job("failed", { can_retry: true, error: error({ action: "change_folder", details: "" }) })),
    [ACTION.RETRY, ACTION.CHANGE_FOLDER, ACTION.CLEAR],
  );
  assert.deepEqual(
    jobActions(job("failed", { can_retry: false, error: error({ action: null, can_retry: false }) })),
    [ACTION.CLEAR, ACTION.DETAILS],
  );
  assert.deepEqual(jobActions(job("failed", { error: error({ action: "check_update" }) })),
    [ACTION.RETRY, ACTION.CHECK_UPDATE, ACTION.CLEAR, ACTION.DETAILS]);
  assert.deepEqual(jobActions(job("failed", { error: error({ action: "reinstall_ffmpeg" }) })),
    [ACTION.RETRY, ACTION.REINSTALL_FFMPEG, ACTION.CLEAR, ACTION.DETAILS]);
});

test("done jobs: Open folder (when it can be opened) and Clear only when the server allows it", () => {
  const done = job("done", { file_path: "C:\\Videos\\Snag\\a.mp4" });
  assert.deepEqual(jobActions(done, { canOpenFolder: true }), [ACTION.OPEN_FOLDER]);
  assert.deepEqual(jobActions(done), []);
  assert.deepEqual(jobActions({ ...done, can_clear: true }, { canOpenFolder: true }), [ACTION.OPEN_FOLDER, ACTION.CLEAR]);
  assert.deepEqual(jobActions({ ...done, file_path: null }, { canOpenFolder: true }), []);
});

test("cancelled jobs: Retry and Clear", () => {
  assert.deepEqual(jobActions(job("cancelled")), [ACTION.RETRY, ACTION.CLEAR]);
});

// --- status line and progress ---

test("the status line for each state", () => {
  assert.equal(jobStatusText(job("waiting")), "Waiting");
  assert.equal(jobStatusText(job("interrupted")), "Interrupted, will resume");
  assert.equal(jobStatusText(job("fetching")), "Starting…");
  assert.equal(jobStatusText(job("downloading", { percent: 42.7, speed: 3.4 * MB, eta: 120 })), "42% · 3.4 MB/s · 2 min left");
  assert.equal(jobStatusText(job("downloading", { percent: 5 })), "5%");
  assert.equal(jobStatusText(job("downloading", { stage: "Downloading audio…", percent: 80 })), "80% · Downloading audio…");
  assert.equal(jobStatusText(job("processing", { stage: "Joining video and audio…" })), "Joining video and audio…");
  assert.equal(jobStatusText(job("processing")), "Finishing…");
  assert.equal(jobStatusText(job("done")), "Done · 1080p MP4");
  // The server's summary for a job that skipped subtitles (snag/store/history.py format_summary): said once.
  assert.equal(jobStatusText(job("done", { subtitles_skipped: true, summary: "1080p MP4 + saved without subtitles" })),
    "Done · 1080p MP4 + saved without subtitles");
  assert.equal(jobStatusText(job("failed", { error: error() })), "Lost connection");
  assert.equal(jobStatusText(job("failed")), "Something went wrong");
  assert.equal(jobStatusText(job("cancelled")), "Cancelled");
});

test("progress bars per state", () => {
  assert.deepEqual(jobProgress(job("downloading", { percent: 42 })), { value: 42, tone: null, indeterminate: false });
  assert.deepEqual(jobProgress(job("fetching")), { value: null, tone: null, indeterminate: true });
  assert.deepEqual(jobProgress(job("processing")), { value: null, tone: null, indeterminate: true });
  assert.deepEqual(jobProgress(job("waiting")), { value: 0, tone: "paused", indeterminate: false });
  assert.deepEqual(jobProgress(job("interrupted", { percent: 30 })), { value: 30, tone: "paused", indeterminate: false });
  assert.equal(jobProgress(job("done")), null);
  assert.equal(jobProgress(job("failed")), null);
});

// --- banners ---

const queue = (fields = {}) => ({ paused: false, held: false, throttled: false, throttled_until: null,
  current_limit: 5, running: 0, waiting: 0, ...fields });
const status = (setupState = "done", updateState = "idle") => ({
  updater: { setup: { state: setupState, message: null }, update: { state: updateState } },
});

test("no banner for a normal queue", () => {
  assert.deepEqual(queueBanners(queue(), status()), []);
  assert.deepEqual(queueBanners(null, {}), []);
});

test("paused, with Resume", () => {
  assert.deepEqual(queueBanners(queue({ paused: true }), status()),
    [{ kind: "paused", text: "Downloads are paused", action: "resume" }]);
});

test("held for an update, or while setting up", () => {
  assert.deepEqual(queueBanners(queue({ held: true }), status("done", "installing")),
    [{ kind: "held", text: "Updating yt-dlp… downloads will continue when it's done", action: null }]);
  assert.deepEqual(queueBanners(queue({ held: true }), status("running")),
    [{ kind: "setup", text: "Setting up Snag… downloads will start when it's ready", action: null }]);
  assert.deepEqual(queueBanners(queue({ held: true }), { updater: { setup: { state: "error", message: "Snag couldn't finish setting up." } } }),
    [{ kind: "setup", text: "Snag couldn't finish setting up.", action: null }]);
  assert.deepEqual(queueBanners(queue({ held: true }), status()),
    [{ kind: "held", text: "Downloads are on hold for a moment", action: null }]);
});

test("a setup that failed and can be retried offers Retry", () => {
  const failed = { updater: { setup: { state: "error", message: "Snag couldn't finish setting up. Offline.", can_retry: true } } };
  assert.deepEqual(queueBanners(queue({ held: true }), failed),
    [{ kind: "setup", text: "Snag couldn't finish setting up. Offline.", action: "retry_setup" }]);
});

test("throttled, with the time downloads resume", () => {
  const until = new Date(2026, 9, 7, 14, 32).getTime() / 1000;
  const now = new Date(2026, 9, 7, 14, 28);
  assert.deepEqual(queueBanners(queue({ throttled: true, throttled_until: until }), status(), { now }),
    [{ kind: "throttled", text: "YouTube is slowing you down. Resuming at 14:32", action: null }]);
  assert.deepEqual(queueBanners(queue({ throttled: true, throttled_until: until }), status(), { now: new Date(2026, 9, 7, 14, 40) }),
    [{ kind: "throttled", text: "YouTube is slowing you down. Downloading one at a time for now", action: null }]);
});

test("several banners together, paused first", () => {
  const kinds = queueBanners(queue({ paused: true, held: true }), status("done", "installing")).map((banner) => banner.kind);
  assert.deepEqual(kinds, ["paused", "held"]);
});

// --- remembered expansion ---

test("the expanded state is remembered, and storage failures are harmless", () => {
  const data = new Map();
  const storage = { getItem: (key) => data.get(key) ?? null, setItem: (key, value) => data.set(key, value) };
  assert.equal(loadExpanded(storage), false);
  saveExpanded(storage, true);
  assert.equal(loadExpanded(storage), true);
  saveExpanded(storage, false);
  assert.equal(loadExpanded(storage), false);
  const broken = { getItem: () => { throw new Error("denied"); }, setItem: () => { throw new Error("denied"); } };
  assert.equal(loadExpanded(broken), false);
  assert.doesNotThrow(() => saveExpanded(broken, true));
  assert.equal(loadExpanded(null), false);
});

// --- what a job row's tool actions report ---

test("Check for update says whether an update was found", () => {
  const status = (update) => ({ updater: { update } });
  assert.deepEqual(updateCheckMessage(status({ current: "2026.08.19", available: "2026.10.01", error: null })),
    { text: "yt-dlp 2026.10.01 is available. Update it in Settings", kind: "info" });
  assert.deepEqual(updateCheckMessage(status({ current: "2026.10.01", available: null, error: null })),
    { text: "yt-dlp is up to date (2026.10.01)", kind: "success" });
  assert.deepEqual(updateCheckMessage(status({ current: null, available: null })), { text: "yt-dlp is up to date", kind: "success" });
  assert.deepEqual(updateCheckMessage(status({ current: "2026.08.19", available: null, error: "GitHub didn't answer" })),
    { text: "Couldn't check for an update: GitHub didn't answer", kind: "error" });
  assert.deepEqual(updateCheckMessage(null), { text: "Checked for a yt-dlp update. See Settings", kind: "info" });
});

test("Reinstall says when a reinstall was already under way", () => {
  assert.equal(reinstallMessage({ started: true }, "ffmpeg"), "Reinstalling ffmpeg…");
  assert.equal(reinstallMessage({ started: false }, "ffmpeg"), "ffmpeg is already being reinstalled");
  assert.equal(reinstallMessage({ started: false }, "Deno"), "Deno is already being reinstalled");
  assert.equal(reinstallMessage(null, "Deno"), "Reinstalling Deno…");
});
