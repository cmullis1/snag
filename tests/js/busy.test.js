// Controls that work or can't be used now stay focusable (aria-disabled, never `disabled`): the busy()
// helper itself, then every control that used to disable itself, on the fake DOM (which drops focus when
// the focused element is disabled, as browsers do). For each: focus survives the action, a second click
// while it runs does nothing, and the control is active again afterwards.
import "./fake-dom.js";
import test from "node:test";
import assert from "node:assert/strict";

import { byClass, byText, find } from "./fake-dom.js";
import { busy, holdWhileInert, isInert, setInert } from "../../ui/components/busy.js";
import { h } from "../../ui/components/el.js";
import { createBanner } from "../../ui/components/banner.js";
import { createDownloadPanel } from "../../ui/components/download-panel.js";
import { createFolderLine } from "../../ui/components/folder-line.js";
import { createHistoryList } from "../../ui/components/history-list.js";
import { createQueueBar } from "../../ui/components/queue-bar.js";
import { createJobRow } from "../../ui/components/job-row.js";
import { createSettingsForm } from "../../ui/components/settings-form.js";
import { createSetupScreen } from "../../ui/components/setup-screen.js";
import { setupScreen } from "../../ui/components/setup-model.js";
import { createPlaylistPicker } from "../../ui/components/playlist-picker.js";
import { createDownloadTab } from "../../web/download-tab.js";
import { createStore } from "../../ui/store.js";

const settle = async () => {
  for (let i = 0; i < 4; i += 1) await new Promise((resolve) => setImmediate(resolve));
};

/** A promise the test settles by hand. */
function deferred() {
  const gate = {};
  gate.promise = new Promise((resolve, reject) => {
    gate.resolve = resolve;
    gate.reject = reject;
  });
  return gate;
}

/**
 * The common check: focus control, activate it twice while `gate` holds the action, then let the action
 * finish. `count()` is how many times the action ran.
 */
async function assertStaysFocused(control, { gate, count, activate = () => control.click(), afterwards = true }) {
  control.focus();
  activate();
  await settle();
  assert.equal(document.activeElement, control, "focus stays on the control while it works");
  assert.equal(control.disabled, false, "never natively disabled");
  assert.equal(isInert(control), true, "aria-disabled while it works");
  activate();
  await settle();
  assert.equal(count(), 1, "a second activation while it works does nothing");
  gate.resolve({});
  await settle();
  assert.equal(document.activeElement, control, "focus is still on the control afterwards");
  if (afterwards) assert.equal(isInert(control), false, "active again afterwards");
}

// --- the helper ---

test("busy() makes the control aria-disabled while the work runs, then active again", async () => {
  const button = h("button", { type: "button" });
  button.focus();
  const gate = deferred();
  const done = busy(button, () => gate.promise);
  assert.equal(button.getAttribute("aria-disabled"), "true");
  assert.equal(document.activeElement, button);
  gate.resolve("result");
  assert.equal(await done, "result");
  assert.equal(button.getAttribute("aria-disabled"), null);
  assert.equal(document.activeElement, button);
});

test("busy() ignores a second call while busy, and any call on a control that is inert", async () => {
  const button = h("button", { type: "button" });
  let runs = 0;
  const gate = deferred();
  const first = busy(button, () => { runs += 1; return gate.promise; });
  assert.equal(await busy(button, () => { runs += 1; }), undefined);
  assert.equal(runs, 1);
  gate.resolve();
  await first;
  setInert(button, true);
  await busy(button, () => { runs += 1; });
  assert.equal(runs, 1, "an inert control isn't run");
  assert.equal(isInert(button), true, "and is left inert, not cleared");
});

test("busy() restores the control and passes the error on when the work fails", async () => {
  const button = h("button", { type: "button" });
  await assert.rejects(busy(button, async () => { throw new Error("no"); }), /no/);
  assert.equal(isInert(button), false);
  await assert.rejects(busy(button, () => { throw new Error("sync"); }), /sync/);
  assert.equal(isInert(button), false);
});

test("holdWhileInert() cancels the click that would toggle an inert switch", () => {
  const box = h("input", { type: "checkbox" });
  holdWhileInert(box);
  box.click();
  assert.equal(box.checked, true);
  setInert(box, true);
  box.click();
  assert.equal(box.checked, true, "left as it was");
});

// --- the queue banner, Clear finished, and a job's buttons ---

