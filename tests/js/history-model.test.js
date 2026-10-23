import test from "node:test";
import assert from "node:assert/strict";

import {
  KIND_FILTERS, PAGE_SIZE, applyPage, canLoadMore, emptyText, historyRequest, initialHistory, pagingText, refreshRequest,
  removeEntry, rowText, withQuery,
} from "../../ui/components/history-model.js";

const NOW = new Date(2026, 9, 7, 15, 0);
const at = (...parts) => new Date(...parts).getTime() / 1000;
const entry = (id, fields = {}) => ({
  id, title: `Video ${id}`, kind: "video", format_summary: "1080p MP4", size: 218 * 1024 * 1024,
  finished_at: at(2026, 9, 7, 14, 2), status: "done", error_message: null, file_path: `D:\\v${id}.mp4`, ...fields,
});

test("the kind filter: All, Video, Audio", () => {
  assert.deepEqual(KIND_FILTERS.map((filter) => [filter.value, filter.label]), [[null, "All"], ["video", "Video"], ["audio", "Audio"]]);
});

test("the first page asks for 50 from the start; Load more continues after what's shown", () => {
  assert.equal(PAGE_SIZE, 50);
  let state = initialHistory();
  assert.deepEqual(historyRequest(state), { search: "", kind: null, limit: 50, offset: 0 });
  state = applyPage(state, { entries: [entry(1), entry(2)], total: 120 }, { more: false });
  assert.deepEqual(historyRequest(state, { more: true }), { search: "", kind: null, limit: 50, offset: 2 });
  state = withQuery(state, { search: "cats", kind: "audio" });
  assert.deepEqual(historyRequest(state), { search: "cats", kind: "audio", limit: 50, offset: 0 });
});

test("a new search or filter starts over", () => {
  let state = applyPage(initialHistory(), { entries: [entry(1)], total: 1 }, { more: false });
  state = withQuery(state, { search: "  dogs " });
  assert.equal(state.search, "dogs");
  assert.equal(state.loaded, false);
  assert.deepEqual(state.entries, []);
  assert.equal(withQuery(state, { search: "dogs" }), state, "the same query changes nothing");
  assert.equal(withQuery(state, { kind: "video" }).kind, "video");
  assert.equal(withQuery(state, { kind: "video" }).search, "dogs", "the search stays");
});

test("pages are added in order without repeating an entry", () => {
  let state = applyPage(initialHistory(), { entries: [entry(3), entry(2)], total: 4 }, { more: false });
  // A download finished meanwhile: everything moved down one place.
  state = applyPage(state, { entries: [entry(2), entry(1)], total: 5 }, { more: true });
  assert.deepEqual(state.entries.map((item) => item.id), [3, 2, 1]);
  assert.equal(state.total, 5);
  assert.equal(state.loaded, true);
  state = applyPage(state, { entries: [entry(9)], total: 1 }, { more: false });
  assert.deepEqual(state.entries.map((item) => item.id), [9], "a first page replaces the list");
});

test("a malformed page is treated as empty", () => {
  const state = applyPage(initialHistory(), null, { more: false });
  assert.deepEqual(state.entries, []);
  assert.equal(state.total, 0);
});

test("Load more is offered until everything is shown", () => {
  let state = applyPage(initialHistory(), { entries: [entry(1), entry(2)], total: 3 }, { more: false });
  assert.equal(canLoadMore(state), true);
  assert.equal(pagingText(state), "Showing 2 of 3");
  state = applyPage(state, { entries: [entry(3)], total: 3 }, { more: true });
  assert.equal(canLoadMore(state), false);
  assert.equal(pagingText(state), "Showing 3 of 3");
  assert.equal(pagingText(initialHistory()), "");
});

test("removing an entry drops it and the total", () => {
  let state = applyPage(initialHistory(), { entries: [entry(1), entry(2)], total: 7 }, { more: false });
  state = removeEntry(state, 1);
  assert.deepEqual(state.entries.map((item) => item.id), [2]);
  assert.equal(state.total, 6);
  assert.equal(removeEntry(state, 42), state, "an entry that isn't shown changes nothing");
});

test("refreshing reloads as many as are shown, at least a page and at most 500", () => {
  let state = initialHistory();
  assert.deepEqual(refreshRequest(state), { search: "", kind: null, limit: 50, offset: 0 });
  state = applyPage(state, { entries: Array.from({ length: 120 }, (_, i) => entry(i)), total: 300 }, { more: false });
  assert.equal(refreshRequest(state).limit, 120);
  state = applyPage(state, { entries: Array.from({ length: 700 }, (_, i) => entry(i)), total: 900 }, { more: false });
  assert.equal(refreshRequest(state).limit, 500);
});

test("a finished row: title · when over summary · size", () => {
  assert.deepEqual(rowText(entry(1), { now: NOW }), {
    title: "Video 1", when: "Today 14:02", detail: "1080p MP4 · 218 MB", failed: false, canOpen: true,
  });
  assert.deepEqual(rowText(entry(2, { size: null, format_summary: "" }), { now: NOW }).detail, "");
  assert.equal(rowText(entry(3, { title: "" }), { now: NOW }).title, "Untitled");
});

test("a failed row shows its message", () => {
  const row = rowText(entry(1, { status: "failed", error_message: "This video isn't available", file_path: null }), { now: NOW });
  assert.equal(row.failed, true);
  assert.equal(row.detail, "Failed · This video isn't available");
  assert.equal(row.canOpen, false);
  assert.equal(rowText(entry(1, { status: "failed", error_message: null }), { now: NOW }).detail, "Failed");
});

test("the empty list says why", () => {
  assert.equal(emptyText(initialHistory()), "Nothing downloaded yet. Finished downloads show up here.");
  assert.equal(emptyText(withQuery(initialHistory(), { search: "cats" })), "No downloads match “cats”.");
  assert.equal(emptyText(withQuery(initialHistory(), { kind: "audio" })), "No audio downloads yet.");
  assert.equal(emptyText(withQuery(initialHistory(), { kind: "video", search: "x" })), "No video downloads match “x”.");
});
