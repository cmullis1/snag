// The popup (extension/popup/app.js) on a fake DOM, with a stand-in `chrome` and API: what it asks
// Snag for, and which view it draws for each answer.
import "../fake-dom.js";
import test from "node:test";
import assert from "node:assert/strict";

import { byClass, byText, find, findAll, isHidden } from "../fake-dom.js";
import { createPopup } from "../../../extension/popup/app.js";
import { NOTHING_TEXT, OFFLINE_TEXT, SNAG_PAGE_TEXT, UNSUPPORTED_LINK_TEXT, appLink } from "../../../extension/popup/model.js";
import { createStore } from "../../../extension/ui/store.js";
import { ApiError } from "../../../extension/ui/api.js";

const settle = async () => {
  for (let i = 0; i < 5; i += 1) await new Promise((resolve) => setImmediate(resolve));
};

const ROW = {
  format_id: "299", label: "1080p60", ext: "mp4", kind: "video", height: 1080, fps: 60, size: 4000,
  size_is_estimate: false, audio_id: "140", has_audio: false, result_ext: "mp4",
};
const VIDEO = {
  type: "video", site: "youtube", video_id: "abc", url: "https://www.youtube.com/watch?v=abc", title: "A video",
  channel: "Someone", duration: 10, thumbnail: null, formats: { video_simple: [ROW], video_all: [ROW], audio: [], audio_tracks: [] },
  subtitle_langs: [], auto_subtitle_langs: [], has_chapters: false, playlist: null, live_status: null,
  js_runtime_missing: false, duplicate: null,
};
const IN_PLAYLIST = { ...VIDEO, playlist: { playlist_id: "PL1", url: "https://www.youtube.com/playlist?list=PL1" } };
const PLAYLIST = {
  type: "playlist", site: "youtube", url: "https://www.youtube.com/playlist?list=PL1", title: "Mix",
  entries: [{ index: 1, duplicate: null }, { index: 2, duplicate: null }],
};
const READY = { version: "0.1.0", tools: {}, updater: { setup: { state: "done" }, update: { available: null } } };
const SETTINGS = { video_dir: "C:\\Videos", audio_dir: "C:\\Music", default_extras: null };
const COOKIE = { domain: ".youtube.com", name: "SID", value: "v", path: "/", secure: true, httpOnly: true, hostOnly: false, session: false };

function stubChrome({ tabUrl = VIDEO.url, cookies = [COOKIE], open = [] } = {}) {
  const calls = [];
  return {
    calls,
    tabs: {
      query: async (filter) => {
        calls.push(["tabs.query", filter]);
        return filter.active ? [{ id: 1, windowId: 1, ...(tabUrl ? { url: tabUrl } : {}) }] : open;
      },
      update: async (id, props) => calls.push(["tabs.update", id, props]),
      create: async (props) => calls.push(["tabs.create", props]),
    },
    windows: { update: async (id, props) => calls.push(["windows.update", id, props]) },
    cookies: {
      getAll: async (filter) => {
        calls.push(["cookies.getAll", filter]);
        return cookies;
      },
    },
  };
}

/** answers: {lookup(url, options), status, settings}: values, or functions, or errors to throw. */
function fakeApi({ lookup = () => VIDEO, status = READY, settings = SETTINGS } = {}) {
  const calls = [];
  const answer = async (value, ...args) => {
    const result = typeof value === "function" ? await value(...args) : value;
    if (result instanceof Error) throw result;
    return result;
  };
  return {
    calls,
    lookup: (url, options) => {
      calls.push(["lookup", url, options]);
      return answer(lookup, url, options);
    },
    getStatus: () => {
      calls.push(["getStatus"]);
      return answer(status);
    },
    getSettings: () => {
      calls.push(["getSettings"]);
      return answer(settings);
    },
    createJob: async (body) => {
      calls.push(["createJob", body]);
      return { id: "j1", spec: { title: "A video" } };
    },
  };
}

const offline = () => new ApiError("Snag isn't running", "offline");

const signIn = () => new ApiError("Sign in to YouTube in Chrome or Edge, then retry", "sign_in", "retry");

/** A promise the test settles: held.release(value) (an Error is thrown by fakeApi's answer). */
function hold() {
  let release;
  const promise = new Promise((resolve) => {
    release = resolve;
  });
  return { promise, release };
}

/** toast stand-in recording what is shown. */
function stubToast() {
  const shown = [];
  return { shown, show: (message, options) => shown.push([message, options]) };
}

