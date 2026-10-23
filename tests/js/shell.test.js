// The full tab's frame (web/shell.js) on a fake DOM and a fake window: setup gating, the Settings dot,
// the playlist redirect, lookups shared between views, settings kept in the store, and focus after a
// route change.
import "./fake-dom.js";
import test from "node:test";
import assert from "node:assert/strict";

import { byClass, byText, find } from "./fake-dom.js";
import { h } from "../../ui/components/el.js";
import { createStore } from "../../ui/store.js";
import { createShell } from "../../web/shell.js";

const settle = async () => {
  for (let i = 0; i < 5; i += 1) await new Promise((resolve) => setImmediate(resolve));
};

/** A window whose hash changes fire hashchange later, as a browser's do; `entries` is the history. */
function fakeWindow(hash = "") {
  const listeners = [];
  const entries = [hash];
  const fire = () => setImmediate(() => listeners.forEach((fn) => fn()));
  const location = {
    current: hash,
    get hash() { return this.current; },
    set hash(value) {
      const next = value.startsWith("#") ? value : `#${value}`;
      if (next === this.current) return;
      this.current = next;
      entries.push(next);
      fire();
    },
    replace(value) {
      this.current = value;
      entries[entries.length - 1] = value;
      fire();
    },
  };
  return {
    entries, location,
    history: { replaceState: (_state, _title, url) => { location.current = url; entries[entries.length - 1] = url; } },
    addEventListener: (type, fn) => { if (type === "hashchange") listeners.push(fn); },
  };
}

const VIDEO_URL = "https://www.youtube.com/watch?v=abc";
const LIST_URL = "https://www.youtube.com/playlist?list=PL1";
const ROW = { format_id: "18", label: "360p", ext: "mp4", kind: "video", has_audio: true, result_ext: "mp4", size: 1000 };
const VIDEO = {
  type: "video", site: "youtube", video_id: "abc", url: VIDEO_URL, title: "A video", channel: "Someone", duration: 10,
  thumbnail: null, formats: { video_simple: [ROW], video_all: [ROW], audio: [], audio_tracks: [] }, subtitle_langs: [],
  auto_subtitle_langs: [], has_chapters: false, playlist: null, live_status: null, js_runtime_missing: false, duplicate: null,
};
const PLAYLIST = {
  type: "playlist", site: "youtube", playlist_id: "PL1", url: LIST_URL, title: "A playlist",
  entries: [{ index: 1, video_id: "abc", url: VIDEO_URL, title: "A video", duration: 10, thumbnail: null, item_index: null, duplicate: null }],
};
const SETTINGS = { video_dir: "C:\\Videos\\Snag", audio_dir: "C:\\Music\\Snag", default_extras: null, quick_extras: null, max_concurrent: 3 };

function fakeApi({ lookups = {}, settings = SETTINGS } = {}) {
  const calls = [];
  const api = {
    calls,
    settingsAnswer: null, // set to a function to hold GET /api/settings until it's called
    async lookup(url) {
      calls.push(["lookup", url]);
      if (!(url in lookups)) throw new Error("not found");
      return lookups[url];
    },
    getSettings() {
      calls.push(["getSettings"]);
      if (api.settingsAnswer) return new Promise((resolve) => { api.settingsAnswer = () => resolve(structuredClone(settings)); });
      return Promise.resolve(structuredClone(settings));
    },
    async listHistory() { return { entries: [], total: 0 }; },
    async pickFolder() { return { path: null }; },
    async retrySetup() { return { started: true }; },
  };
  return api;
}

const shells = [];
test.after(() => shells.forEach((shell) => shell.destroy()));