test("the banner's Resume and Retry setup keep focus and ignore a second click", async () => {
  for (const [action, label, option] of [["resume", "Resume", "onResume"], ["retry_setup", "Retry setup", "onRetrySetup"]]) {
    const gate = deferred();
    let calls = 0;
    const banner = createBanner({ [option]: () => { calls += 1; return gate.promise; } });
    banner.update([{ kind: "paused", text: "Held", action }]);
    await assertStaysFocused(byText(banner.element, label, "button"), { gate, count: () => calls });
  }
});

const QUEUE = { paused: false, held: false, throttled: false, throttled_until: null, current_limit: 3, running: 0, waiting: 0 };
const job = (id, state, { spec = {}, ...fields } = {}) => ({
  id, state, percent: null, speed: null, eta: null, stage: null, error: null, file_path: null, created_at: 0,
  subtitles_skipped: false, summary: "1080p MP4", can_retry: false, can_clear: false,
  spec: { url: `https://x/${id}`, title: `Video ${id}`, dest_dir: "C:\\Videos", ...spec }, ...fields,
});

function queueWith(jobs, api) {
  const store = createStore();
  store.dispatch({ type: "snapshot", jobs, queue: QUEUE, status: {} });
  const queue = createQueueBar({ api, store, toast: null, storage: null });
  queue.setExpanded(true);
  return { queue, store };
}

test("Clear finished keeps focus and ignores a second click", async () => {
  const gate = deferred();
  let calls = 0;
  const { queue } = queueWith([job("d", "done", { file_path: "C:\\d.mp4", can_clear: true })],
    { clearFinished: () => { calls += 1; return gate.promise; } });
  await assertStaysFocused(byText(queue.element, "Clear finished", "button"), { gate, count: () => calls });
});

test("a job's Cancel button keeps focus and ignore a second click", async () => {
  const gate = deferred();
  let calls = 0;
  const { queue } = queueWith([job("c", "downloading", { percent: 5 })],
    { cancelJob: () => { calls += 1; return gate.promise; } });
  await assertStaysFocused(byText(queue.element, "Cancel", "button"), { gate, count: () => calls });
});

test("a batch's Retry failed keeps focus and ignores a second click", async () => {
  const gate = deferred();
  let calls = 0;
  const member = (id, state, index) => job(id, state, { spec: { batch_id: "B", playlist_title: "Mix", playlist_index: index } });
  const { queue } = queueWith([member("p1", "done", 1), member("p2", "failed", 2)],
    { retryFailed: () => { calls += 1; return gate.promise; } });
  await assertStaysFocused(byText(queue.element, "Retry failed", "button"), { gate, count: () => calls });
});

// --- the download panel ---

const ROW = {
  format_id: "299", label: "1080p60", ext: "mp4", kind: "video", height: 1080, fps: 60, size: 4000,
  size_is_estimate: false, audio_id: "140", has_audio: false, result_ext: "mp4",
};
const videoInfo = (extra = {}) => ({
  type: "video", site: "youtube", video_id: "abc", url: "https://www.youtube.com/watch?v=abc", title: "A video",
  channel: "Someone", duration: 10, thumbnail: null, formats: { video_simple: [ROW], video_all: [ROW], audio: [], audio_tracks: [] },
  subtitle_langs: [], auto_subtitle_langs: [], has_chapters: false, playlist: null, live_status: null,
  js_runtime_missing: false, duplicate: null, ...extra,
});
const SETTINGS = { video_dir: "C:\\Videos\\Snag", audio_dir: "C:\\Music\\Snag", default_extras: null };

test("the panel's Reinstall Deno keeps focus and ignores a second click", async () => {
  const gate = deferred();
  let calls = 0;
  const api = { reinstallTool: () => { calls += 1; return gate.promise; } };
  const panel = createDownloadPanel({ api, url: "https://y.test/v", info: videoInfo({ js_runtime_missing: true }), settings: SETTINGS });
  await assertStaysFocused(byText(panel.element, "Reinstall Deno", "button"), { gate, count: () => calls });
});

test("the panel's Download keeps focus while it adds the download, and ignores a second click", async () => {
  const gate = deferred();
  let calls = 0;
  const api = { createJob: () => { calls += 1; return gate.promise; } };
  const panel = createDownloadPanel({ api, url: "https://y.test/v", info: videoInfo(), settings: SETTINGS });
  const download = byClass(panel.element, "panel-download")[0];
  await assertStaysFocused(download, { gate, count: () => calls });
  assert.equal(download.textContent, "Download");
});