async function open({ chrome = stubChrome(), api = fakeApi(), store = createStore(), toast = null } = {}) {
  const closed = [];
  const popup = createPopup({ chrome, api, store, toast, closeWindow: () => closed.push(true) });
  await popup.start();
  await settle();
  return { popup, chrome, api, store, closed, root: popup.element };
}

const text = (node) => node.textContent;
const visibleButton = (root, label) => {
  const button = byText(root, label, "button");
  return button && !isHidden(button) ? button : null;
};

// --- what it asks for ---

const lookups = (api) => api.calls.filter(([name]) => name === "lookup");
const readCookies = (chrome) => chrome.calls.filter(([name]) => name === "cookies.getAll");

test("the tab's page is looked up without cookies; they aren't even read", async () => {
  const { chrome, api } = await open();
  assert.deepEqual(chrome.calls[0], ["tabs.query", { active: true, currentWindow: true }]);
  assert.deepEqual(readCookies(chrome), []);
  assert.equal(lookups(api).length, 1);
  assert.equal(lookups(api)[0][1], VIDEO.url);
  assert.equal(lookups(api)[0][2]?.cookies, undefined);
});

test("a page that needs sign-in is looked up again with that site's cookies, and its download carries them", async () => {
  const api = fakeApi({ lookup: (url, options) => (options?.cookies ? VIDEO : signIn()) });
  const { chrome, root } = await open({ api });
  assert.deepEqual(readCookies(chrome), [["cookies.getAll", { url: VIDEO.url }]]);
  const [first, second] = lookups(api);
  assert.equal(first[2]?.cookies, undefined);
  assert.equal(second[1], VIDEO.url);
  assert.equal(second[2].cookies[0].name, "SID");
  byText(root, "Download", "button").click();
  await settle();
  const job = api.calls.find(([name]) => name === "createJob");
  assert.equal(job[1].cookies[0].name, "SID");
});

test("a page that needs sign-in on a site without cookies says so, without sending an empty list", async () => {
  const api = fakeApi({ lookup: signIn });
  const { root } = await open({ api, chrome: stubChrome({ cookies: [] }) });
  assert.equal(lookups(api).length, 1, "an empty list would clear what Snag keeps for the site");
  assert.ok(text(root).includes("Sign in to YouTube in Chrome or Edge, then retry"));
});

// --- the views ---

test("a video: the compact download panel, and its download sends no cookies", async () => {
  const { root, api } = await open();
  const panel = byClass(root, "panel")[0];
  assert.ok(panel.classList.contains("panel-compact"));
  assert.ok(text(root).includes("A video"));
  assert.equal(visibleButton(root, "Choose videos"), null, "not in a playlist");
  byText(root, "Download", "button").click();
  await settle();
  const job = api.calls.find(([name]) => name === "createJob");
  assert.equal(job[1].url, VIDEO.url);
  assert.equal("cookies" in job[1], false);
});

test("a video in a playlist: its download asks for the tab's address, which Snag looked up", async () => {
  const tabUrl = "https://www.youtube.com/watch?v=abc&list=PL1";
  const { root, api } = await open({ chrome: stubChrome({ tabUrl }), api: fakeApi({ lookup: IN_PLAYLIST }) });
  byText(root, "Download", "button").click();
  await settle();
  const job = api.calls.find(([name]) => name === "createJob");
  assert.equal(job[1].url, tabUrl, "not the lookup's page URL, which Snag would look up again");
});

test("a video in a playlist: Choose videos opens the full tab's picker and closes the popup", async () => {
  const { root, chrome, closed } = await open({ api: fakeApi({ lookup: IN_PLAYLIST }) });
  assert.ok(byClass(root, "panel").length === 1);
  visibleButton(root, "Choose videos").click();
  await settle();
  assert.deepEqual(chrome.calls.at(-1), ["tabs.create", { url: appLink("playlist", IN_PLAYLIST.playlist.url) }]);
  assert.equal(closed.length, 1);
});

test("a playlist: its name and count, and Choose videos", async () => {
  const { root, chrome } = await open({ chrome: stubChrome({ tabUrl: PLAYLIST.url }), api: fakeApi({ lookup: PLAYLIST }) });
  assert.ok(text(root).includes("Mix"));
  assert.ok(text(root).includes("Playlist · 2 videos"));
  assert.equal(visibleButton(root, "Just this video"), null);
  visibleButton(root, "Choose videos").click();
  await settle();
  assert.deepEqual(chrome.calls.at(-1), ["tabs.create", { url: appLink("playlist", PLAYLIST.url) }]);
});

