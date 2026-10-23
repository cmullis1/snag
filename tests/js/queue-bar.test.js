// The queue bar and job rows on a fake DOM: what is announced, focus while rows update and reorder, and
// the actions finished and failed jobs offer.
import "./fake-dom.js";
import test from "node:test";
import assert from "node:assert/strict";

import { byClass, byText, find, findAll } from "./fake-dom.js";
import { createQueueBar } from "../../ui/components/queue-bar.js";
import { createJobRow } from "../../ui/components/job-row.js";
import { h } from "../../ui/components/el.js";
import { placeChildren } from "../../ui/components/reconcile.js";
import { createStore } from "../../ui/store.js";

const QUEUE = { paused: false, held: false, throttled: false, throttled_until: null, current_limit: 3, running: 0, waiting: 0 };

function job(id, state, fields = {}) {
  const { spec = {}, ...rest } = fields;
  return {
    id, state, percent: null, speed: null, eta: null, stage: null, error: null, file_path: null, created_at: 0,
    subtitles_skipped: false, summary: "1080p MP4", can_retry: false, can_clear: false,
    spec: { url: `https://x/${id}`, title: `Video ${id}`, dest_dir: "C:\\Videos" , ...spec }, ...rest,
  };
}

function fakeApi() {
  const calls = [];
  const record = (name, result = {}) => async (...args) => {
    calls.push([name, ...args]);
    return typeof result === "function" ? result(...args) : result;
  };
  return {
    calls,
    cancelJob: record("cancelJob"), retryJob: record("retryJob"), clearJob: record("clearJob"),
    clearFinished: record("clearFinished"), openJobFolder: record("openJobFolder", { opened: "C:\\Videos" }),
    resumeQueue: record("resumeQueue"), retrySetup: record("retrySetup"), pickFolder: record("pickFolder", { path: null }),
    checkForUpdate: record("checkForUpdate", { updater: { update: { current: "2026.08.19", available: "2026.10.01" } } }),
    reinstallTool: record("reinstallTool", { started: false }),
  };
}

function fakeToast() {
  const shown = [];
  return { shown, show: (message, options = {}) => shown.push([message, options.kind ?? "info"]) };
}

function bar(jobs, { expanded = true } = {}) {
  const store = createStore();
  store.dispatch({ type: "snapshot", jobs, queue: QUEUE, status: {} });
  const api = fakeApi();
  const toast = fakeToast();
  const queue = createQueueBar({ api, store, toast, storage: null });
  queue.setExpanded(expanded);
  return { queue, store, api, toast };
}

const settle = () => new Promise((resolve) => setImmediate(resolve));
const summaryOf = (queue) => byClass(queue.element, "queue-summary")[0];
const toggleOf = (queue) => byClass(queue.element, "queue-toggle")[0];
const statusRegion = (queue) => find(queue.element, (node) => node.getAttribute("role") === "status");
const rowOf = (queue, title) => find(queue.element, (node) => node.classList.contains("job-row")
  && byClass(node, "job-title")[0]?.textContent === title);
const button = (root, text) => byText(root, text, "button");

// --- what is announced ---

test("the summary inside the toggle isn't a live region; a status region outside it is", () => {
  const { queue } = bar([job("a", "downloading", { percent: 10 })]);
  const summary = summaryOf(queue);
  const toggle = toggleOf(queue);
  assert.equal(summary.textContent, "1 downloading");
  for (let node = summary; node; node = node.parentNode) assert.equal(node.getAttribute("aria-live"), null);
  const region = statusRegion(queue);
  assert.ok(region, "a status region");
  assert.equal(toggle.contains(region), false, "outside the toggle button");
  assert.equal(region.textContent, "", "nothing is announced on arrival");
});

test("progress updates don't touch the summary or the status region; count changes are announced once", () => {
  const { queue, store } = bar([job("a", "downloading", { percent: 10 }), job("b", "waiting")]);
  const summary = summaryOf(queue);
  const region = statusRegion(queue);
  const before = [summary.mutations, region.mutations];
  for (const percent of [20, 30, 40]) store.dispatch({ type: "job", job: job("a", "downloading", { percent }) });
  assert.deepEqual([summary.mutations, region.mutations], before);
  store.dispatch({ type: "job", job: job("b", "downloading", { percent: 0 }) });
  assert.equal(summary.textContent, "2 downloading");
  assert.equal(region.textContent, "2 downloading");
  const announced = region.mutations;
  store.dispatch({ type: "job", job: job("b", "downloading", { percent: 5 }) });
  assert.equal(region.mutations, announced);
});

// --- focus ---