test("the folder line's Change keeps focus while the picker is open, and ignores a second click", async () => {
  const gate = deferred();
  let calls = 0;
  const line = createFolderLine({ folder: "C:\\Videos", pickFolder: () => { calls += 1; return gate.promise; } });
  await assertStaysFocused(byText(line.element, "Change", "button"), { gate, count: () => calls });
});

// --- History ---

function historyEntries(count) {
  return Array.from({ length: count }, (_, i) => ({
    id: 1000 - i, title: `Video ${i}`, url: `https://y.test/${i}`, kind: "video", format_summary: "1080p MP4", size: 1024,
    finished_at: 1, status: "done", error_message: null, file_path: `D:\\${i}.mp4`,
  }));
}

function historyWith(overrides, count = 3) {
  const all = historyEntries(count);
  const api = {
    listHistory: async (query) => ({ entries: all.slice(query.offset, query.offset + query.limit), total: all.length }),
    removeHistoryEntry: async () => ({ deleted: 1 }),
    clearHistory: async () => ({ deleted: 0 }),
    openHistoryFolder: async () => ({ opened: "x" }),
    ...overrides,
  };
  const history = createHistoryList({ api, toast: null, onDownloadAgain: () => {}, setTimer: () => 0, clearTimer: () => {} });
  return history;
}

test("History's Open folder and Download again buttons keep focus and ignore a second click", async () => {
  const gate = deferred();
  let opens = 0;
  const history = historyWith({ openHistoryFolder: () => { opens += 1; return gate.promise; } });
  await history.refresh();
  const open = find(history.element, (node) => node.getAttribute?.("aria-label")?.startsWith("Open folder:"));
  await assertStaysFocused(open, { gate, count: () => opens });

  const again = deferred();
  let agains = 0;
  const other = createHistoryList({
    api: { listHistory: async () => ({ entries: historyEntries(1), total: 1 }) }, toast: null,
    onDownloadAgain: () => { agains += 1; return again.promise; }, setTimer: () => 0, clearTimer: () => {},
  });
  await other.refresh();
  const button = find(other.element, (node) => node.getAttribute?.("aria-label")?.startsWith("Download again:"));
  await assertStaysFocused(button, { gate: again, count: () => agains });
});

test("History's Clear confirm keeps focus until the list answers, and ignores a second click", async () => {
  const gate = deferred();
  let calls = 0;
  const history = historyWith({ clearHistory: () => { calls += 1; return gate.promise; } });
  await history.refresh();
  const confirm = byClass(history.element, "history-confirm")[0];
  byClass(history.element, "history-foot")[0].children.at(-1).click();
  const yes = byText(confirm, "Clear history", "button");
  yes.focus();
  yes.click();
  await settle();
  assert.equal(document.activeElement, yes);
  assert.equal(yes.disabled, false);
  assert.equal(isInert(yes), true);
  yes.click();
  await settle();
  assert.equal(calls, 1);
  gate.resolve({});
  await settle();
  assert.equal(isInert(yes), false);
});

test("History's Load more keeps focus while it loads, and ignores a second click", async () => {
  const gate = deferred();
  let pages = 0;
  const all = historyEntries(80);
  const history = createHistoryList({
    api: {
      listHistory: async (query) => {
        pages += 1;
        if (query.offset > 0) await gate.promise;
        return { entries: all.slice(query.offset, query.offset + query.limit), total: all.length };
      },
    },
    toast: null, onDownloadAgain: () => {}, setTimer: () => 0, clearTimer: () => {},
  });
  await history.refresh();
  assert.equal(pages, 1);
  const more = byText(history.element, "Load more", "button");
  more.focus();
  more.click();
  await settle();
  assert.equal(document.activeElement, more);
  assert.equal(more.disabled, false);
  assert.equal(isInert(more), true);
  more.click();
  await settle();
  assert.equal(pages, 2, "no second page request while one is loading");
  gate.resolve();
  await settle();
  assert.equal(document.activeElement, more);
  assert.equal(isInert(more), false);
});

// --- Settings ---

const EXTRAS = {
  subtitles: true, subtitle_lang: "en", subtitle_mode: "embed", thumbnail_tags: true, chapters: true, sponsorblock: false,
  sponsorblock_mode: "remove", sponsorblock_categories: ["sponsor"], trim_start: null, trim_end: null,
};
const SAVED = {
  video_dir: "C:\\Videos\\Snag", audio_dir: "C:\\Music\\Snag", quick_max_height: null, quick_extras: EXTRAS,
  default_extras: EXTRAS, max_concurrent: 5, check_updates_daily: true, start_with_windows: true, remembered_dirs: [],
};