test("a playlist from a video's address: Just this video looks the video up", async () => {
  const tabUrl = "https://www.youtube.com/watch?v=abc&list=PL1";
  const api = fakeApi({ lookup: (url) => (url === tabUrl ? PLAYLIST : VIDEO) });
  const { root } = await open({ chrome: stubChrome({ tabUrl }), api });
  visibleButton(root, "Just this video").click();
  await settle();
  assert.deepEqual(api.calls.filter(([name]) => name === "lookup").map(([, url]) => url), [tabUrl, "https://www.youtube.com/watch?v=abc"]);
  assert.equal(byClass(root, "panel").length, 1);
});

test("a chrome:// page: nothing to download, nothing looked up, and a paste box", async () => {
  const { root, api } = await open({ chrome: stubChrome({ tabUrl: "chrome://extensions/" }) });
  assert.ok(text(root).includes(NOTHING_TEXT));
  assert.equal(api.calls.some(([name]) => name === "lookup"), false);
  const input = find(root, (node) => node.tagName === "INPUT");
  input.value = "youtube.com/watch?v=abc";
  find(root, (node) => node.tagName === "FORM").dispatch("submit");
  await settle();
  assert.deepEqual(api.calls.find(([name]) => name === "lookup").slice(0, 2), ["lookup", "https://youtube.com/watch?v=abc"]);
  assert.equal(byClass(root, "panel").length, 1);
});

test("a tab whose address the popup can't see is nothing to download too", async () => {
  const { root } = await open({ chrome: stubChrome({ tabUrl: null }) });
  assert.ok(text(root).includes(NOTHING_TEXT));
});

test("the paste box says when the text isn't a link", async () => {
  const { root, api } = await open({ chrome: stubChrome({ tabUrl: "edge://newtab/" }) });
  find(root, (node) => node.tagName === "INPUT").value = "hello";
  find(root, (node) => node.tagName === "FORM").dispatch("submit");
  await settle();
  assert.ok(text(root).includes("That isn't a link"));
  assert.equal(api.calls.some(([name]) => name === "lookup"), false);
});

test("a page the lookup says is unsupported is nothing to download", async () => {
  const api = fakeApi({ lookup: () => new ApiError("Nothing to download on this page", "unsupported") });
  const { root } = await open({ chrome: stubChrome({ tabUrl: "https://example.com/" }), api });
  assert.ok(text(root).includes(NOTHING_TEXT));
  assert.ok(find(root, (node) => node.tagName === "INPUT"), "with the paste box");
});

test("another lookup failure says what went wrong, with Try again", async () => {
  let fail = true;
  const api = fakeApi({ lookup: () => (fail ? new ApiError("Sign in to YouTube in Chrome or Edge, then retry", "sign_in") : VIDEO) });
  const { root } = await open({ api });
  assert.ok(text(root).includes("Sign in to YouTube in Chrome or Edge, then retry"));
  fail = false;
  visibleButton(root, "Try again").click();
  await settle();
  assert.equal(byClass(root, "panel").length, 1);
});

test("a lookup failure retrying can't help says why, without Try again", async () => {
  const message = "This video requires payment to watch, so Snag can't download it.";
  const error = { kind: "unavailable", message, action: null, can_retry: false, keep_partial: false, details: "" };
  const api = fakeApi({ lookup: () => new ApiError(message, "unavailable", null, { status: 422, error }) });
  const { root } = await open({ api });
  assert.ok(text(root).includes(message));
  assert.equal(visibleButton(root, "Try again"), null);
  assert.ok(find(root, (node) => node.tagName === "INPUT"), "the paste box is still there");
});

test("Snag not running: the offline message, and Try again asks again", async () => {
  let running = false;
  const api = fakeApi({ status: () => (running ? READY : offline()), lookup: () => (running ? VIDEO : offline()) });
  const { root } = await open({ api });
  assert.ok(text(root).includes(OFFLINE_TEXT));
  assert.equal(byClass(root, "panel").length, 0);
  assert.equal(visibleButton(root, "Open app"), null, "there is no app to open");
  running = true;
  visibleButton(root, "Try again").click();
  await settle();
  assert.equal(text(root).includes(OFFLINE_TEXT), false);
  assert.equal(byClass(root, "panel").length, 1);
  assert.ok(visibleButton(root, "Open app"));
});

