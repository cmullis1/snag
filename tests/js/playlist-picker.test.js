// The playlist picker on a fake DOM: ticking videos, choosing a quality, and the batch it sends.
import "./fake-dom.js";
import test from "node:test";
import assert from "node:assert/strict";

import { byClass, byText, findAll } from "./fake-dom.js";
import { createPlaylistPicker } from "../../ui/components/playlist-picker.js";

const SETTINGS = {
  video_dir: "C:\\Users\\me\\Videos\\Snag", audio_dir: "C:\\Users\\me\\Music\\Snag",
  default_extras: {
    subtitles: true, subtitle_lang: "en", subtitle_mode: "embed", thumbnail_tags: true, chapters: true,
    sponsorblock: false, sponsorblock_mode: "remove", sponsorblock_categories: ["sponsor"], trim_start: null, trim_end: null,
  },
};
const LOOKED_UP = "https://m.youtube.com/playlist?list=PL1";
const INFO = {
  type: "playlist", site: "youtube", url: "https://www.youtube.com/playlist?list=PL1", title: "Fake: Mix",
  entries: [
    { index: 1, video_id: "a", title: "One", duration: 61, thumbnail: "https://i.ytimg.com/a.jpg", duplicate: null },
    { index: 2, video_id: "b", title: "Two", duration: 62, thumbnail: "javascript:alert(1)",
      duplicate: { finished_at: 1, format_summary: "1080p MP4" } },
    { index: 3, video_id: "c", title: "Three", duration: 63, thumbnail: null, duplicate: null },
  ],
};

function makePicker({
  createBatch = async (body) => ({ batch_id: "b1", jobs: body.entries.map((i) => ({ id: `j${i}` })) }), jobs = [],
} = {}) {
  const sent = [];
  const toasts = [];
  const queued = [];
  const api = { createBatch: async (body) => { sent.push(body); return createBatch(body); } };
  const picker = createPlaylistPicker({
    api, url: LOOKED_UP, info: INFO, settings: SETTINGS, toast: { show: (text, options) => toasts.push([text, options?.kind]) },
    onQueued: (result) => queued.push(result), pickFolder: async () => "D:\\Clips", jobs,
  });
  return { picker, sent, toasts, queued };
}

const checkboxes = (picker) => byClass(picker.element, "picker-check");
const button = (picker) => byClass(picker.element, "picker-download")[0];
const settle = () => new Promise((resolve) => setImmediate(resolve));

test("videos not downloaded before start ticked; the line and button count them", () => {
  const { picker } = makePicker();
  assert.deepEqual(checkboxes(picker).map((box) => box.checked), [true, false, true]);
  assert.equal(byClass(picker.element, "picker-counts")[0].textContent, "3 videos · 2 selected");
  assert.equal(button(picker).textContent, "Download 2 videos");
  assert.ok(picker.element.textContent.includes("02 · Two"));
  assert.ok(picker.element.textContent.includes("have it"));
});

test("videos already in the queue are marked in queue and start unticked", () => {
  const { picker } = makePicker({
    jobs: [{ id: "j1", state: "downloading", spec: { site: "youtube", video_id: "c" } },
      { id: "j2", state: "failed", spec: { site: "youtube", video_id: "a" } }],
  });
  assert.deepEqual(checkboxes(picker).map((box) => box.checked), [true, false, false]);
  assert.equal(button(picker).textContent, "Download 1 video");
  const rows = byClass(picker.element, "picker-row");
  assert.ok(rows[2].textContent.includes("in queue"));
  assert.ok(!rows[0].textContent.includes("in queue"), "a failed job doesn't count");
  // Still choosable: ticking it adds it again.
  checkboxes(picker)[2].checked = true;
  checkboxes(picker)[2].dispatch("change");
  assert.deepEqual(picker.getRequest().entries, [1, 3]);
});

test("only https: and data:image/ thumbnails (the page's img-src) are shown", () => {
  const { picker } = makePicker();
  const images = findAll(picker.element, (node) => node.tagName === "IMG");
  assert.deepEqual(images.map((img) => img.getAttribute("src")), ["https://i.ytimg.com/a.jpg"]);
  const more = createPlaylistPicker({
    api: {}, url: LOOKED_UP, settings: SETTINGS,
    info: { ...INFO, entries: [
      { index: 1, video_id: "d", title: "Four", duration: 64, thumbnail: "http://i.ytimg.com/d.jpg", duplicate: null },
      { index: 2, video_id: "e", title: "Five", duration: 65, thumbnail: "data:image/png;base64,iVBORw0KGgo=", duplicate: null },
    ] },
  });
  assert.deepEqual(findAll(more.element, (node) => node.tagName === "IMG").map((img) => img.getAttribute("src")),
    ["data:image/png;base64,iVBORw0KGgo="], "http: is left out");
});

