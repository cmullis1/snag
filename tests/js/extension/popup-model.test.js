// The popup's pure rules (extension/popup/model.js): which view shows for which inputs, the links into
// the full tab, the paste box, the playlist card and the slim queue line.
import test from "node:test";
import assert from "node:assert/strict";

import {
  APP_TAB_PATTERNS, NOTHING_TEXT, OFFLINE_TEXT, SNAG_PAGE_TEXT, UNSUPPORTED_LINK_TEXT, VIEW, appLink, noticeFor, pageTarget, pastedTarget, playlistSummary,
  queueLine, selectView, singleVideoUrl,
} from "../../../extension/popup/model.js";
import { SNAG_APP_URL } from "../../../extension/lib/server.js";
import { OFFLINE_MESSAGE } from "../../../extension/lib/worker.js";
import { ROUTE, buildHash, parseHash } from "../../../web/router.js";
import { lookupTarget, playlistSummary as tabPlaylistSummary } from "../../../web/download-model.js";
import { createStore } from "../../../extension/ui/store.js";

const VIDEO = { type: "video", url: "https://www.youtube.com/watch?v=abc", title: "A video", playlist: null };
const IN_PLAYLIST = {
  ...VIDEO, url: "https://www.youtube.com/watch?v=abc", playlist: { playlist_id: "PL1", url: "https://www.youtube.com/playlist?list=PL1" },
};
const PLAYLIST = {
  type: "playlist", url: "https://www.youtube.com/playlist?list=PL1", title: "Mix",
  entries: [{ index: 1, duplicate: null }, { index: 2, duplicate: { id: 4 } }, { index: 3, duplicate: null }],
};
const READY = { updater: { setup: { state: "done" }, update: { available: null } }, tools: {} };
const SETTING_UP = { updater: { setup: { state: "running", tools: {} } }, tools: {} };
const SETUP_FAILED = { updater: { setup: { state: "error", message: "Couldn't download ffmpeg.", can_retry: true } }, tools: {} };

// --- the page ---

test("only web pages are looked up", () => {
  assert.deepEqual(pageTarget("https://www.youtube.com/watch?v=abc"), { target: "https://www.youtube.com/watch?v=abc", snag: false });
  assert.deepEqual(pageTarget("http://example.com/video"), { target: "http://example.com/video", snag: false });
  for (const url of ["chrome://extensions/", "edge://newtab/", "file:///C:/a.mp4", "about:blank", "chrome-extension://x/popup.html",
    "view-source:https://a.example/", "", undefined, null]) {
    assert.deepEqual(pageTarget(url), { target: null, snag: false }, String(url));
  }
});

test("Snag's own tab isn't looked up, but a link it shows is, read as the tab reads it", () => {
  for (const url of ["http://127.0.0.1:47823/", "http://localhost:47823/#history", "http://127.0.0.1:47823/#settings",
    "http://127.0.0.1:47823/#download", "http://127.0.0.1:47823/#playlist", "http://127.0.0.1:47823/#download?url=",
    "http://127.0.0.1:47823/#download?url=chrome%3A%2F%2Fextensions", "http://127.0.0.1:47823/#download?url=not%20a%20link"]) {
    assert.deepEqual(pageTarget(url), { target: null, snag: true }, url);
  }
  const links = [
    "https://www.youtube.com/watch?v=abc&list=PLx&index=3",
    "https://www.youtube.com/playlist?list=PL1",
    "https://example.com/%E2%9C%93?q=%26",
  ];
  for (const url of links) {
    for (const route of [ROUTE.DOWNLOAD, ROUTE.PLAYLIST]) {
      for (const base of ["http://127.0.0.1:47823/", "http://localhost:47823/"]) {
        const tab = `${base}${buildHash(route, { url })}`;
        assert.deepEqual(pageTarget(tab), { target: parseHash(new URL(tab).hash).params.url, snag: false }, tab);
        assert.equal(pageTarget(tab).target, url);
      }
    }
  }
  // Pasted into the address bar as it is (not encoded), the tab reads the rest of the hash as the link.
  const raw = "http://127.0.0.1:47823/#download?url=https://www.youtube.com/watch?v=abc&list=PLx";
  assert.equal(pageTarget(raw).target, parseHash(new URL(raw).hash).params.url);
  assert.equal(pageTarget("http://127.0.0.1:47823/#DOWNLOAD/?url=https%3A%2F%2Fa.example%2Fv").target, "https://a.example/v");
  // Another port or scheme is someone else's page.
  assert.deepEqual(pageTarget("http://127.0.0.1:8080/#download?url=x"), { target: "http://127.0.0.1:8080/#download?url=x", snag: false });
  assert.deepEqual(pageTarget("https://localhost:47823/"), { target: "https://localhost:47823/", snag: false });
});