test("setting up: a notice with Open app instead of the download screen, until setup is done", async () => {
  const store = createStore();
  const settingUp = { ...READY, updater: { setup: { state: "running", tools: {} } } };
  const { root, chrome } = await open({ api: fakeApi({ status: settingUp }), store });
  assert.ok(text(root).includes("Setting up Snag…"));
  assert.equal(byClass(root, "panel").length, 0);
  // The hub's snapshot says setup has finished: the video shows.
  store.dispatch({ type: "snapshot", jobs: [], queue: { paused: false }, status: READY });
  await settle();
  assert.equal(text(root).includes("Setting up Snag…"), false);
  assert.equal(byClass(root, "panel").length, 1);
  assert.equal(findAll(root, (node) => node.tagName === "BUTTON" && text(node) === "Open app").length, 1, "the header's only");
});

test("setting up: See progress in Snag opens the full tab, which shows the setup screen", async () => {
  const settingUp = { ...READY, updater: { setup: { state: "running", tools: {} } } };
  const { root, chrome } = await open({ api: fakeApi({ status: settingUp }) });
  visibleButton(root, "See progress in Snag").click();
  await settle();
  assert.deepEqual(chrome.calls.at(-1), ["tabs.create", { url: appLink() }]);
});

test("setup failed: Retry setup asks Snag to try again, and says so when that fails", async () => {
  const failed = { ...READY, updater: { setup: { state: "error", message: "Couldn't download ffmpeg.", can_retry: true } } };
  const api = fakeApi({ status: failed });
  let refuse = false;
  api.retrySetup = async () => {
    api.calls.push(["retrySetup"]);
    if (refuse) throw new ApiError("Setup is already running", "http");
    return { started: true };
  };
  const { root } = await open({ api });
  assert.ok(text(root).includes("Couldn't download ffmpeg."));
  visibleButton(root, "Retry setup").click();
  await settle();
  assert.deepEqual(api.calls.at(-1), ["retrySetup"]);
  refuse = true;
  visibleButton(root, "Retry setup").click();
  await settle();
  assert.ok(text(root).includes("Setup is already running"));
});

test("an available update is a notice above the view, linking to Settings", async () => {
  const status = { ...READY, updater: { setup: { state: "done" }, update: { available: "2026.10.01" } } };
  const { root, chrome } = await open({ api: fakeApi({ status }) });
  const notice = byClass(root, "popup-notice")[0];
  assert.equal(isHidden(notice), false);
  assert.ok(text(notice).includes("yt-dlp 2026.10.01 is available"));
  byText(notice, "Update in Snag", "button").click();
  await settle();
  assert.deepEqual(chrome.calls.at(-1), ["tabs.create", { url: appLink("settings") }]);
});

test("Open app in the header brings an open full tab forward", async () => {
  const chrome = stubChrome({ open: [{ id: 5, windowId: 2, url: "http://127.0.0.1:47823/" }] });
  const { root, closed } = await open({ chrome });
  byText(byClass(root, "popup-header")[0], "Open app", "button").click();
  await settle();
  assert.deepEqual(chrome.calls.slice(-2), [["tabs.update", 5, { active: true }], ["windows.update", 2, { focused: true }]]);
  assert.equal(closed.length, 1);
});

test("the queue line follows the hub's jobs", async () => {
  const store = createStore();
  const { root } = await open({ store });
  const line = byClass(root, "popup-queue")[0];
  assert.equal(isHidden(line), true, "no downloads, no line");
  store.dispatch({
    type: "snapshot", status: READY, queue: { paused: false },
    jobs: [{ id: "a", state: "downloading", percent: 50, created_at: 1 }, { id: "b", state: "waiting", created_at: 2 }],
  });
  assert.equal(isHidden(line), false);
  assert.ok(text(line).includes("1 downloading · 1 waiting"));
});

test("the settings are loaded for the panel's folder when the hub hasn't sent them", async () => {
  const store = createStore();
  const { root } = await open({ store });
  assert.equal(store.getState().settings, SETTINGS);
  assert.ok(text(root).includes("C:\\Videos"));
});

// --- the playlist a video is in ---

