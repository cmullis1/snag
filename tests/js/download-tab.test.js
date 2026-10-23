// The Download tab on a fake DOM: a newer lookup cancels the one under way, late answers are dropped,
// and a kept lookup is shown at once.
import "./fake-dom.js";
import test from "node:test";
import assert from "node:assert/strict";

import { byClass, byText } from "./fake-dom.js";
import { createDownloadTab } from "../../web/download-tab.js";

const settle = async () => {
  for (let i = 0; i < 3; i += 1) await new Promise((resolve) => setImmediate(resolve));
};

const ROW = { format_id: "18", label: "360p", ext: "mp4", kind: "video", has_audio: true, result_ext: "mp4", size: 1000 };
const video = (id, title) => ({
  type: "video", site: "youtube", video_id: id, url: `https://y.test/${id}`, title, channel: null, duration: null,
  thumbnail: null, formats: { video_simple: [ROW], video_all: [ROW], audio: [], audio_tracks: [] }, subtitle_langs: [],
  auto_subtitle_langs: [], has_chapters: false, playlist: null, live_status: null, js_runtime_missing: false, duplicate: null,
});

/** Lookups answered by the test: answer(url, info) or fail(url, error). */
function make({ recall = () => null } = {}) {
  const asked = new Map(); // url -> {resolve, reject, signal}
  const jobs = []; // POST /api/jobs bodies
  const api = {
    createJob: async (body) => { jobs.push(body); return { id: `j${jobs.length}` }; },
    lookup: (url, { signal } = {}) => new Promise((resolve, reject) => asked.set(url, { resolve, reject, signal })),
  };
  const remembered = [];
  const tab = createDownloadTab({
    api, store: null, toast: null, getSettings: () => null, navigate: () => {}, recall,
    remember: (url, info) => remembered.push(url),
  });
  const shown = () => byClass(tab.element, "panel-title").map((node) => node.textContent);
  const errorText = () => byClass(tab.element, "lookup-error")[0].textContent;
  return { tab, asked, jobs, remembered, shown, errorText };
}

test("a newer link cancels the lookup under way, and the older answer is dropped", async () => {
  const { tab, asked, shown, remembered } = make();
  tab.show({ url: "https://y.test/a" });
  tab.show({ url: "https://y.test/b" });
  assert.equal(asked.get("https://y.test/a").signal.aborted, true, "the older lookup is cancelled");
  asked.get("https://y.test/b").resolve(video("b", "Video B"));
  await settle();
  asked.get("https://y.test/a").resolve(video("a", "Video A")); // a server that answered anyway
  await settle();
  assert.deepEqual(shown(), ["Video B"]);
  assert.deepEqual(remembered, ["https://y.test/b"]);
});

test("an older lookup that fails late shows no error over the newer result", async () => {
  const { tab, asked, shown, errorText } = make();
  tab.show({ url: "https://y.test/a" });
  tab.show({ url: "https://y.test/b" });
  asked.get("https://y.test/b").resolve(video("b", "Video B"));
  await settle();
  asked.get("https://y.test/a").reject(new Error("Snag couldn't reach YouTube"));
  await settle();
  assert.equal(errorText(), "");
  assert.deepEqual(shown(), ["Video B"]);
});

test("the same link shown again isn't looked up again; a failure lets it be retried", async () => {
  const { tab, asked, errorText } = make();
  tab.show({ url: "https://y.test/a" });
  asked.get("https://y.test/a").reject(new Error("Snag couldn't reach YouTube"));
  await settle();
  assert.equal(errorText(), "Snag couldn't reach YouTube");
  asked.clear();
  tab.show({ url: "https://y.test/a" });
  assert.ok(asked.has("https://y.test/a"), "looked up again after it failed");
  tab.show({ url: "https://y.test/a" });
  assert.equal(asked.size, 1);
});

test("a kept lookup is shown at once, cancelling one under way", async () => {
  const kept = video("k", "Kept video");
  const { tab, asked, shown } = make({ recall: (url) => (url === "https://y.test/k" ? kept : null) });
  tab.show({ url: "https://y.test/a" });
  tab.show({ url: "https://y.test/k" });
  assert.deepEqual(shown(), ["Kept video"]);
  assert.equal(asked.get("https://y.test/a").signal.aborted, true);
  assert.equal(asked.has("https://y.test/k"), false, "not looked up again");
});

test("a route link that isn't a link (a malformed one) says it didn't come through, and isn't looked up", () => {
  const { tab, asked, shown, errorText } = make();
  tab.show({ url: "%E0%A4%A" });
  assert.equal(asked.size, 0);
  assert.equal(errorText(), "That link didn't come through. Paste it again.");
  assert.deepEqual(shown(), []);
  assert.equal(byClass(tab.element, "paste-input")[0].value, "%E0%A4%A", "what arrived is in the box");
  tab.show({ url: "https://y.test/a" });
  assert.ok(asked.has("https://y.test/a"));
  assert.equal(errorText(), "");
});

test("a route link that may not be looked up by itself waits in the box for Look up", () => {
  const { tab, asked, shown } = make();
  tab.show({ url: "https://vimeo.test/1", lookUp: false });
  assert.equal(asked.size, 0, "not looked up");
  assert.deepEqual(shown(), []);
  assert.equal(byClass(tab.element, "paste-input")[0].value, "https://vimeo.test/1");
  assert.equal(byClass(tab.element, "lookup-status")[0].textContent, "Press Look up to look up this link.");
  tab.show({ url: "https://vimeo.test/1", lookUp: false });
  assert.equal(asked.size, 0, "shown again (the page redrawn), still waiting");
  byClass(tab.element, "paste")[0].dispatch("submit");
  assert.ok(asked.has("https://vimeo.test/1"), "Look up looks it up");
});

test("a download asks for the link that was looked up (a kept lookup's too), not the lookup's page URL", async () => {
  const kept = video("k", "Kept video");
  const { tab, asked, jobs } = make({ recall: (url) => (url === "https://y.test/k?list=L" ? kept : null) });
  tab.show({ url: "https://y.test/a?list=L" });
  asked.get("https://y.test/a?list=L").resolve(video("a", "Video A"));
  await settle();
  byText(tab.element, "Download", "button").click();
  await settle();
  tab.show({ url: "https://y.test/k?list=L" });
  byText(tab.element, "Download", "button").click();
  await settle();
  assert.deepEqual(jobs.map((body) => body.url), ["https://y.test/a?list=L", "https://y.test/k?list=L"]);
});