async function settingsWith(api) {
  const store = createStore();
  const timers = new Map();
  let next = 0;
  const form = createSettingsForm({
    api: { getSettings: async () => structuredClone(SAVED), updateSettings: async () => structuredClone(SAVED),
      pickFolder: async () => ({ path: null }), ...api },
    store, onSettings: () => {}, now: () => new Date(Date.UTC(2026, 9, 7, 14, 30)),
    setTimer: (fn) => { next += 1; timers.set(next, fn); return next; }, clearTimer: (id) => timers.delete(id),
    setRepeat: () => 0, clearRepeat: () => {},
  });
  await form.load();
  return { form, store, timers };
}

test("a Settings switch keeps focus while it saves, and a second click doesn't toggle it", async () => {
  const gate = deferred();
  let saves = 0;
  const { form } = await settingsWith({
    updateSettings: async (changes) => { saves += 1; return saves === 1 ? gate.promise : { ...SAVED, ...changes }; },
  });
  const [startup] = byClass(form.element, "switch");
  startup.focus();
  startup.click();
  await settle();
  assert.equal(startup.checked, false);
  assert.equal(document.activeElement, startup);
  assert.equal(startup.disabled, false);
  assert.equal(isInert(startup), true);
  startup.click();
  await settle();
  assert.equal(startup.checked, false, "the second click didn't toggle it back");
  assert.equal(saves, 1);
  gate.resolve({ ...SAVED, start_with_windows: false });
  await settle();
  assert.equal(document.activeElement, startup);
  assert.equal(isInert(startup), false);
  startup.click();
  await settle();
  assert.equal(startup.checked, true, "and it works again afterwards");
});

test("Settings' tool buttons (Check now, Update, Reinstall) keep focus and ignore a second click", async () => {
  const gates = { check: deferred(), update: deferred(), ffmpeg: deferred(), deno: deferred() };
  const calls = { check: 0, update: 0, reinstall: [] };
  const { form, store } = await settingsWith({
    checkForUpdate: () => { calls.check += 1; return gates.check.promise; },
    installUpdate: () => { calls.update += 1; return gates.update.promise; },
    reinstallTool: (name) => { calls.reinstall.push(name); return gates[name].promise; },
  });
  store.dispatch({
    type: "snapshot", jobs: [], queue: {},
    status: {
      tools: { ytdlp: { present: true, version: "2026.08.19" }, ffmpeg: { present: true, version: "7.1" },
        ffprobe: { present: true, version: "7.1" }, deno: { present: true, version: "2.0" } },
      updater: { setup: { state: "done", tools: {} }, update: { available: "2026.09.30", state: "idle" } },
    },
  });
  await assertStaysFocused(byText(form.element, "Check now", "button"), { gate: gates.check, count: () => calls.check });
  await assertStaysFocused(byText(form.element, "Update to 2026.09.30", "button"), { gate: gates.update, count: () => calls.update });
  for (const name of ["ffmpeg", "deno"]) {
    const reinstall = find(form.element, (node) => node.getAttribute?.("aria-label") === `Reinstall ${name === "deno" ? "Deno" : name}`);
    await assertStaysFocused(reinstall, { gate: gates[name], count: () => calls.reinstall.filter((item) => item === name).length });
  }
});

test("the Settings folder picker's Change keeps focus while the picker is open", async () => {
  const gate = deferred();
  let picks = 0;
  const { form } = await settingsWith({ pickFolder: () => { picks += 1; return gate.promise; } });
  const change = byText(byClass(form.element, "folder-line")[0], "Change", "button");
  await assertStaysFocused(change, { gate, count: () => picks });
});

test("the stepper keeps focus on the button that just reached its limit, and a press there does nothing", async () => {
  const { form, timers } = await settingsWith({});
  const more = find(form.element, (node) => node.getAttribute?.("aria-label") === "More at once");
  const count = byClass(form.element, "stepper-value")[0];
  more.focus();
  for (let i = 0; i < 5; i += 1) more.click();
  assert.equal(count.textContent, "10");
  assert.equal(document.activeElement, more, "focus stays after the last step up");
  assert.equal(more.disabled, false);
  assert.equal(isInert(more), true);
  more.click();
  assert.equal(count.textContent, "10");
  assert.equal(timers.size, 1, "one save pending");
  const fewer = find(form.element, (node) => node.getAttribute?.("aria-label") === "Fewer at once");
  assert.equal(isInert(fewer), false);
});