function make({ hash = "", lookups = {}, holdSettings = false } = {}) {
  const store = createStore();
  const api = fakeApi({ lookups });
  if (holdSettings) api.settingsAnswer = () => {};
  const win = fakeWindow(hash);
  const doc = { body: h("body"), title: "" };
  const toast = { element: h("div"), shown: [], show(text, options) { this.shown.push([text, options?.kind]); } };
  const shell = createShell({ api, store, toast, win, doc, setTimer: () => 0, clearTimer: () => {} });
  const started = shell.start();
  shells.push(shell);
  return { shell, store, api, win, doc, started };
}

const SNAPSHOT = (status = {}) => ({ type: "snapshot", jobs: [], queue: {}, status });

test("the setup screen stands in for the tabs, takes focus, and gives way when setup is done", async () => {
  const { shell, store, doc, started } = make({ hash: "#history" });
  await started;
  store.dispatch(SNAPSHOT({ updater: { setup: { state: "running", tools: {} } } }));
  const { nav, footer, setupView, views } = shell.elements;
  assert.equal(setupView.hidden, false);
  assert.equal(nav.hidden, true);
  assert.equal(footer.hidden, true);
  assert.ok(Object.values(views).every((view) => view.hidden));
  assert.equal(doc.title, "Setting up · Snag");
  assert.equal(document.activeElement?.tagName, "H1", "the setup title has focus");
  store.dispatch({ type: "status", updater: { setup: { state: "done", tools: {} } } });
  assert.equal(setupView.hidden, true);
  assert.equal(nav.hidden, false);
  assert.equal(views.history.hidden, false);
  assert.equal(doc.title, "History · Snag");
});

test("the Settings tab shows a dot while a tool is missing", async () => {
  const { shell, store, started } = make();
  await started;
  const { settingsDot, settingsDotLabel } = shell.elements;
  assert.equal(settingsDot.hidden, true);
  store.dispatch(SNAPSHOT({ tools: { ytdlp: { present: false }, ffmpeg: { present: true }, ffprobe: { present: true }, deno: { present: true } } }));
  assert.equal(settingsDot.hidden, false);
  assert.ok(settingsDot.classList.contains("dot-danger"));
  assert.equal(settingsDotLabel.textContent, " (yt-dlp is missing)");
});

test("a playlist link that turns out to be a video replaces the history entry, and the lookup is reused", async () => {
  const hash = `#playlist?url=${encodeURIComponent(VIDEO_URL)}`;
  const { api, win, shell, started } = make({ hash, lookups: { [VIDEO_URL]: VIDEO } });
  await started;
  await settle();
  assert.deepEqual(win.entries, [`#download?url=${encodeURIComponent(VIDEO_URL)}`], "Back doesn't return to the redirect");
  assert.equal(shell.elements.views.download.hidden, false);
  assert.ok(shell.elements.views.download.textContent.includes("A video"));
  assert.equal(api.calls.filter(([name]) => name === "lookup").length, 1, "looked up once");
});

test("the same playlist opened twice while it is looked up is looked up once", async () => {
  const hash = `#playlist?url=${encodeURIComponent(LIST_URL)}`;
  const { api, shell, started } = make({ hash, lookups: { [LIST_URL]: PLAYLIST } });
  shell.render();
  await started;
  await settle();
  assert.equal(api.calls.filter(([name]) => name === "lookup").length, 1);
  assert.ok(byClass(shell.elements.views.playlist, "picker-title").length === 1);
});

test("a playlist opened while the settings load isn't shown once another link is opened", async () => {
  const other = "https://www.youtube.com/playlist?list=PL2";
  const { shell, api, win } = make({
    hash: `#playlist?url=${encodeURIComponent(LIST_URL)}`, holdSettings: true,
    lookups: { [LIST_URL]: PLAYLIST, [other]: { ...PLAYLIST, url: other, title: "Another playlist" } },
  });
  shell.render(); // the picker for LIST_URL waits for the settings
  await settle();
  win.location.current = `#playlist?url=${encodeURIComponent(other)}`;
  shell.render();
  await settle();
  api.settingsAnswer();
  await settle();
  const titles = byClass(shell.elements.views.playlist, "picker-title").map((node) => node.textContent);
  assert.deepEqual(titles, ["Another playlist"]);
});