test("nothing to download says so for the page, or that this is Snag", () => {
  assert.deepEqual(selectView({ target: null, status: READY }), { view: VIEW.NOTHING, text: NOTHING_TEXT, pasteError: null });
  assert.deepEqual(selectView({ target: null, snag: true, status: READY }), { view: VIEW.NOTHING, text: SNAG_PAGE_TEXT, pasteError: null });
  const unsupported = { error: { kind: "unsupported", message: "Unsupported URL" } };
  assert.deepEqual(selectView({ target: "https://example.com/", lookup: unsupported }),
    { view: VIEW.NOTHING, text: NOTHING_TEXT, pasteError: null });
  assert.deepEqual(selectView({ target: "https://example.com/", pasted: true, lookup: unsupported }),
    { view: VIEW.NOTHING, text: NOTHING_TEXT, pasteError: UNSUPPORTED_LINK_TEXT });
  assert.deepEqual(selectView({ target: "https://example.com/", pasted: true, snag: true, lookup: unsupported }),
    { view: VIEW.NOTHING, text: SNAG_PAGE_TEXT, pasteError: UNSUPPORTED_LINK_TEXT });
});

test("while the tab's address is being read, the view is loading, not nothing", () => {
  assert.deepEqual(selectView({ reading: true }), { view: VIEW.LOADING });
  assert.equal(selectView({ reading: true, offline: true }).view, VIEW.OFFLINE);
  assert.equal(selectView({ reading: true, status: SETTING_UP }).view, VIEW.SETUP);
});

// --- which view ---

test("Snag not answering is the offline view, whatever else is known", () => {
  assert.deepEqual(selectView({ target: VIDEO.url, offline: true }), { view: VIEW.OFFLINE, text: OFFLINE_TEXT });
  assert.equal(selectView({ target: null, offline: true, status: SETTING_UP }).view, VIEW.OFFLINE);
  assert.equal(selectView({ target: VIDEO.url, offline: true, lookup: { info: VIDEO } }).view, VIEW.OFFLINE);
  // The same words as the right-click menu's notification and the full tab's note.
  assert.equal(OFFLINE_TEXT, OFFLINE_MESSAGE);
});

test("first-run setup running or failed is the setup view, before anything is looked up", () => {
  const running = selectView({ target: VIDEO.url, status: SETTING_UP, lookup: null });
  assert.equal(running.view, VIEW.SETUP);
  assert.equal(running.title, "Setting up Snag…");
  assert.equal(running.failed, false);
  assert.equal(running.canRetry, false);
  const failed = selectView({ target: null, status: SETUP_FAILED });
  assert.equal(failed.view, VIEW.SETUP);
  assert.equal(failed.title, "Snag couldn't finish setting up");
  assert.equal(failed.message, "Couldn't download ffmpeg.");
  assert.equal(failed.failed, true);
  assert.equal(failed.canRetry, true);
  // Even a lookup that came back first gives way: downloads wait for setup anyway.
  assert.equal(selectView({ target: VIDEO.url, status: SETTING_UP, lookup: { info: VIDEO } }).view, VIEW.SETUP);
});

test("a page that isn't on the web is nothing to download", () => {
  assert.deepEqual(selectView({ target: null, status: READY }), { view: VIEW.NOTHING, text: NOTHING_TEXT, pasteError: null });
  assert.equal(selectView({ target: null }).view, VIEW.NOTHING, "the status isn't needed to know that");
});

test("a lookup under way is the loading view", () => {
  assert.equal(selectView({ target: VIDEO.url, status: READY, lookup: null }).view, VIEW.LOADING);
  assert.equal(selectView({ target: VIDEO.url }).view, VIEW.LOADING, "status not known yet");
});