// --- the setup screen, the playlist picker and the lookup box ---

test("the setup screen's Retry keeps focus while setup restarts, and ignores a second click", async () => {
  const gate = deferred();
  let calls = 0;
  const screen = createSetupScreen({ api: { retrySetup: () => { calls += 1; return gate.promise; } } });
  screen.update(setupScreen({ updater: { setup: { state: "error", message: "Couldn't download ffmpeg", can_retry: true, tools: {} } } }, {}));
  const retry = byText(screen.element, "Retry", "button");
  await assertStaysFocused(retry, { gate, count: () => calls });
  assert.equal(retry.textContent, "Retry");
});

const PLAYLIST = {
  type: "playlist", url: "https://www.youtube.com/playlist?list=PL1", title: "Fake: Mix",
  entries: [1, 2, 3].map((index) => ({ index, video_id: `v${index}`, title: `Video ${index}`, duration: 60, thumbnail: null, duplicate: null })),
};

test("the playlist picker's Download keeps focus while it adds the batch, and ignores a second click", async () => {
  const gate = deferred();
  let calls = 0;
  const picker = createPlaylistPicker({
    api: { createBatch: () => { calls += 1; return gate.promise; } }, url: "https://y.test/list", info: PLAYLIST, settings: SETTINGS, toast: null,
    onQueued: () => {}, pickFolder: async () => null,
  });
  await assertStaysFocused(byClass(picker.element, "picker-download")[0], { gate, count: () => calls });
});

test("the picker's All and None keep focus when pressing them makes them not apply", () => {
  const picker = createPlaylistPicker({ api: {}, url: "https://y.test/list", info: PLAYLIST, settings: SETTINGS, toast: null, onQueued: () => {}, pickFolder: async () => null });
  const all = byText(picker.element, "All", "button");
  const none = byText(picker.element, "None", "button");
  all.focus();
  all.click();
  assert.equal(document.activeElement, all);
  assert.equal(all.disabled, false);
  assert.equal(isInert(all), true, "everything is ticked: All no longer applies");
  all.click();
  assert.equal(picker.getRequest().entries.length, 3);
  none.focus();
  none.click();
  assert.equal(document.activeElement, none);
  assert.equal(isInert(none), true);
  assert.equal(isInert(all), false);
  none.click();
  assert.equal(picker.getRequest(), null);
});

test("the Look up button keeps focus while a lookup runs, and Enter during one doesn't start another", async () => {
  const gate = deferred();
  let lookups = 0;
  const tab = createDownloadTab({
    api: { lookup: () => { lookups += 1; return gate.promise; } }, store: null, toast: null, getSettings: () => null,
    navigate: () => {},
  });
  const input = byClass(tab.element, "paste-input")[0];
  const button = byClass(tab.element, "paste-button")[0];
  input.value = "https://y.test/a";
  button.focus();
  const form = tab.element.children[0];
  form.dispatch("submit"); // a click on the button, or Enter in the box
  await settle();
  assert.equal(lookups, 1);
  assert.equal(document.activeElement, button);
  assert.equal(button.disabled, false);
  assert.equal(isInert(button), true);
  input.value = "https://y.test/b";
  form.dispatch("submit"); // Enter in the box, or a second click on the button
  await settle();
  assert.equal(lookups, 1, "no second lookup while one runs");
  gate.reject(new Error("Snag couldn't reach YouTube"));
  await settle();
  assert.equal(isInert(button), false);
  assert.equal(document.activeElement, button);
});

test("a Reinstall that has no toast to tell still asks the server (the call isn't inside the optional toast)", async () => {
  const calls = [];
  const api = { reinstallTool: async (name) => { calls.push(name); return { started: true }; } };
  const failed = job("r", "failed", { can_retry: true, error: { kind: "k", message: "m", action: "reinstall_ffmpeg", details: "" } });
  const row = createJobRow({ job: failed, api });
  byText(row.element, "Reinstall ffmpeg", "button").click();
  await settle();
  assert.deepEqual(calls, ["ffmpeg"]);
  const panel = createDownloadPanel({ api, url: "https://y.test/v", info: videoInfo({ js_runtime_missing: true }), settings: SETTINGS });
  byText(panel.element, "Reinstall Deno", "button").click();
  await settle();
  assert.deepEqual(calls, ["ffmpeg", "deno"]);
});
