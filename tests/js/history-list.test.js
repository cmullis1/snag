// The History list on a fake DOM: paging, search, row actions and Clear history.
import "./fake-dom.js";
import test from "node:test";
import assert from "node:assert/strict";

import { byClass, byText, find, isHidden } from "./fake-dom.js";
import { createHistoryList } from "../../ui/components/history-list.js";

const settle = () => new Promise((resolve) => setImmediate(resolve));

function entries(count, { from = 0 } = {}) {
  return Array.from({ length: count }, (_, i) => ({
    id: 1000 - from - i, title: `Video ${from + i}`, url: `https://y.test/${from + i}`, kind: "video",
    format_summary: "1080p MP4", size: 1024 * 1024, finished_at: 1, status: "done", error_message: null,
    file_path: `D:\\${from + i}.mp4`,
  }));
}

/** An api whose History holds `all`, answering searches by title. */
function fakeApi(all) {
  const calls = [];
  return {
    calls,
    all,
    async listHistory(query) {
      calls.push(["list", query]);
      const matching = all.filter((entry) => (!query.search || entry.title.includes(query.search))
        && (!query.kind || entry.kind === query.kind));
      return { entries: matching.slice(query.offset, query.offset + query.limit), total: matching.length };
    },
    async removeHistoryEntry(id) {
      calls.push(["remove", id]);
      all.splice(all.findIndex((entry) => entry.id === id), 1);
      return { deleted: 1 };
    },
    async clearHistory() {
      calls.push(["clear"]);
      all.length = 0;
      return { deleted: 0 };
    },
    async openHistoryFolder(id) {
      calls.push(["open", id]);
      return { opened: "x" };
    },
  };
}

function make(all, options = {}) {
  const api = fakeApi(all);
  const timers = [];
  const toasts = [];
  const again = [];
  const history = createHistoryList({
    api, toast: { show: (text, o) => toasts.push([text, o?.kind]) }, onDownloadAgain: (url) => again.push(url),
    setTimer: (fn) => { timers.push(fn); return timers.length; }, clearTimer: () => {}, ...options,
  });
  return { api, history, timers, toasts, again };
}

const rows = (history) => byClass(history.element, "history-row");
const pagingLine = (history) => find(history.element, (node) => node.getAttribute("aria-live") === "polite"
  && node.parentNode?.classList.contains("cluster")).textContent;

test("the first page loads 50; Load more adds the next", async () => {
  const { api, history } = make(entries(120));
  await history.refresh();
  assert.equal(rows(history).length, 50);
  assert.equal(pagingLine(history), "Showing 50 of 120");
  assert.deepEqual(api.calls[0], ["list", { search: "", kind: null, limit: 50, offset: 0 }]);
  byText(history.element, "Load more", "button").click();
  await settle();
  assert.equal(rows(history).length, 100);
  assert.deepEqual(api.calls[1][1].offset, 50);
  byText(history.element, "Load more", "button").click();
  await settle();
  assert.equal(pagingLine(history), "Showing 120 of 120");
  assert.equal(isHidden(byText(history.element, "Load more", "button")), true);
});

test("a row reads title · when over summary · size; a failed one shows its message", async () => {
  const all = entries(1);
  all.push({ ...entries(1, { from: 1 })[0], status: "failed", error_message: "This video isn't available", file_path: null });
  const { history } = make(all);
  await history.refresh();
  const [done, failed] = rows(history);
  assert.ok(done.textContent.startsWith("Video 0"));
  assert.ok(done.textContent.includes("1080p MP4 · 1 MB"));
  assert.ok(failed.textContent.includes("Failed · This video isn't available"));
  assert.ok(find(failed, (node) => node.classList.contains("danger-text")));
  assert.ok(find(done, (node) => node.getAttribute("aria-label") === "Open folder: Video 0"));
  assert.equal(find(failed, (node) => node.getAttribute("aria-label") === "Open folder: Video 1"), null, "no file to show");
});

test("searching waits for typing to pause, then starts over", async () => {
  const { api, history, timers } = make(entries(60));
  await history.refresh();
  const search = byClass(history.element, "history-search")[0];
  search.value = "Video 5";
  search.dispatch("input");
  assert.equal(api.calls.length, 1, "not yet");
  timers.at(-1)();
  await settle();
  assert.deepEqual(api.calls.at(-1), ["list", { search: "Video 5", kind: null, limit: 50, offset: 0 }]);
  assert.deepEqual(rows(history).map((row) => Number(row.dataset.id)), [995, 950, 949, 948, 947, 946, 945, 944, 943, 942, 941]);
});

test("the kind filter asks the server and an empty result says why", async () => {
  const { api, history } = make(entries(3));
  await history.refresh();
  byText(history.element, "Audio", "button").click();
  await settle();
  assert.equal(api.calls.at(-1)[1].kind, "audio");
  assert.equal(byText(history.element, "Audio", "button").getAttribute("aria-checked"), "true");
  assert.equal(rows(history).length, 0);
  assert.equal(byClass(history.element, "history-empty")[0].textContent, "No audio downloads yet.");
});