test("a video is the video view; one in a playlist links to the playlist picker", () => {
  const video = selectView({ target: VIDEO.url, status: READY, lookup: { info: VIDEO } });
  assert.equal(video.view, VIEW.VIDEO);
  assert.equal(video.info, VIDEO);
  assert.equal(video.playlistLink, null);
  const inPlaylist = selectView({ target: IN_PLAYLIST.url, status: READY, lookup: { info: IN_PLAYLIST } });
  assert.equal(inPlaylist.view, VIEW.VIDEO);
  assert.equal(inPlaylist.playlistLink, appLink(ROUTE.PLAYLIST, IN_PLAYLIST.playlist.url));
  assert.equal(selectView({ target: VIDEO.url, lookup: { info: VIDEO } }).view, VIEW.VIDEO, "status not known yet");
});

test("a playlist is the playlist view with its summary and the picker's link", () => {
  const view = selectView({ target: PLAYLIST.url, status: READY, lookup: { info: PLAYLIST } });
  assert.equal(view.view, VIEW.PLAYLIST);
  assert.deepEqual(view.summary, { title: "Mix", text: "3 videos · 1 downloaded before" });
  assert.equal(view.chooseLink, appLink(ROUTE.PLAYLIST, PLAYLIST.url));
  assert.equal(view.singleVideo, null);
  const fromWatch = selectView({ target: "https://www.youtube.com/watch?v=xyz&list=PL1", lookup: { info: PLAYLIST } });
  assert.equal(fromWatch.singleVideo, "https://www.youtube.com/watch?v=xyz");
});

test("a page the site's extractor doesn't know is nothing to download; other failures say what went wrong", () => {
  const unsupported = { kind: "unsupported", message: "Nothing to download on this page" };
  assert.deepEqual(selectView({ target: "https://example.com/", lookup: { error: unsupported } }), { view: VIEW.NOTHING, text: NOTHING_TEXT, pasteError: null });
  const signIn = { kind: "sign_in", message: "Sign in to YouTube in Chrome or Edge, then retry" };
  assert.deepEqual(selectView({ target: VIDEO.url, status: READY, lookup: { error: signIn } }),
    { view: VIEW.ERROR, text: "Sign in to YouTube in Chrome or Edge, then retry", canRetry: true });
  assert.deepEqual(selectView({ target: VIDEO.url, lookup: { error: { kind: "http" } } }),
    { view: VIEW.ERROR, text: "Snag couldn't look this page up", canRetry: true });
});

test("a failed lookup offers Try again only when Snag says retrying can help", () => {
  // lookup.error is an ApiError: the server's ErrorInfo, when it sent one, is its .error.
  const failed = (canRetry) => ({
    kind: "unavailable", message: "This video requires payment to watch, so Snag can't download it.",
    error: { kind: "unavailable", message: "This video requires payment to watch, so Snag can't download it.", can_retry: canRetry },
  });
  assert.equal(selectView({ target: VIDEO.url, lookup: { error: failed(false) } }).canRetry, false);
  assert.equal(selectView({ target: VIDEO.url, lookup: { error: failed(true) } }).canRetry, true);
  // No verdict from Snag (a timeout, an answer that wasn't Snag's error JSON): asking again may help.
  assert.equal(selectView({ target: VIDEO.url, lookup: { error: { kind: "http", message: "x", error: null } } }).canRetry, true);
  assert.equal(selectView({ target: VIDEO.url, lookup: { error: { kind: "x", message: "x", error: { message: "x" } } } }).canRetry, true);
});

// --- the notice above the view ---

test("an update or a missing tool is a notice; nothing otherwise", () => {
  assert.equal(noticeFor(READY), null);
  assert.equal(noticeFor(null), null);
  assert.deepEqual(noticeFor({ ...READY, updater: { ...READY.updater, update: { available: "2026.10.01" } } }),
    { text: "yt-dlp 2026.10.01 is available", tone: "accent", action: "Update in Snag" });
  assert.deepEqual(noticeFor({ tools: { ytdlp: { present: false } } }),
    { text: "yt-dlp is missing", tone: "danger", action: "Fix in Snag" });
  assert.deepEqual(noticeFor({ tools: { deno: { present: false }, ytdlp: { present: true }, ffmpeg: { present: true }, ffprobe: { present: true } } }),
    { text: "Deno is missing", tone: "warning", action: "Fix in Snag" });
});

// --- links into the full tab ---