test("a video in a playlist: the line shows at once, and names the playlist once it's looked up", async () => {
  const held = hold();
  const api = fakeApi({ lookup: (url) => (url === IN_PLAYLIST.playlist.url ? held.promise : IN_PLAYLIST) });
  const { root } = await open({ api });
  assert.equal(byClass(root, "panel").length, 1);
  assert.ok(text(root).includes("This video is part of a playlist"));
  assert.deepEqual(lookups(api).map(([, url, options]) => [url, options?.cookies]), [
    [VIDEO.url, undefined], [IN_PLAYLIST.playlist.url, undefined],
  ]);
  held.release(PLAYLIST);
  await settle();
  assert.ok(text(root).includes("From playlist Mix"));
  assert.equal(text(root).includes("This video is part of a playlist"), false);
  const name = byClass(root, "panel-playlist-name")[0];
  assert.equal(text(name), "Mix");
  assert.equal(name.tagName, "BDI", "a right-to-left name is isolated");
  assert.ok(visibleButton(root, "Choose videos"));
});

test("a video in a playlist: a failed playlist lookup leaves the line as it is", async () => {
  const api = fakeApi({ lookup: (url) => (url === IN_PLAYLIST.playlist.url ? new ApiError("Video unavailable", "unavailable") : IN_PLAYLIST) });
  const { root } = await open({ api });
  assert.ok(text(root).includes("This video is part of a playlist"));
  assert.ok(visibleButton(root, "Choose videos"));
});

test("a video in a playlist: the playlist's name arriving after the view changed is dropped", async () => {
  const held = hold();
  const store = createStore();
  const api = fakeApi({ lookup: (url) => (url === IN_PLAYLIST.playlist.url ? held.promise : IN_PLAYLIST) });
  const { root } = await open({ api, store });
  const settingUp = { ...READY, updater: { setup: { state: "running", tools: {} } } };
  store.dispatch({ type: "snapshot", jobs: [], queue: { paused: false }, status: settingUp });
  await settle();
  held.release(PLAYLIST);
  await settle();
  assert.equal(text(root).includes("From playlist"), false);
  assert.ok(text(root).includes("Setting up Snag…"));
});

// --- the paste box ---

const pasteInput = (root) => find(root, (node) => node.tagName === "INPUT");
const submitPaste = (root) => find(root, (node) => node.tagName === "FORM").dispatch("submit");

test("a pasted link Snag can't download from keeps its text, says so in an alert, and keeps focus in the box", async () => {
  const api = fakeApi({ lookup: () => new ApiError("Unsupported URL", "unsupported") });
  const { root } = await open({ chrome: stubChrome({ tabUrl: "edge://newtab/" }), api });
  pasteInput(root).value = "https://example.com/page";
  pasteInput(root).focus();
  submitPaste(root);
  await settle();
  assert.equal(lookups(api)[0][1], "https://example.com/page");
  const input = pasteInput(root);
  assert.equal(input.value, "https://example.com/page");
  const alert = find(root, (node) => node.getAttribute?.("role") === "alert" && text(node) === UNSUPPORTED_LINK_TEXT);
  assert.ok(alert, "the field error");
  assert.equal(UNSUPPORTED_LINK_TEXT, "Snag can't download from that link");
  assert.equal(globalThis.document.activeElement, input);
  // The next try clears it.
  input.value = "hello";
  submitPaste(root);
  await settle();
  assert.equal(text(root).includes(UNSUPPORTED_LINK_TEXT), false);
  assert.ok(text(root).includes("That isn't a link"));
});

test("the tab's own unsupported page shows no field error", async () => {
  const api = fakeApi({ lookup: () => new ApiError("Unsupported URL", "unsupported") });
  const { root } = await open({ chrome: stubChrome({ tabUrl: "https://example.com/" }), api });
  assert.ok(text(root).includes(NOTHING_TEXT));
  assert.equal(text(root).includes(UNSUPPORTED_LINK_TEXT), false);
});

// --- recovering ---

test("Snag starting while the popup is open: the hub getting through asks everything again", async () => {
  let running = false;
  const store = createStore();
  const api = fakeApi({ status: () => (running ? READY : offline()), lookup: () => (running ? VIDEO : offline()) });
  const { root } = await open({ api, store });
  assert.ok(text(root).includes(OFFLINE_TEXT));
  running = true;
  store.dispatch({ type: "connection", state: "connecting" });
  await settle();
  assert.ok(text(root).includes(OFFLINE_TEXT), "connecting isn't through yet");
  store.dispatch({ type: "connection", state: "connected" });
  await settle();
  assert.equal(text(root).includes(OFFLINE_TEXT), false);
  assert.equal(byClass(root, "panel").length, 1);
});