test("the settings live in the store: GET first, then the server's settings events; an answer an event overtook is dropped", async () => {
  const { store, api } = make({ holdSettings: true });
  store.dispatch({ type: "settings", settings: { ...SETTINGS, video_dir: "D:\\Newer" } });
  api.settingsAnswer();
  await settle();
  assert.equal(store.getState().settings.video_dir, "D:\\Newer");
  const fresh = make();
  await fresh.started;
  assert.equal(fresh.store.getState().settings.video_dir, SETTINGS.video_dir);
});

test("a route changed from inside the page moves focus to the new view", async () => {
  const { shell, started } = make({ hash: "#download" });
  await started;
  shell.navigate("history");
  await settle();
  assert.equal(document.activeElement?.getAttribute("type"), "search", "History's search box");
  shell.navigate("settings");
  await settle();
  assert.equal(document.activeElement?.textContent, "Folders", "Settings' first heading");
  shell.navigate("download");
  await settle();
  assert.ok(document.activeElement?.classList.contains("paste-input"), "the paste box");
});

test("Choose videos opens the picker with focus on its title, and Back returns to the video's link", async () => {
  const { shell, win, started } = make({ hash: `#download?url=${encodeURIComponent(LIST_URL)}`, lookups: { [LIST_URL]: PLAYLIST } });
  await started;
  await settle();
  byText(shell.elements.views.download, "Choose videos", "button").click();
  await settle();
  assert.equal(document.activeElement?.textContent, "A playlist");
  find(shell.elements.views.playlist, (node) => node.classList.contains("picker-back")).click();
  await settle();
  assert.equal(win.location.hash, `#download?url=${encodeURIComponent(LIST_URL)}`);
  assert.ok(shell.elements.views.download.textContent.includes("A playlist"), "the kept lookup is shown again");
});

test("callers sharing a lookup under way: only the latest builds the picker", async () => {
  const hash = `#playlist?url=${encodeURIComponent(LIST_URL)}`;
  const store = createStore();
  let answer;
  const api = fakeApi();
  api.lookup = (url) => {
    api.calls.push(["lookup", url]);
    return new Promise((resolve) => { answer = () => resolve(PLAYLIST); });
  };
  const win = fakeWindow(hash);
  const doc = { body: h("body"), title: "" };
  const toast = { element: h("div"), show() {} };
  const shell = createShell({ api, store, toast, win, doc, setTimer: () => 0, clearTimer: () => {} });
  shells.push(shell);
  await shell.start(); // the first caller, waiting on the lookup
  shell.render({ focus: true }); // the second (the hash again), sharing it
  const created = [];
  const createElement = document.createElement;
  document.createElement = (tag) => {
    const node = createElement(tag);
    created.push(node);
    return node;
  };
  try {
    answer();
    await settle();
  } finally {
    document.createElement = createElement;
  }
  assert.equal(api.calls.filter(([name]) => name === "lookup").length, 1);
  assert.equal(created.filter((node) => node.classList.contains("picker")).length, 1, "one picker built");
  assert.equal(document.activeElement?.textContent, "A playlist", "the latest caller's focus");
});

test("a malformed playlist link opens the Download tab saying it didn't come through", async () => {
  const { api, win, shell, started } = make({ hash: "#playlist?url=%E0%A4%A" });
  await started;
  await settle();
  assert.equal(api.calls.filter(([name]) => name === "lookup").length, 0);
  assert.deepEqual(win.entries, [`#download?url=${encodeURIComponent("%E0%A4%A")}`]);
  assert.equal(byClass(shell.elements.views.download, "lookup-error")[0].textContent, "That link didn't come through. Paste it again.");
});

// --- kept lookups go stale once a video is downloaded ---