test("a focused Cancel button keeps focus while jobs are added, finish and progress", () => {
  const { queue, store } = bar([
    job("a", "downloading", { percent: 10, created_at: 1 }), job("b", "downloading", { percent: 10, created_at: 2 }),
  ]);
  const cancelA = button(rowOf(queue, "Video a"), "Cancel");
  cancelA.focus();
  store.dispatch({ type: "job", job: job("c", "downloading", { created_at: 3 }) }); // listed first (newest)
  assert.equal(document.activeElement, cancelA);
  store.dispatch({ type: "job", job: job("b", "done", { created_at: 2 }) }); // b moves to Finished
  assert.equal(document.activeElement, cancelA);
  store.dispatch({ type: "job", job: job("a", "downloading", { percent: 50, created_at: 1 }) });
  assert.equal(document.activeElement, cancelA);
  assert.equal(button(rowOf(queue, "Video a"), "Cancel"), cancelA, "the same button, updated in place");
  store.dispatch({ type: "job_removed", id: "c" });
  assert.equal(document.activeElement, cancelA);
});

test("placeChildren moves only rows out of place, and gives focus back to one it had to move", () => {
  const list = h("ul", null);
  const [a, b, c] = ["a", "b", "c"].map((name) => h("li", null, h("button", { type: "button" }, name)));
  placeChildren(list, [a, b, c]);
  const focusA = a.firstChild;
  focusA.focus();
  const before = list.mutations;
  placeChildren(list, [c, a, b]);
  assert.deepEqual(list.children, [c, a, b]);
  assert.equal(list.mutations - before, 2, "only c moved (out and back in)");
  assert.equal(document.activeElement, focusA);
  const focusC = c.firstChild;
  focusC.focus();
  placeChildren(list, [a, b, c]);
  assert.deepEqual(list.children, [a, b, c]);
  assert.equal(document.activeElement, focusC, "a moved row gets its focus back");
  placeChildren(list, [b]);
  assert.deepEqual(list.children, [b]);
  assert.equal(document.activeElement, null, "a row that left can't keep focus");
});

// --- finished and failed jobs ---

test("a finished job offers Open folder (through the API) and, when the server allows, Clear", async () => {
  const done = job("d", "done", { file_path: "C:\\Videos\\d.mp4", can_clear: true });
  const { queue, api, store } = bar([done]);
  const row = rowOf(queue, "Video d");
  assert.deepEqual(findAll(byClass(row, "job-actions")[0], (node) => node.tagName === "BUTTON").map((node) => node.textContent),
    ["Open folder", "Clear"]);
  button(row, "Open folder").click();
  button(row, "Clear").click();
  await settle();
  assert.deepEqual(api.calls, [["openJobFolder", "d"], ["clearJob", "d"]]);
  store.dispatch({ type: "job", job: { ...done, can_clear: false } });
  assert.equal(button(rowOf(queue, "Video d"), "Clear"), null);
});

test("Details names its job, and the raw error text is set only when it changes", () => {
  const failed = job("f", "failed", { can_retry: true, can_clear: true, error: { kind: "network", message: "Lost connection", action: "retry", details: "ERROR: timed out" } });
  const row = createJobRow({ job: failed, api: fakeApi() });
  const details = button(row.element, "Details");
  assert.equal(details.getAttribute("aria-label"), "Details: Video f");
  assert.equal(button(row.element, "Retry").getAttribute("aria-label"), "Retry: Video f");
  const pre = find(row.element, (node) => node.tagName === "PRE");
  const before = pre.mutations;
  row.update({ ...failed });
  assert.equal(pre.mutations, before);
  row.update({ ...failed, spec: { ...failed.spec, title: "Renamed" } });
  assert.equal(details.getAttribute("aria-label"), "Details: Renamed");
});

test("Check for update says whether one was found; Reinstall says when one is already running", async () => {
  const api = fakeApi();
  const toast = fakeToast();
  const failed = (action) => job(`x-${action}`, "failed", { can_retry: true, error: { kind: "k", message: "m", action, details: "" } });
  const check = createJobRow({ job: failed("check_update"), api, toast });
  button(check.element, "Check for update").click();
  await settle();
  const reinstall = createJobRow({ job: failed("reinstall_ffmpeg"), api, toast });
  button(reinstall.element, "Reinstall ffmpeg").click();
  await settle();
  assert.deepEqual(toast.shown, [
    ["yt-dlp 2026.10.01 is available. Update it in Settings", "info"],
    ["ffmpeg is already being reinstalled", "info"],
  ]);
});

// --- focus when a job's own buttons change or leave ---

const actionButtons = (row) => findAll(byClass(row, "job-actions")[0], (node) => node.tagName === "BUTTON");

test("Retry turning into Cancel keeps focus on the row's new button", async () => {
  const failed = job("r", "failed", { can_retry: true, can_clear: true, error: { kind: "network", message: "Lost", action: "retry", details: "" } });
  const { queue, store } = bar([failed]);
  const retry = button(rowOf(queue, "Video r"), "Retry");
  retry.focus();
  retry.click();
  store.dispatch({ type: "job", job: job("r", "waiting") });
  await settle();
  const cancel = button(rowOf(queue, "Video r"), "Cancel");
  assert.ok(cancel);
  assert.equal(document.activeElement, cancel);
});