test("a lookup that failed while Snag was setting up is asked again once setup is done", async () => {
  let ready = false;
  const store = createStore();
  const settingUp = { ...READY, updater: { setup: { state: "running", tools: {} } } };
  const api = fakeApi({ status: settingUp, lookup: () => (ready ? VIDEO : new ApiError("yt-dlp is missing", "unknown")) });
  const { root } = await open({ api, store });
  assert.ok(text(root).includes("Setting up Snag…"));
  assert.equal(lookups(api).length, 1);
  ready = true;
  store.dispatch({ type: "snapshot", jobs: [], queue: { paused: false }, status: READY });
  await settle();
  assert.equal(lookups(api).length, 2);
  assert.equal(byClass(root, "panel").length, 1);
  assert.equal(text(root).includes("yt-dlp is missing"), false);
});

test("an older lookup's answer arriving after a newer one is dropped", async () => {
  let running = false;
  const first = hold();
  const store = createStore();
  const api = fakeApi({
    status: () => (running ? READY : offline()),
    lookup: () => (lookups(api).length === 1 ? first.promise : VIDEO),
  });
  const { root } = await open({ api, store });
  assert.ok(text(root).includes(OFFLINE_TEXT));
  running = true;
  store.dispatch({ type: "connection", state: "connected" });
  await settle();
  assert.equal(lookups(api).length, 2);
  assert.equal(byClass(root, "panel").length, 1);
  first.release(new ApiError("Video unavailable", "unavailable"));
  await settle();
  assert.equal(byClass(root, "panel").length, 1);
  assert.equal(text(root).includes("Video unavailable"), false);
});

// --- accessibility ---

test("the popup's heading is Snag", async () => {
  const { root } = await open();
  const heading = find(root, (node) => node.tagName === "H1");
  assert.equal(text(heading), "Snag");
});

test("nothing to download: focus starts in the paste box", async () => {
  const { root } = await open({ chrome: stubChrome({ tabUrl: "chrome://extensions/" }) });
  assert.equal(globalThis.document.activeElement, pasteInput(root));
});

test("status regions hold only their text, no buttons", async () => {
  const settingUp = { ...READY, updater: { setup: { state: "running", tools: {} } } };
  const failed = { ...READY, updater: { setup: { state: "error", message: "Couldn't download ffmpeg.", can_retry: true } } };
  const held = hold();
  for (const api of [fakeApi({ status: settingUp }), fakeApi({ status: failed }), fakeApi({ lookup: () => held.promise })]) {
    const { root } = await open({ api });
    const regions = findAll(root, (node) => node.getAttribute?.("role") === "status");
    assert.ok(regions.length > 0);
    for (const region of regions) {
      assert.equal(findAll(region, (node) => node.tagName === "BUTTON").length, 0, text(region));
      assert.ok(text(region).trim());
    }
  }
});

// --- opening the full tab ---

test("a tab that can't be opened is a toast, and the popup stays open", async () => {
  const chrome = stubChrome();
  chrome.tabs.create = async () => {
    throw new Error("Tabs cannot be edited right now");
  };
  const toast = stubToast();
  const { root, closed } = await open({ chrome, toast });
  byText(byClass(root, "popup-header")[0], "Open app", "button").click();
  await settle();
  assert.equal(closed.length, 0);
  assert.deepEqual(toast.shown, [["Snag couldn't open its tab. Try again.", { kind: "error" }]]);
});

// --- Snag's own tab ---

test("Snag's own tab isn't looked up: it says this is Snag, with the paste box", async () => {
  for (const tabUrl of ["http://127.0.0.1:47823/", "http://127.0.0.1:47823/#history", "http://localhost:47823/#download"]) {
    const { root, api } = await open({ chrome: stubChrome({ tabUrl }) });
    assert.equal(lookups(api).length, 0, tabUrl);
    assert.ok(text(root).includes(SNAG_PAGE_TEXT), tabUrl);
    assert.ok(pasteInput(root), tabUrl);
  }
});

test("Snag's own tab showing a link looks that link up", async () => {
  const tabUrl = appLink("download", VIDEO.url);
  const { root, api } = await open({ chrome: stubChrome({ tabUrl }) });
  assert.deepEqual(lookups(api).map(([, url]) => url), [VIDEO.url]);
  assert.equal(byClass(root, "panel").length, 1);
  const playlist = await open({ chrome: stubChrome({ tabUrl: appLink("playlist", PLAYLIST.url) }), api: fakeApi({ lookup: PLAYLIST }) });
  assert.deepEqual(lookups(playlist.api).map(([, url]) => url), [PLAYLIST.url]);
  assert.ok(text(playlist.root).includes("Playlist · 2 videos"));
});