test("links into the full tab are the tab's own deep links and read back exactly", () => {
  assert.equal(appLink(), SNAG_APP_URL);
  for (const url of [
    "https://www.youtube.com/playlist?list=PL1",
    "https://www.youtube.com/watch?v=abc&list=PLx&index=3",
    "https://example.com/a b?c=d+e#frag",
    "https://example.com/%E2%9C%93?q=%26",
  ]) {
    const link = appLink(ROUTE.PLAYLIST, url);
    assert.equal(link, `${SNAG_APP_URL}${buildHash(ROUTE.PLAYLIST, { url })}`);
    const hash = new URL(link).hash;
    assert.deepEqual(parseHash(hash), { route: ROUTE.PLAYLIST, params: { url } }, url);
  }
  assert.equal(appLink(ROUTE.HISTORY), `${SNAG_APP_URL}#history`);
});

test("the app tab is found at either of Snag's addresses", () => {
  assert.deepEqual(APP_TAB_PATTERNS, ["http://127.0.0.1:47823/*", "http://localhost:47823/*"]);
});

// --- the paste box (the same rule as the full tab's) ---

test("the paste box finds a link the way the full tab's does", () => {
  for (const text of [
    "", "   ", "https://www.youtube.com/watch?v=abc", "  watch this: https://youtu.be/abc?t=3 thanks",
    "youtube.com/watch?v=abc", "www.example.co.uk/v/1", "not a link", "ftp://files.example/a.mp4",
  ]) {
    assert.deepEqual(pastedTarget(text), lookupTarget(text), JSON.stringify(text));
  }
  assert.deepEqual(pastedTarget("youtube.com/watch?v=abc"), { url: "https://youtube.com/watch?v=abc" });
});

test("the playlist card's summary is the full tab's", () => {
  for (const info of [PLAYLIST, { title: "", entries: [] }, { title: "One", entries: [{ duplicate: null }] }, null]) {
    assert.deepEqual(playlistSummary(info), tabPlaylistSummary(info));
  }
});

// --- Just this video ---

test("Just this video is offered for a playlist looked up from one video's address", () => {
  assert.equal(singleVideoUrl("https://www.youtube.com/watch?v=abc&list=PL1", PLAYLIST), "https://www.youtube.com/watch?v=abc");
  assert.equal(singleVideoUrl("https://m.youtube.com/watch?list=PL1&v=a_b-C", PLAYLIST), "https://www.youtube.com/watch?v=a_b-C");
  assert.equal(singleVideoUrl("https://youtu.be/xyz?list=PL1", PLAYLIST), "https://www.youtube.com/watch?v=xyz");
  assert.equal(singleVideoUrl("https://www.youtube.com/playlist?list=PL1", PLAYLIST), null);
  assert.equal(singleVideoUrl("https://example.com/watch?v=abc", PLAYLIST), null, "only YouTube's v= names a video");
  assert.equal(singleVideoUrl("https://www.youtube.com/watch?v=abc&list=PL1", VIDEO), null, "a video already is one");
  assert.equal(singleVideoUrl("https://www.youtube.com/watch?v=a%20b", PLAYLIST), null, "not a video id");
  assert.equal(singleVideoUrl("https://youtu.be/%E0%A4%A", PLAYLIST), null, "a malformed escape is no id");
  assert.equal(singleVideoUrl(null, PLAYLIST), null);
});

// --- the slim queue line ---

function storeWith(jobs, queue = { paused: false }) {
  const store = createStore();
  store.dispatch({ type: "snapshot", jobs, queue, status: {} });
  return store.getState();
}

test("the queue line counts the downloads and shows their combined progress", () => {
  assert.deepEqual(queueLine(createStore().getState()), { hidden: true, text: "No downloads", progress: null, paused: false });
  const state = storeWith([
    { id: "a", state: "downloading", percent: 40, created_at: 1 },
    { id: "b", state: "downloading", percent: 60, created_at: 2 },
    { id: "c", state: "waiting", created_at: 3 },
  ]);
  const line = queueLine(state);
  assert.equal(line.hidden, false);
  assert.equal(line.text, "2 downloading · 1 waiting");
  assert.equal(typeof line.progress, "number");
  assert.equal(line.paused, false);
  assert.equal(queueLine(storeWith([{ id: "d", state: "done", created_at: 1 }])).progress, null);
  assert.equal(queueLine(storeWith([{ id: "d", state: "done", created_at: 1 }])).text, "1 done");
  assert.equal(queueLine(storeWith([{ id: "a", state: "waiting", created_at: 1 }], { paused: true })).paused, true);
});