test("a button for the same action keeps focus when the others change", () => {
  const failed = { can_retry: true, can_clear: true, error: { kind: "network", message: "Lost", action: "retry", details: "ERROR: x" } };
  const { queue, store } = bar([job("f", "failed", failed)]);
  button(rowOf(queue, "Video f"), "Clear").focus();
  store.dispatch({ type: "job", job: job("f", "failed", { ...failed, can_retry: false }) });
  assert.equal(document.activeElement, button(rowOf(queue, "Video f"), "Clear"), "the new Clear button");
});

test("a job finishing while its Cancel is focused moves focus to its first new button", () => {
  const { queue, store } = bar([job("a", "downloading", { percent: 90, created_at: 1 }), job("b", "waiting", { created_at: 2 })]);
  button(rowOf(queue, "Video a"), "Cancel").focus();
  store.dispatch({ type: "job", job: job("a", "done", { created_at: 1, file_path: "C:\Videos\a.mp4", can_clear: true }) });
  const row = rowOf(queue, "Video a");
  assert.equal(document.activeElement, actionButtons(row)[0]);
  assert.equal(document.activeElement.textContent, "Open folder");
});

test("a row left without buttons gives focus to its title", () => {
  const row = createJobRow({ job: job("t", "downloading", { percent: 5 }), api: fakeApi() });
  button(row.element, "Cancel").focus();
  row.update(job("t", "done", { can_clear: false }));
  const title = byClass(row.element, "job-title")[0];
  assert.equal(title.getAttribute("tabindex"), "-1");
  assert.equal(document.activeElement, title);
});

test("a row's actions changing while focus is elsewhere leave focus alone", () => {
  const outside = h("button", { type: "button" }, "Elsewhere");
  outside.focus();
  const row = createJobRow({ job: job("o", "downloading", { percent: 5 }), api: fakeApi() });
  row.update(job("o", "done", { can_clear: true }));
  assert.equal(document.activeElement, outside);
});

test("a cleared row hands focus to the next row, or the one before", () => {
  const done = (id, created_at) => job(id, "done", { created_at, file_path: `C:\Videos\${id}.mp4`, can_clear: true });
  const { queue, store } = bar([done("x", 3), done("y", 2), done("z", 1)]); // newest first: x, y, z
  button(rowOf(queue, "Video y"), "Clear").focus();
  store.dispatch({ type: "job_removed", id: "y" });
  assert.equal(document.activeElement, actionButtons(rowOf(queue, "Video z"))[0], "the next row");
  button(rowOf(queue, "Video z"), "Clear").focus();
  store.dispatch({ type: "job_removed", id: "z" });
  assert.equal(document.activeElement, actionButtons(rowOf(queue, "Video x"))[0], "the row before");
});

test("Clear finished hiding hands focus to the first row left", async () => {
  const { queue, store, api } = bar([
    job("d", "done", { created_at: 1, file_path: "C:\Videos\d.mp4", can_clear: true }), job("w", "downloading", { percent: 3, created_at: 2 }),
  ]);
  const clear = button(queue.element, "Clear finished");
  clear.focus();
  clear.click();
  await settle();
  assert.deepEqual(api.calls, [["clearFinished"]]);
  store.dispatch({ type: "job_removed", id: "d" });
  assert.equal(clear.hidden, true);
  assert.equal(document.activeElement, button(rowOf(queue, "Video w"), "Cancel"));
});

test("the queue emptying while focus is in it gives focus to the bar itself", () => {
  const { queue, store } = bar([job("e", "done", { file_path: "C:\Videos\e.mp4", can_clear: true })]);
  button(rowOf(queue, "Video e"), "Clear").focus();
  store.dispatch({ type: "job_removed", id: "e" });
  assert.equal(toggleOf(queue).hidden, true);
  assert.equal(queue.element.getAttribute("tabindex"), "-1");
  assert.equal(document.activeElement, queue.element);

  const collapsed = bar([job("c", "downloading", { percent: 1 })], { expanded: false });
  toggleOf(collapsed.queue).focus();
  collapsed.store.dispatch({ type: "job_removed", id: "c" });
  assert.equal(document.activeElement, collapsed.queue.element, "the hidden toggle's focus goes to the bar");
});

test("a batch's Retry failed hiding hands focus to its Show videos button", () => {
  const member = (id, state, index) => job(id, state, { spec: { batch_id: "B", playlist_title: "Mix", playlist_index: index } });
  const { queue, store } = bar([member("p1", "done", 1), member("p2", "failed", 2)]);
  const retryFailed = button(queue.element, "Retry failed");
  assert.equal(retryFailed.hidden, false);
  retryFailed.focus();
  store.dispatch({ type: "job", job: member("p2", "waiting", 2) });
  assert.equal(retryFailed.hidden, true);
  assert.equal(document.activeElement, button(queue.element, "Show videos"));
});