const OTHER_URL = "https://www.youtube.com/watch?v=xyz";
const OTHER = { ...VIDEO, video_id: "xyz", url: OTHER_URL, title: "Another video" };
const lookupCount = (api, url) => api.calls.filter(([name, asked]) => name === "lookup" && asked === url).length;
const HISTORY_ENTRY = {
  id: 1, title: "A video", url: VIDEO_URL, kind: "video", format_summary: "360p MP4", size: 1000, finished_at: 1,
  status: "done", error_message: null, file_path: "C:\Videos\Snag\A video.mp4",
};

test("History's Download again looks the video up afresh, even when it was kept or is still shown", async () => {
  const { shell, api, started } = make({ hash: `#download?url=${encodeURIComponent(VIDEO_URL)}`, lookups: { [VIDEO_URL]: VIDEO, [OTHER_URL]: OTHER } });
  api.listHistory = async () => ({ entries: [HISTORY_ENTRY], total: 1 });
  await started;
  await settle();
  assert.equal(lookupCount(api, VIDEO_URL), 1);
  const again = () => find(shell.elements.views.history, (node) => node.getAttribute("aria-label") === "Download again: A video");

  // The Download tab still shows the video: Download again doesn't just show it as it was.
  shell.navigate("history");
  await settle();
  again().click();
  await settle();
  assert.equal(lookupCount(api, VIDEO_URL), 2);
  assert.equal(shell.elements.views.download.hidden, false);

  // Kept from before while another video is shown: not reused either.
  shell.navigate("download", { url: OTHER_URL });
  await settle();
  shell.navigate("history");
  await settle();
  again().click();
  await settle();
  assert.equal(lookupCount(api, VIDEO_URL), 3);
  assert.ok(shell.elements.views.download.textContent.includes("A video"));
});

test("a job for a video finishing drops its kept lookup; other videos' stay", async () => {
  const { shell, store, api, started } = make({ hash: `#download?url=${encodeURIComponent(VIDEO_URL)}`, lookups: { [VIDEO_URL]: VIDEO, [OTHER_URL]: OTHER } });
  await started;
  await settle();
  shell.navigate("download", { url: OTHER_URL });
  await settle();
  const finished = (videoId, url) => ({
    id: `job-${videoId}`, state: "done", created_at: 1,
    spec: { url, site: "youtube", video_id: videoId, title: "x", batch_id: null },
  });
  store.dispatch({ type: "job", job: finished("abc", VIDEO_URL) });
  shell.navigate("download", { url: VIDEO_URL });
  await settle();
  assert.equal(lookupCount(api, VIDEO_URL), 2, "looked up again: its \"already downloaded\" note is current");
  shell.navigate("download", { url: OTHER_URL });
  await settle();
  assert.equal(lookupCount(api, OTHER_URL), 1, "another video's kept lookup is still used");
});

test("a job for a video finishing drops a kept playlist that holds it", async () => {
  const { shell, store, api, started } = make({ hash: `#playlist?url=${encodeURIComponent(LIST_URL)}`, lookups: { [LIST_URL]: PLAYLIST, [OTHER_URL]: OTHER } });
  await started;
  await settle();
  shell.navigate("download", { url: OTHER_URL });
  await settle();
  store.dispatch({ type: "job", job: { id: "j", state: "done", created_at: 1, spec: { url: VIDEO_URL, site: "youtube", video_id: "abc" } } });
  shell.navigate("playlist", { url: LIST_URL });
  await settle();
  assert.equal(lookupCount(api, LIST_URL), 2);
});

test("the playlist picker marks videos already in the queue, unticked", async () => {
  const { shell, store, started } = make({ lookups: { [LIST_URL]: PLAYLIST } });
  await started;
  store.dispatch({ type: "job", job: { id: "j", state: "waiting", created_at: 1, spec: { url: VIDEO_URL, site: "youtube", video_id: "abc" } } });
  shell.navigate("playlist", { url: LIST_URL });
  await settle();
  await settle();
  const [box] = byClass(shell.elements.views.playlist, "picker-check");
  assert.equal(box.checked, false);
  assert.ok(shell.elements.views.playlist.textContent.includes("in queue"));
});