test("All and None tick everything or nothing", () => {
  const { picker } = makePicker();
  byText(picker.element, "All", "button").click();
  assert.deepEqual(checkboxes(picker).map((box) => box.checked), [true, true, true]);
  assert.equal(button(picker).textContent, "Download 3 videos");
  byText(picker.element, "None", "button").click();
  assert.equal(button(picker).disabled, true, "nothing ticked: not applicable, so natively disabled");
  assert.equal(button(picker).textContent, "Download");
  assert.equal(picker.getRequest(), null);
});

test("Best available is the default quality; a chip changes it and the folder follows audio", () => {
  const { picker } = makePicker();
  const chip = (label) => byText(picker.element, label, "button");
  assert.equal(chip("Best available").getAttribute("aria-checked"), "true");
  assert.equal(byClass(picker.element, "folder-line-value")[0].title, "C:\\Users\\me\\Videos\\Snag\\Fake_ Mix");
  chip("MP3").click();
  assert.equal(chip("MP3").getAttribute("aria-checked"), "true");
  assert.equal(chip("Best available").getAttribute("tabindex"), "-1");
  assert.equal(byClass(picker.element, "folder-line-value")[0].title, "C:\\Users\\me\\Music\\Snag\\Fake_ Mix");
  assert.deepEqual(picker.getRequest().quality, { kind: "mp3" });
});

test("arrow keys move between quality chips", () => {
  const { picker } = makePicker();
  const best = byText(picker.element, "Best available", "button");
  best.dispatch("keydown", { key: "ArrowRight" });
  assert.deepEqual(picker.getRequest().quality, { kind: "quick", height: 2160 });
  byText(picker.element, "≤2160p", "button").dispatch("keydown", { key: "End" });
  assert.deepEqual(picker.getRequest().quality, { kind: "best_audio" });
  byText(picker.element, "Original audio", "button").dispatch("keydown", { key: "ArrowRight" });
  assert.deepEqual(picker.getRequest().quality, { kind: "quick", height: null }, "wraps around");
});

test("Download sends the ticked videos with the quality, extras and a picked folder", async () => {
  const { picker, sent, toasts, queued } = makePicker();
  checkboxes(picker)[0].checked = false;
  checkboxes(picker)[0].dispatch("change");
  byText(picker.element, "≤720p", "button").click();
  byText(picker.element, "Change", "button").click();
  await settle();
  assert.equal(byClass(picker.element, "folder-line-value")[0].title, "D:\\Clips\\Fake_ Mix");
  button(picker).click();
  await settle();
  assert.equal(sent.length, 1);
  assert.deepEqual(sent[0].entries, [3]);
  assert.deepEqual(sent[0].quality, { kind: "quick", height: 720 });
  assert.equal(sent[0].dest_dir, "D:\\Clips");
  assert.equal(sent[0].url, LOOKED_UP, "the address looked up, not the playlist's page URL");
  assert.equal(sent[0].extras.subtitles, true);
  assert.deepEqual(toasts, [["Queued 1 video", "success"]]);
  assert.equal(queued.length, 1);
});

test("a refused batch shows the server's message and the button comes back", async () => {
  const { picker } = makePicker({ createBatch: async () => { throw new Error("Some of those videos aren't in this playlist. Look it up again."); } });
  button(picker).click();
  await settle();
  assert.ok(picker.element.textContent.includes("Some of those videos aren't in this playlist"));
  assert.equal(button(picker).disabled, false);
  assert.equal(button(picker).textContent, "Download 2 videos");
});

/** Live regions (alerts, status, aria-live) whose text holds text: what a screen reader announces it from. */
const announcers = (root, text) => findAll(root, (node) => (["alert", "status"].includes(node.getAttribute("role"))
  || ["polite", "assertive"].includes(node.getAttribute("aria-live"))) && node.textContent.includes(text));

test("a refused extras message is announced once: the picker's alert, with a quiet copy in the editor", async () => {
  const { ApiError } = await import("../../ui/api.js");
  const message = "Pick at least one SponsorBlock category.";
  const { picker } = makePicker({ createBatch: async () => { throw new ApiError(message, "invalid_extras", "edit_extras", { status: 400 }); } });
  button(picker).click();
  await settle();
  const live = announcers(picker.element, message);
  assert.equal(live.length, 1);
  assert.ok(live[0].classList.contains("panel-error"));
  assert.ok(byClass(picker.element, "extras-editor")[0].textContent.includes(message), "still shown by the extras");
});

test("a picker needs the address that was looked up", () => {
  assert.throws(() => createPlaylistPicker({ api: {}, info: INFO, settings: SETTINGS }), /looked-up address/);
});
