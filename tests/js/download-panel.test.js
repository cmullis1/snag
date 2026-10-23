// The download panel built on a fake DOM: the lookup's thumbnail URL, the server refusing the extras, and
// default folders that change while it is open.
import "./fake-dom.js";
import test from "node:test";
import assert from "node:assert/strict";

import { byClass, byText, findAll } from "./fake-dom.js";
import { createDownloadPanel } from "../../ui/components/download-panel.js";
import { createStore } from "../../ui/store.js";
import { ApiError } from "../../ui/api.js";
import { isExtrasError } from "../../ui/fields.js";

const ROW = {
  format_id: "299", label: "1080p60", ext: "mp4", kind: "video", height: 1080, fps: 60, size: 4000,
  size_is_estimate: false, audio_id: "140", has_audio: false, result_ext: "mp4",
};

const LOOKED_UP = "https://youtu.be/abc"; // the address looked up (its page URL is info.url)

function info(thumbnail) {
  return {
    type: "video", site: "youtube", video_id: "abc", url: "https://www.youtube.com/watch?v=abc", title: "A video",
    channel: "Someone", duration: 10, thumbnail, formats: { video_simple: [ROW], video_all: [ROW], audio: [], audio_tracks: [] },
    subtitle_langs: [], auto_subtitle_langs: [], has_chapters: false, playlist: null, live_status: null,
    js_runtime_missing: false, duplicate: null,
  };
}

const images = (panel) => findAll(panel.element, (node) => node.tagName === "IMG");

test("the panel shows an https thumbnail", () => {
  const panel = createDownloadPanel({ api: {}, url: LOOKED_UP, info: info("https://i.ytimg.com/vi/abc/hq.jpg") });
  assert.deepEqual(images(panel).map((img) => img.getAttribute("src")), ["https://i.ytimg.com/vi/abc/hq.jpg"]);
});

test("a thumbnail that isn't https: or data:image/ (the page's img-src) is left out instead of breaking the panel", () => {
  for (const url of ["javascript:alert(1)", "data:text/html,<b>x</b>", "file:///C:/x.jpg", "http://i.ytimg.com/vi/abc/hq.jpg",
    "blob:https://x.test/1", "/local.jpg"]) {
    const panel = createDownloadPanel({ api: {}, url: LOOKED_UP, info: info(url) });
    assert.equal(images(panel).length, 0, url);
    assert.ok(panel.element.textContent.includes("A video"));
  }
});

test("no thumbnail, no image", () => {
  assert.equal(images(createDownloadPanel({ api: {}, url: LOOKED_UP, info: info(null) })).length, 0);
});

// --- server errors and settings that change ---

const settle = () => new Promise((resolve) => setImmediate(resolve));
const SETTINGS = { video_dir: "C:\\Videos\\Snag", audio_dir: "C:\\Music\\Snag", default_extras: null };

function failingPanel(error) {
  const api = { createJob: async () => { throw error; } };
  return createDownloadPanel({ api, url: LOOKED_UP, info: info(null), settings: SETTINGS });
}

test("the server refusing the extras (invalid_extras / edit_extras) opens the editor with its message", async () => {
  assert.equal(isExtrasError({ kind: "invalid_extras" }), true);
  assert.equal(isExtrasError({ kind: "bad_request", action: "edit_extras" }), true);
  assert.equal(isExtrasError({ kind: "bad_request", message: "Bad extras" }), false, "the message alone doesn't count");
  assert.equal(isExtrasError(null), false);

  const message = "The trim end must come after the start.";
  const panel = failingPanel(new ApiError(message, "invalid_extras", "edit_extras", { status: 400 }));
  byText(panel.element, "Download", "button").click();
  await settle();
  assert.equal(byText(panel.element, "Done", "button").getAttribute("aria-expanded"), "true", "the editor is open");
  const editor = byClass(panel.element, "panel-editor")[0];
  assert.equal(editor.hidden, false);
  assert.ok(editor.textContent.includes(message), "the editor shows the server's message");
});

test("other errors leave the extras editor closed, whatever their wording", async () => {
  const panel = failingPanel(new ApiError("Snag doesn't know these extras: x", "bad_request", null, { status: 400 }));
  byText(panel.element, "Download", "button").click();
  await settle();
  assert.equal(byClass(panel.element, "panel-editor")[0].hidden, true);
  assert.ok(byClass(panel.element, "panel-error")[0].textContent.includes("Snag doesn't know these extras"));
});

test("the folder line follows a changed default folder until one is picked", async () => {
  const store = createStore();
  const picked = [];
  const panel = createDownloadPanel({
    api: {}, store, url: LOOKED_UP, info: info(null), settings: SETTINGS, pickFolder: async () => "E:\\Picked\\Here",
  });
  const folder = () => byClass(panel.element, "folder-line-value")[0].textContent;
  assert.equal(folder(), "Videos\\Snag");
  store.dispatch({ type: "settings", settings: { ...SETTINGS, video_dir: "D:\\Clips\\New" } });
  assert.equal(folder(), "Clips\\New");
  byText(panel.element, "Change", "button").click();
  await settle();
  picked.push(folder());
  store.dispatch({ type: "settings", settings: { ...SETTINGS, video_dir: "D:\\Other\\Place" } });
  assert.deepEqual([...picked, folder()], ["Picked\\Here", "Picked\\Here"], "a picked folder stays");
});