test("an older answer never replaces a newer one", async () => {
  const all = entries(3);
  const api = fakeApi(all);
  let release;
  const slow = new Promise((resolve) => { release = resolve; });
  const list = api.listHistory.bind(api);
  let first = true;
  api.listHistory = async (query) => {
    if (first) {
      first = false;
      await slow;
    }
    return list(query);
  };
  const history = createHistoryList({ api, setTimer: (fn) => fn(), clearTimer: () => {} });
  const loading = history.refresh();
  byText(history.element, "Video", "button").click();
  await settle();
  all.push({ ...entries(1, { from: 9 })[0] });
  release();
  await loading;
  assert.equal(rows(history).length, 3, "the newer (kind) answer stands");
});

test("row actions: Open folder, Download again, Remove from history", async () => {
  const { api, history, toasts, again } = make(entries(3));
  await history.refresh();
  find(history.element, (node) => node.getAttribute("aria-label") === "Open folder: Video 1").click();
  find(history.element, (node) => node.getAttribute("aria-label") === "Download again: Video 1").click();
  await settle();
  assert.deepEqual(api.calls.filter(([kind]) => kind === "open"), [["open", 999]]);
  assert.deepEqual(again, ["https://y.test/1"]);
  const more = find(history.element, (node) => node.getAttribute("aria-label") === "More: Video 1");
  more.click();
  assert.equal(more.getAttribute("aria-expanded"), "true");
  const menu = find(history.element, (node) => node.id === more.getAttribute("aria-controls"));
  assert.equal(menu.hidden, false);
  byText(menu, "Remove from history", "button").click();
  await settle();
  assert.equal(menu.hidden, true);
  assert.deepEqual(rows(history).map((row) => Number(row.dataset.id)), [1000, 998]);
  assert.equal(pagingLine(history), "Showing 2 of 2");
  assert.deepEqual(toasts.at(-1), ["Removed from History", undefined]);
});

test("a failed action shows the server's message", async () => {
  const { api, history, toasts } = make(entries(1));
  api.openHistoryFolder = async () => { throw new Error("The folder D:\\ isn't there anymore."); };
  await history.refresh();
  find(history.element, (node) => node.getAttribute("aria-label") === "Open folder: Video 0").click();
  await settle();
  assert.deepEqual(toasts, [["The folder D:\\ isn't there anymore.", "error"]]);
});

test("Clear history asks first", async () => {
  const { api, history } = make(entries(2));
  await history.refresh();
  const confirm = byClass(history.element, "history-confirm")[0];
  assert.equal(confirm.hidden, true);
  byClass(history.element, "history-foot")[0].children.at(-1).click();
  assert.equal(confirm.hidden, false);
  byText(confirm, "Cancel", "button").click();
  assert.equal(confirm.hidden, true);
  assert.equal(api.calls.some(([kind]) => kind === "clear"), false);
  byClass(history.element, "history-foot")[0].children.at(-1).click();
  byText(confirm, "Clear history", "button").click();
  await settle();
  await settle();
  assert.equal(api.calls.some(([kind]) => kind === "clear"), true);
  assert.equal(rows(history).length, 0);
  assert.equal(byClass(history.element, "history-empty")[0].textContent, "Nothing downloaded yet. Finished downloads show up here.");
});

test("Clear history's confirm step survives a refresh", async () => {
  const { history } = make(entries(3));
  await history.refresh();
  const clear = [...byClass(history.element, "history-foot")[0].children].at(-1);
  assert.equal(clear.textContent, "Clear history");
  clear.click();
  const confirm = byClass(history.element, "history-confirm")[0];
  assert.equal(confirm.hidden, false);
  await history.refresh(); // a download finished meanwhile
  assert.equal(confirm.hidden, false, "still asking");
  assert.equal(clear.hidden, true, "the first Clear history button stays hidden while asking");
});

test("a load answered after a removal doesn't bring the removed entry back", async () => {
  const all = entries(3);
  const { api, history } = make(all);
  await history.refresh();
  const listHistory = api.listHistory.bind(api);
  let answer = null;
  // The next load is answered later, with History as it was when it was asked.
  api.listHistory = (query) => {
    const before = listHistory(query);
    api.listHistory = listHistory;
    return new Promise((resolve) => { answer = () => resolve(before); });
  };
  const loading = history.refresh();
  const first = rows(history)[0];
  const removed = first.dataset.id;
  find(first, (node) => node.getAttribute("aria-label")?.startsWith("More:")).click();
  byText(first, "Remove from history", "button").click();
  await settle();
  answer(); // the overtaken load answers, with the entry still in it
  await loading;
  await settle();
  assert.ok(rows(history).every((row) => row.dataset.id !== removed), "the removed entry stays gone");
  assert.equal(rows(history).length, 2);
});

test("the … button is a disclosure, not a menu", async () => {
  const { history } = make(entries(1));
  await history.refresh();
  const more = find(history.element, (node) => node.getAttribute("aria-label")?.startsWith("More:"));
  assert.equal(more.getAttribute("aria-haspopup"), null);
  assert.equal(find(history.element, (node) => ["menu", "menuitem"].includes(node.getAttribute("role"))), null);
  more.click();
  assert.equal(more.getAttribute("aria-expanded"), "true");
});