const VIMEO_URL = "https://vimeo.com/1";

test("a link from outside the page is looked up by itself only when it's YouTube's", async () => {
  const youtube = make({ hash: `#download?url=${encodeURIComponent(VIDEO_URL)}`, lookups: { [VIDEO_URL]: VIDEO } });
  await youtube.started;
  await settle();
  assert.equal(lookupCount(youtube.api, VIDEO_URL), 1);

  const other = make({ hash: `#download?url=${encodeURIComponent(VIMEO_URL)}`, lookups: { [VIMEO_URL]: { ...VIDEO, url: VIMEO_URL } } });
  await other.started;
  await settle();
  assert.equal(lookupCount(other.api, VIMEO_URL), 0, "waits for Look up");
  const view = other.shell.elements.views.download;
  assert.equal(byClass(view, "paste-input")[0].value, VIMEO_URL);
  assert.ok(view.textContent.includes("Press Look up"));
  byClass(view, "paste")[0].dispatch("submit");
  await settle();
  assert.equal(lookupCount(other.api, VIMEO_URL), 1);
});

test("a playlist link from outside the page that isn't YouTube's opens the Download tab waiting for Look up", async () => {
  const { shell, api, win, started } = make({ hash: `#playlist?url=${encodeURIComponent(VIMEO_URL)}` });
  await started;
  await settle();
  assert.equal(lookupCount(api, VIMEO_URL), 0);
  assert.equal(win.location.hash, `#download?url=${encodeURIComponent(VIMEO_URL)}`);
  assert.equal(byClass(shell.elements.views.download, "paste-input")[0].value, VIMEO_URL);
});

test("a link opened from inside the page (History's Download again) is looked up whatever its site", async () => {
  const { shell, api, started } = make({ lookups: { [VIMEO_URL]: { ...VIDEO, url: VIMEO_URL } } });
  await started;
  shell.navigate("download", { url: VIMEO_URL });
  await settle();
  assert.equal(lookupCount(api, VIMEO_URL), 1);
});

const IN_LIST_URL = `${VIDEO_URL}&list=PL1`;
const IN_LIST = { ...VIDEO, playlist: { playlist_id: "PL1", url: LIST_URL } };

test("a video looked up from inside a playlist isn't kept under its plain address, which would lose the playlist", async () => {
  const hash = `#download?url=${encodeURIComponent(IN_LIST_URL)}`;
  const { shell, api, win, started } = make({ hash, lookups: { [IN_LIST_URL]: IN_LIST, [VIDEO_URL]: VIDEO } });
  await started;
  await settle();
  assert.ok(shell.elements.views.download.textContent.includes("part of a playlist"));
  win.location.hash = `#download?url=${encodeURIComponent(VIDEO_URL)}`;
  await settle();
  assert.deepEqual(api.calls.filter(([name]) => name === "lookup").map(([, url]) => url), [IN_LIST_URL, VIDEO_URL]);
  assert.ok(!shell.elements.views.download.textContent.includes("part of a playlist"), "the plain address is looked up on its own");
});

test("a lookup is also kept under its page's own address when the two read alike", async () => {
  const SHORT_URL = "https://youtu.be/abc";
  const hash = `#download?url=${encodeURIComponent(SHORT_URL)}`;
  const { shell, api, win, started } = make({ hash, lookups: { [SHORT_URL]: VIDEO } });
  await started;
  await settle();
  win.location.hash = `#download?url=${encodeURIComponent(VIDEO_URL)}`;
  await settle();
  assert.deepEqual(api.calls.filter(([name]) => name === "lookup").map(([, url]) => url), [SHORT_URL], "not looked up again");
  assert.ok(shell.elements.views.download.textContent.includes("A video"));
});