/** Live regions (alerts, status, aria-live) whose text holds text: what a screen reader announces it from. */
const announcers = (root, text) => findAll(root, (node) => (["alert", "status"].includes(node.getAttribute("role"))
  || ["polite", "assertive"].includes(node.getAttribute("aria-live"))) && node.textContent.includes(text));

test("a refused extras message is announced once: the panel's alert, with a quiet copy in the editor", async () => {
  const message = "The trim end must come after the start.";
  const panel = failingPanel(new ApiError(message, "invalid_extras", "edit_extras", { status: 400 }));
  byText(panel.element, "Download", "button").click();
  await settle();
  const live = announcers(panel.element, message);
  assert.equal(live.length, 1);
  assert.ok(live[0].classList.contains("panel-error"));
  assert.ok(byClass(panel.element, "panel-editor")[0].textContent.includes(message), "still shown by the extras");
});

// --- the playlist line ---

const IN_PLAYLIST = { playlist: { id: "PL1", url: "https://www.youtube.com/playlist?list=PL1" } };
const playlistLine = (panel) => byText(panel.element, "Choose videos", "button")?.parentNode ?? null;

test("a video in a playlist: the line is unnamed until setPlaylistName names it, isolated in a <bdi>", () => {
  const chosen = [];
  const panel = createDownloadPanel({ api: {}, url: LOOKED_UP, info: { ...info(null), ...IN_PLAYLIST }, onChoosePlaylist: (ref) => chosen.push(ref) });
  const line = playlistLine(panel);
  assert.equal(line.textContent.includes("This video is part of a playlist"), true);
  assert.equal(byClass(panel.element, "panel-playlist-name").length, 0);
  panel.setPlaylistName("قائمة Mix");
  assert.equal(line.textContent.includes("From playlist قائمة Mix"), true);
  assert.equal(line.textContent.includes("This video is part of a playlist"), false);
  const names = byClass(panel.element, "panel-playlist-name");
  assert.equal(names.length, 1);
  assert.equal(names[0].tagName, "BDI");
  assert.equal(names[0].textContent, "قائمة Mix");
  panel.setPlaylistName("Other");
  assert.deepEqual(byClass(panel.element, "panel-playlist-name").map((node) => node.textContent), ["Other"], "renamed, not added");
  byText(panel.element, "Choose videos", "button").click();
  assert.deepEqual(chosen, [IN_PLAYLIST.playlist], "the button still chooses the playlist");
});

test("setPlaylistName with a blank or missing name goes back to the unnamed line", () => {
  const panel = createDownloadPanel({ api: {}, url: LOOKED_UP, info: { ...info(null), ...IN_PLAYLIST }, onChoosePlaylist: () => {} });
  for (const name of ["   ", "", null, undefined, 42]) {
    panel.setPlaylistName("Mix");
    panel.setPlaylistName(name);
    assert.equal(playlistLine(panel).textContent.includes("This video is part of a playlist"), true, String(name));
    assert.equal(byClass(panel.element, "panel-playlist-name").length, 0, String(name));
  }
  panel.setPlaylistName("  Mix  ");
  assert.equal(byClass(panel.element, "panel-playlist-name")[0].textContent, "Mix", "trimmed");
});

test("setPlaylistName on a panel without a playlist line shows nothing", () => {
  for (const panel of [
    createDownloadPanel({ api: {}, url: LOOKED_UP, info: info(null), onChoosePlaylist: () => {} }),
    createDownloadPanel({ api: {}, url: LOOKED_UP, info: { ...info(null), ...IN_PLAYLIST } }),
  ]) {
    panel.setPlaylistName("Mix");
    assert.equal(panel.element.textContent.includes("playlist"), false);
    assert.equal(byClass(panel.element, "panel-playlist-name").length, 0);
  }
});

test("a data:image/ thumbnail is shown", () => {
  const url = "data:image/png;base64,iVBORw0KGgo=";
  const panel = createDownloadPanel({ api: {}, url: LOOKED_UP, info: info(url) });
  assert.deepEqual(images(panel).map((img) => img.getAttribute("src")), [url]);
});

// --- the address the download asks for ---

test("the download asks for the address that was looked up, not the lookup's page URL", async () => {
  // watch?v=abc&list=PL1 answers with abc's page; asking Snag for that page alone would be a new lookup.
  const lookedUp = "https://www.youtube.com/watch?v=abc&list=PL1";
  const sent = [];
  const api = { createJob: async (body) => { sent.push(body); return { id: "j1" }; } };
  const panel = createDownloadPanel({ api, url: lookedUp, info: { ...info(null), ...IN_PLAYLIST }, settings: SETTINGS });
  assert.equal(panel.getRequest().url, lookedUp);
  byText(panel.element, "Download", "button").click();
  await settle();
  assert.deepEqual(sent.map((body) => body.url), [lookedUp]);
});

test("a panel needs the address that was looked up", () => {
  assert.throws(() => createDownloadPanel({ api: {}, info: info(null) }), /looked-up address/);
});
