import test from "node:test";
import assert from "node:assert/strict";

import { SNAG_BASE, createSnagApi } from "../../../extension/lib/server.js";
import {
  ALARM_NAME,
  ALARM_PERIOD_MINUTES,
  KEEPALIVE_MS,
  MAX_RETRIES,
  MENU_ID,
  MENU_TITLE,
  RETRY_ALARM_PREFIX,
  RETRY_DELAY_MINUTES,
  VIDEO_MENU_ID,
  VIDEO_MENU_TITLE,
  NOT_WEB_MESSAGE,
  OFFLINE_MESSAGE,
  PAGE_CHANGED_MESSAGE,
  createWorker,
  errorMessage,
  notificationFor,
  registerListeners,
  targetUrl,
} from "../../../extension/lib/worker.js";
import { ApiError } from "../../../extension/ui/api.js";
import { stubChrome } from "./chrome-stub.js";

const SID = { domain: ".youtube.com", name: "SID", value: "v", path: "/", secure: true, httpOnly: true, hostOnly: false, session: false, expirationDate: 1790000000 };
const JOB = { id: "j1", state: "waiting", summary: "1080p MP4 + subtitles", spec: { title: "A video", url: "https://www.youtube.com/watch?v=x" } };

/** fetch stand-in recording calls; respond(url, init) gives the Response (or throws, for "offline"). */
function stubFetch(respond = () => Response.json(JOB, { status: 201 })) {
  const calls = [];
  const fetch = async (url, init = {}) => {
    calls.push({ url, method: init.method, body: init.body === undefined ? undefined : JSON.parse(init.body) });
    return respond(url, init);
  };
  return { fetch, calls };
}

const offline = () => {
  throw new TypeError("fetch failed");
};

/** setInterval/clearInterval stand-ins: tick() runs every live interval once. */
function stubTimers() {
  const live = new Map();
  let next = 1;
  return {
    live,
    setInterval: (callback, ms) => {
      const id = next++;
      live.set(id, { callback, ms });
      return id;
    },
    clearInterval: (id) => {
      live.delete(id);
    },
    tick() {
      for (const { callback } of [...live.values()]) callback();
    },
  };
}

/** console stand-in recording warnings. */
function stubConsole() {
  const warnings = [];
  return { warnings, warn: (...args) => warnings.push(args.map(String).join(" ")) };
}

function setUp({ respond, cookies = () => [SID], alarms } = {}) {
  const chrome = stubChrome({ cookies, alarms });
  const { fetch, calls } = stubFetch(respond);
  const timers = stubTimers();
  const log = stubConsole();
  const worker = createWorker({ chrome, api: createSnagApi({ fetch }), timers, log });
  return { chrome, worker, calls, timers, log };
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

// ----- pure parts -----

test("the server is Snag's fixed local address", () => {
  assert.equal(SNAG_BASE, "http://127.0.0.1:47823");
});

test("a right-click on a video downloads the page it's on, not the video's own address", () => {
  const info = { menuItemId: VIDEO_MENU_ID, mediaType: "video", srcUrl: "blob:https://www.youtube.com/1-2-3", pageUrl: "https://www.youtube.com/watch?v=x" };
  assert.equal(targetUrl(info, { url: "https://www.youtube.com/" }), "https://www.youtube.com/watch?v=x");
  assert.equal(targetUrl({ ...info, pageUrl: undefined }, { url: "https://www.youtube.com/watch?v=y" }), "https://www.youtube.com/watch?v=y");
  // A video inside a link still downloads the page for the video item (the link item has the link).
  assert.equal(targetUrl({ ...info, linkUrl: "https://a.example/v" }, {}), "https://www.youtube.com/watch?v=x");
});

test("a right-click on a video in an embedded player downloads the player's page, not the page around it", () => {
  const info = {
    menuItemId: VIDEO_MENU_ID, mediaType: "video", srcUrl: "blob:https://www.youtube.com/4-5-6",
    frameUrl: "https://www.youtube.com/embed/abc123", pageUrl: "https://blog.example/post",
  };
  assert.equal(targetUrl(info, { url: "https://blog.example/post" }), "https://www.youtube.com/embed/abc123");
  // A frame without a web address of its own (about:blank, about:srcdoc) can't be downloaded: the page.
  for (const frameUrl of ["about:blank", "about:srcdoc", ""]) {
    assert.equal(targetUrl({ ...info, frameUrl }, {}), "https://blog.example/post", frameUrl);
  }
  // The link and page item keep the link, else the page, even in a frame.
  assert.equal(targetUrl({ menuItemId: MENU_ID, frameUrl: "https://www.youtube.com/embed/abc123", pageUrl: "https://blog.example/post" }, {}), "https://blog.example/post");
});

test("the video menu item says it downloads the video", () => {
  assert.equal(MENU_TITLE, "Download with Snag");
  assert.equal(VIDEO_MENU_TITLE, "Download this video with Snag");
});

test("a right-click downloads the link when there is one, else the page", () => {
  assert.equal(targetUrl({ linkUrl: "https://a.example/v", pageUrl: "https://b.example/" }, { url: "https://c.example/" }), "https://a.example/v");
  assert.equal(targetUrl({ pageUrl: "https://b.example/" }, { url: "https://c.example/" }), "https://b.example/");
  assert.equal(targetUrl({}, { url: "https://c.example/" }), "https://c.example/");
  assert.equal(targetUrl({}, undefined), null);
});

test("error messages are the server's, or say Snag isn't running", () => {
  assert.equal(errorMessage(new ApiError("Snag isn't running", "offline")), OFFLINE_MESSAGE);
  assert.match(OFFLINE_MESSAGE, /^Snag isn't running\. It starts with Windows/);
  assert.equal(errorMessage(new ApiError("This video is private.", "unavailable")), "This video is private.");
  assert.equal(errorMessage(new Error("boom")), "Snag couldn't add that download.");
  assert.equal(errorMessage(undefined), "Snag couldn't add that download.");
});

test("notifications say Queued with the title, or the error", () => {
  const icon = "chrome-extension://id/icons/icon-128.png";
  assert.deepEqual(notificationFor({ ok: true, title: "A video", summary: "1080p MP4" }, icon), {
    type: "basic", iconUrl: icon, title: "Snag", message: "Queued: A video",
  });
  assert.equal(notificationFor({ ok: true, title: "", summary: "1080p MP4" }, icon).message, "Queued: 1080p MP4");
  assert.equal(notificationFor({ ok: false, message: OFFLINE_MESSAGE }, icon).message, OFFLINE_MESSAGE);
});

// ----- quick downloads -----

test("a quick download of an ordinary page sends no cookies and reports the queued job", async () => {
  const { chrome, worker, calls } = setUp();
  const outcome = await worker.quickDownload("https://www.youtube.com/watch?v=x");
  assert.deepEqual(outcome, { ok: true, jobId: "j1", title: "A video", summary: "1080p MP4 + subtitles" });
  assert.deepEqual(chrome.record.cookieQueries, [], "the cookies aren't even read");
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, `${SNAG_BASE}/api/jobs/quick`);
  assert.equal(calls[0].method, "POST");
  assert.deepEqual(calls[0].body, { url: "https://www.youtube.com/watch?v=x" });
});

const SIGN_IN_ANSWER = { error: { kind: "sign_in", message: "Sign in to YouTube in Chrome or Edge, then retry", action: "retry" } };

/** Snag's answer to /api/jobs/quick: sign-in required without cookies, the job with them. */
const needsSignIn = (url, init) => (JSON.parse(init.body).cookies
  ? Response.json(JOB, { status: 201 })
  : Response.json(SIGN_IN_ANSWER, { status: 422 }));

test("a quick download that needs sign-in is sent once more, with the site's cookies", async () => {
  const { chrome, worker, calls } = setUp({ respond: needsSignIn });
  const outcome = await worker.quickDownload("https://www.youtube.com/watch?v=x");
  assert.deepEqual(outcome, { ok: true, jobId: "j1", title: "A video", summary: "1080p MP4 + subtitles" });
  assert.deepEqual(chrome.record.cookieQueries, [{ url: "https://www.youtube.com/watch?v=x" }]);
  assert.deepEqual(calls.map((call) => call.body), [
    { url: "https://www.youtube.com/watch?v=x" },
    { url: "https://www.youtube.com/watch?v=x", cookies: [SID] },
  ]);
});

test("a quick download that needs sign-in on a site without cookies reports the sign-in error", async () => {
  const { worker, calls } = setUp({ respond: needsSignIn, cookies: () => [] });
  assert.deepEqual(await worker.quickDownload("https://vimeo.com/1"), {
    ok: false, kind: "sign_in", message: "Sign in to YouTube in Chrome or Edge, then retry",
  });
  assert.equal(calls.length, 1, "an empty list would clear what Snag keeps for the site");
});

test("the YouTube button's quick download sends cookies only after a sign-in error too", async () => {
  const { chrome, worker, calls } = setUp({ respond: needsSignIn });
  const sender = { id: chrome.runtime.id, tab: { id: 3 }, frameId: 0, url: "https://www.youtube.com/watch?v=x" };
  assert.equal((await worker.handleMessage({ type: "quick", url: sender.url }, sender)).ok, true);
  assert.deepEqual(calls.map((call) => Boolean(call.body.cookies)), [false, true]);
});

test("the menu sends cookies only after a sign-in error too", async () => {
  const { chrome, worker, calls } = setUp({ respond: needsSignIn });
  await worker.onContextMenuClicked({ menuItemId: MENU_ID, pageUrl: "https://www.youtube.com/watch?v=x" }, {});
  assert.deepEqual(calls.map((call) => Boolean(call.body.cookies)), [false, true]);
  assert.equal(chrome.record.notifications[0].message, "Queued: A video");
});

test("a quick download reports the server's error", async () => {
  const respond = () => Response.json({ error: { kind: "bad_request", message: "This link is a playlist. Choose the videos to download from it." } }, { status: 400 });
  const { worker } = setUp({ respond });
  assert.deepEqual(await worker.quickDownload("https://www.youtube.com/playlist?list=PL1"), {
    ok: false, kind: "bad_request", message: "This link is a playlist. Choose the videos to download from it.",
  });
});

test("a quick download says Snag isn't running when the server can't be reached", async () => {
  const { worker } = setUp({ respond: offline });
  assert.deepEqual(await worker.quickDownload("https://www.youtube.com/watch?v=x"), {
    ok: false, kind: "offline", message: OFFLINE_MESSAGE,
  });
});

test("pages that aren't on the web aren't sent to the server", async () => {
  const { worker, calls } = setUp();
  assert.deepEqual(await worker.quickDownload("chrome://extensions"), { ok: false, kind: "bad_request", message: NOT_WEB_MESSAGE });
  assert.deepEqual(await worker.quickDownload(undefined), { ok: false, kind: "bad_request", message: NOT_WEB_MESSAGE });
  assert.equal(calls.length, 0);
});

// ----- keeping the service worker alive while Snag answers -----

/** A respond() whose answer waits until release() is called. */
function heldResponse(answer = () => Response.json(JOB, { status: 201 })) {
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  return { respond: async (url, init) => { await gate; return answer(url, init); }, release: () => release() };
}

test("a pending request calls the extension API every 20 seconds, and stops when it ends", async () => {
  const held = heldResponse();
  const { chrome, worker, timers } = setUp({ respond: held.respond });
  const outcome = worker.quickDownload("https://www.youtube.com/watch?v=x");
  await flush();
  assert.equal(timers.live.size, 1);
  assert.equal([...timers.live.values()][0].ms, KEEPALIVE_MS);
  assert.equal(KEEPALIVE_MS, 20_000);
  timers.tick();
  timers.tick();
  await flush();
  assert.equal(chrome.record.platformInfoCalls, 2);
  held.release();
  assert.equal((await outcome).ok, true);
  assert.equal(timers.live.size, 0);
});

test("overlapping requests share one keep-alive, cleared after the last", async () => {
  const held = heldResponse((url) => (url.endsWith("/api/cookies") ? Response.json({ retried: 0 }) : Response.json(JOB, { status: 201 })));
  const { worker, timers } = setUp({ respond: held.respond });
  const quick = worker.quickDownload("https://www.youtube.com/watch?v=x");
  const another = worker.quickDownload("https://vimeo.com/1");
  const snapshot = worker.pushYouTubeCookies();
  await flush();
  assert.equal(timers.live.size, 1);
  held.release();
  await Promise.all([quick, another, snapshot]);
  assert.equal(timers.live.size, 0);
});

test("the keep-alive is cleared when the request fails, and a failing keep-alive call is harmless", async () => {
  const { chrome, worker, timers } = setUp({ respond: offline });
  chrome.runtime.getPlatformInfo = () => { throw new Error("no"); };
  const outcome = worker.quickDownload("https://www.youtube.com/watch?v=x");
  timers.tick();
  assert.equal((await outcome).kind, "offline");
  assert.equal(timers.live.size, 0);
  const unreadable = setUp({ cookies: () => new Error("no cookies today") });
  await unreadable.worker.pushYouTubeCookies();
  assert.equal(unreadable.timers.live.size, 0);
});

// ----- cookies -----

test("the YouTube snapshot sends every youtube.com cookie for www.youtube.com", async () => {
  const { chrome, worker, calls } = setUp({ respond: () => Response.json({ retried: 0 }) });
  assert.deepEqual(await worker.pushYouTubeCookies(), { ok: true, retried: 0 });
  assert.deepEqual(chrome.record.cookieQueries, [{ domain: "youtube.com" }]);
  assert.deepEqual(calls[0].body, { url: "https://www.youtube.com/", cookies: [SID] });
});

test("an empty snapshot is still sent: signing out of YouTube clears Snag's copy", async () => {
  const { worker, calls } = setUp({ cookies: () => [], respond: () => Response.json({ retried: 0 }) });
  await worker.pushYouTubeCookies();
  assert.deepEqual(calls[0].body, { url: "https://www.youtube.com/", cookies: [] });
});

test("snapshot failures are silent", async () => {
  const down = setUp({ respond: offline });
  assert.deepEqual(await down.worker.pushYouTubeCookies(), { ok: false, kind: "offline", message: OFFLINE_MESSAGE });
  const unreadable = setUp({ cookies: () => new Error("no cookies today") });
  assert.equal((await unreadable.worker.pushYouTubeCookies()).ok, false);
  assert.equal(unreadable.calls.length, 0, "a failed read must not be pushed as an empty list");
  assert.equal(down.chrome.record.notifications.length + unreadable.chrome.record.notifications.length, 0);
});

// ----- browser events -----

test("setting up makes the menu, the 30-minute alarm, and pushes the YouTube snapshot", async () => {
  const { chrome, worker, calls } = setUp({ respond: () => Response.json({ retried: 0 }) });
  await worker.setUp();
  assert.equal(chrome.record.removedAllMenus, 1);
  assert.deepEqual(chrome.record.menus, [{
    id: MENU_ID,
    title: "Download with Snag",
    contexts: ["link", "page"],
    documentUrlPatterns: ["http://*/*", "https://*/*"],
    targetUrlPatterns: ["http://*/*", "https://*/*"],
  }, {
    // Its own item: targetUrlPatterns would filter a video's src, which is often blob: (YouTube's is).
    id: VIDEO_MENU_ID,
    title: "Download this video with Snag",
    contexts: ["video"],
    documentUrlPatterns: ["http://*/*", "https://*/*"],
  }]);
  assert.deepEqual(chrome.record.alarmsCreated, [{ name: ALARM_NAME, periodInMinutes: ALARM_PERIOD_MINUTES, delayInMinutes: ALARM_PERIOD_MINUTES }]);
  assert.equal(ALARM_PERIOD_MINUTES, 30);
  assert.equal(calls[0].url, `${SNAG_BASE}/api/cookies`);
});

test("setting up again replaces the menu and keeps an alarm that's already set", async () => {
  const alarms = { [ALARM_NAME]: { name: ALARM_NAME, periodInMinutes: 30 } };
  const { chrome, worker } = setUp({ alarms, respond: () => Response.json({ retried: 0 }) });
  await worker.setUp();
  await worker.setUp();
  assert.equal(chrome.record.menus.length, 2);
  assert.deepEqual(chrome.record.alarmsCreated, []);
});

test("setting up while Snag is down still makes the menu and the alarm, and retries the snapshot in a minute", async () => {
  const { chrome, worker, log } = setUp({ respond: offline });
  await worker.setUp();
  assert.equal(chrome.record.menus.length, 2);
  assert.deepEqual(chrome.record.alarmsCreated, [
    { name: ALARM_NAME, periodInMinutes: ALARM_PERIOD_MINUTES, delayInMinutes: ALARM_PERIOD_MINUTES },
    { name: `${RETRY_ALARM_PREFIX}1`, delayInMinutes: RETRY_DELAY_MINUTES },
  ]);
  assert.equal(RETRY_DELAY_MINUTES, 1);
  assert.equal(chrome.record.notifications.length, 0);
  assert.deepEqual(log.warnings, [], "Snag not running isn't worth a warning");
});

test("the retry pushes again, and keeps retrying while Snag is down, up to MAX_RETRIES times", async () => {
  const { chrome, worker, calls } = setUp({ respond: offline });
  await worker.setUp();
  for (let attempt = 1; attempt <= MAX_RETRIES; attempt += 1) {
    const name = `${RETRY_ALARM_PREFIX}${attempt}`;
    assert.ok(chrome.existingAlarms.has(name), name);
    chrome.existingAlarms.delete(name); // a one-off alarm is gone once it fires
    await worker.onAlarm({ name });
  }
  assert.equal(MAX_RETRIES, 5);
  assert.equal(calls.length, 1 + MAX_RETRIES);
  const retries = chrome.record.alarmsCreated.filter((alarm) => alarm.name.startsWith(RETRY_ALARM_PREFIX));
  assert.deepEqual(retries.map((alarm) => alarm.name), [1, 2, 3, 4, 5].map((n) => `${RETRY_ALARM_PREFIX}${n}`));
  assert.ok(retries.every((alarm) => alarm.delayInMinutes === 1 && !("periodInMinutes" in alarm)));
  assert.equal([...chrome.existingAlarms.keys()].filter((name) => name.startsWith(RETRY_ALARM_PREFIX)).length, 0);
});

test("a retry that reaches Snag ends the retries", async () => {
  let up = false;
  const { chrome, worker } = setUp({ respond: () => (up ? Response.json({ retried: 0 }) : offline()) });
  await worker.setUp();
  up = true;
  chrome.existingAlarms.delete(`${RETRY_ALARM_PREFIX}1`);
  await worker.onAlarm({ name: `${RETRY_ALARM_PREFIX}1` });
  assert.equal(chrome.record.alarmsCreated.filter((alarm) => alarm.name.startsWith(RETRY_ALARM_PREFIX)).length, 1);
});

test("only Snag being unreachable is retried, not an error it answered", async () => {
  const respond = () => Response.json({ error: { kind: "internal", message: "Something went wrong." } }, { status: 500 });
  const { chrome, worker } = setUp({ respond });
  await worker.setUp();
  assert.equal(chrome.record.alarmsCreated.filter((alarm) => alarm.name.startsWith(RETRY_ALARM_PREFIX)).length, 0);
});

test("setting up again starts the retries over instead of running two series", async () => {
  const { chrome, worker } = setUp({ respond: offline });
  await worker.setUp();
  chrome.existingAlarms.delete(`${RETRY_ALARM_PREFIX}1`);
  await worker.onAlarm({ name: `${RETRY_ALARM_PREFIX}1` });
  assert.ok(chrome.existingAlarms.has(`${RETRY_ALARM_PREFIX}2`));
  await worker.setUp();
  const pending = [...chrome.existingAlarms.keys()].filter((name) => name.startsWith(RETRY_ALARM_PREFIX));
  assert.deepEqual(pending, [`${RETRY_ALARM_PREFIX}1`]);
});

test("a retry alarm whose name isn't a count in range is ignored", async () => {
  const { worker, calls } = setUp({ respond: offline });
  for (const suffix of ["x", "0", "-1", "1.5", String(MAX_RETRIES + 1), ""]) {
    await worker.onAlarm({ name: `${RETRY_ALARM_PREFIX}${suffix}` });
  }
  assert.equal(calls.length, 0);
});

test("a menu or alarm that can't be set up is a console warning, and the rest still happens", async () => {
  const { chrome, worker, calls, log } = setUp({ respond: () => Response.json({ retried: 0 }) });
  chrome.alarms.create = async () => { throw new Error("alarms broke"); };
  chrome.contextMenus.create = (properties, callback) => {
    chrome.runtime.lastError = { message: "menus broke" };
    callback?.();
    chrome.runtime.lastError = undefined;
  };
  await worker.setUp();
  assert.ok(log.warnings.length >= 2);
  assert.ok(log.warnings.some((line) => /menu/i.test(line) && /menus broke/.test(line)));
  assert.ok(log.warnings.some((line) => /alarm/i.test(line) && /alarms broke/.test(line)));
  assert.ok(log.warnings.every((line) => !line.includes(SID.value)));
  assert.equal(calls.length, 1, "the snapshot is still pushed");
});

test("a retry alarm that can't be set is a console warning", async () => {
  const { chrome, worker, log } = setUp({ respond: offline, alarms: { [ALARM_NAME]: { name: ALARM_NAME } } });
  chrome.alarms.create = async () => { throw new Error("alarms broke"); };
  await worker.setUp();
  assert.equal(log.warnings.length, 1);
  assert.match(log.warnings[0], /retry/i);
});

test("a menu that is already there (two set-ups at once) isn't a warning", async () => {
  const { chrome, worker, log } = setUp({ respond: () => Response.json({ retried: 0 }) });
  chrome.contextMenus.create = (properties, callback) => {
    chrome.runtime.lastError = { message: `Cannot create item with duplicate id ${properties.id}` };
    callback?.();
    chrome.runtime.lastError = undefined;
  };
  await worker.setUp();
  assert.deepEqual(log.warnings, []);
});

test("every start of the worker makes sure the alarm exists, keeping one already set", async () => {
  const fresh = setUp();
  await fresh.worker.onWorkerStart();
  assert.deepEqual(fresh.chrome.record.alarmsCreated.map((alarm) => alarm.name), [ALARM_NAME]);
  assert.equal(fresh.calls.length, 0, "a start isn't a snapshot");

  const kept = setUp({ alarms: { [ALARM_NAME]: { name: ALARM_NAME, periodInMinutes: 30, scheduledTime: 5 } } });
  await kept.worker.onWorkerStart();
  assert.deepEqual(kept.chrome.record.alarmsCreated, []);

  const broken = setUp();
  broken.chrome.alarms.get = async () => { throw new Error("alarms broke"); };
  await broken.worker.onWorkerStart();
  assert.equal(broken.log.warnings.length, 1);
  assert.match(broken.log.warnings[0], /alarms broke/);
});

test("the menu queues the link and notifies", async () => {
  const { chrome, worker, calls } = setUp();
  await worker.onContextMenuClicked({ menuItemId: MENU_ID, linkUrl: "https://www.youtube.com/watch?v=x", pageUrl: "https://example.com/" }, { url: "https://example.com/" });
  assert.equal(calls[0].body.url, "https://www.youtube.com/watch?v=x");
  assert.deepEqual(chrome.record.notifications, [{
    type: "basic", iconUrl: chrome.runtime.getURL("icons/icon-128.png"), title: "Snag", message: "Queued: A video",
  }]);
});

test("the video menu queues the page the video is on", async () => {
  const { chrome, worker, calls } = setUp();
  await worker.onContextMenuClicked({ menuItemId: VIDEO_MENU_ID, mediaType: "video", srcUrl: "blob:https://www.youtube.com/1-2", pageUrl: "https://www.youtube.com/watch?v=x" }, { url: "https://www.youtube.com/watch?v=x" });
  assert.equal(calls[0].body.url, "https://www.youtube.com/watch?v=x");
  assert.equal(chrome.record.notifications[0].message, "Queued: A video");
});

test("the video menu on an embedded player queues the player's page", async () => {
  const { worker, calls } = setUp();
  await worker.onContextMenuClicked({
    menuItemId: VIDEO_MENU_ID, mediaType: "video", srcUrl: "blob:https://www.youtube.com/7-8",
    frameUrl: "https://www.youtube.com/embed/abc123", pageUrl: "https://blog.example/post",
  }, { url: "https://blog.example/post" });
  assert.equal(calls[0].body.url, "https://www.youtube.com/embed/abc123");
});

test("the menu notifies Snag isn't running", async () => {
  const { chrome, worker } = setUp({ respond: offline });
  await worker.onContextMenuClicked({ menuItemId: MENU_ID, pageUrl: "https://www.youtube.com/watch?v=x" }, {});
  assert.equal(chrome.record.notifications[0].message, OFFLINE_MESSAGE);
});

test("other menu items and alarms are ignored", async () => {
  const { chrome, worker, calls } = setUp();
  await worker.onContextMenuClicked({ menuItemId: "someone-else", pageUrl: "https://a.example/" }, {});
  await worker.onAlarm({ name: "another-alarm" });
  assert.equal(calls.length, 0);
  assert.equal(chrome.record.notifications.length, 0);
});

test("the alarm pushes the YouTube snapshot", async () => {
  const { worker, calls } = setUp({ respond: () => Response.json({ retried: 0 }) });
  await worker.onAlarm({ name: ALARM_NAME });
  assert.deepEqual(calls[0].body.url, "https://www.youtube.com/");
});

const WATCH_URL = "https://www.youtube.com/watch?v=x";

/** The sender Chrome reports for the YouTube content script in a tab's top frame. */
function youtubeSender(chrome, fields = {}) {
  return { id: chrome.runtime.id, tab: { id: 3, url: WATCH_URL }, frameId: 0, url: WATCH_URL, origin: "https://www.youtube.com", ...fields };
}

/** The sender Chrome reports for one of the extension's own pages (the popup). */
function pageSender(chrome, fields = {}) {
  return { id: chrome.runtime.id, url: chrome.runtime.getURL("popup/popup.html"), origin: `chrome-extension://${chrome.runtime.id}`, ...fields };
}

const answering = (url) => (url.endsWith("/api/cookies") ? Response.json({ retried: 1 }) : Response.json(JOB, { status: 201 }));

test("messages: quick from the YouTube button", async () => {
  const { chrome, worker, calls } = setUp({ respond: answering });
  assert.deepEqual(await worker.handleMessage({ type: "quick", url: WATCH_URL }, youtubeSender(chrome)), {
    ok: true, jobId: "j1", title: "A video", summary: "1080p MP4 + subtitles",
  });
  assert.deepEqual(calls[0].body.url, WATCH_URL);
  assert.equal(calls.length, 1);
});

test("quick is taken only from the YouTube page's top frame, for the page it's on", async () => {
  const { chrome, worker, calls } = setUp({ respond: answering });
  const quick = { type: "quick", url: WATCH_URL };
  // Not a content script: the popup, or a page of the extension, has no tab.
  assert.equal(worker.handleMessage(quick, pageSender(chrome)), null);
  // A frame inside the page, even one on youtube.com.
  assert.equal(worker.handleMessage(quick, youtubeSender(chrome, { frameId: 2 })), null);
  assert.equal(worker.handleMessage(quick, youtubeSender(chrome, { frameId: undefined })), null);
  // Another site's page, or another YouTube host.
  for (const url of ["https://evil.example/watch?v=x", "https://music.youtube.com/watch?v=x", "http://www.youtube.com/watch?v=x", "https://www.youtube.com.evil.example/watch?v=x"]) {
    assert.equal(worker.handleMessage({ type: "quick", url }, youtubeSender(chrome, { url })), null, url);
  }
  assert.equal(worker.handleMessage(quick, youtubeSender(chrome, { url: undefined })), null);
  assert.equal(worker.handleMessage(quick, youtubeSender(chrome, { url: "not a url" })), null);
  assert.equal(calls.length, 0);
});

test("quick for another address than the page sending it is refused without asking the server", async () => {
  const { chrome, worker, calls } = setUp({ respond: answering });
  for (const url of ["https://www.youtube.com/watch?v=other", "https://evil.example/", undefined]) {
    const answer = await worker.handleMessage({ type: "quick", url }, youtubeSender(chrome));
    assert.equal(answer.ok, false, String(url));
    assert.equal(answer.kind, "bad_request");
    assert.equal(answer.message, PAGE_CHANGED_MESSAGE);
  }
  assert.equal(calls.length, 0);
});

test("unknown messages, and messages from elsewhere, aren't answered", () => {
  const { chrome, worker } = setUp();
  assert.equal(worker.handleMessage({ type: "other" }, pageSender(chrome)), null);
  // Nothing pushes a site's cookies through the worker: the popup asks Snag itself.
  assert.equal(worker.handleMessage({ type: "pushCookies", url: "https://vimeo.com/1" }, pageSender(chrome)), null);
  assert.equal(worker.handleMessage({ type: "other" }, youtubeSender(chrome)), null);
  assert.equal(worker.handleMessage(null, pageSender(chrome)), null);
  assert.equal(worker.handleMessage("quick", youtubeSender(chrome)), null);
  assert.equal(worker.handleMessage({ type: "quick", url: WATCH_URL }, youtubeSender(chrome, { id: "another-extension" })), null);
  assert.equal(worker.handleMessage({ type: "quick", url: WATCH_URL }, undefined), null);
});

test("registered listeners: install and startup set up, clicks, alarms and messages are handled", async () => {
  const { chrome, worker, calls } = setUp({ respond: (url) => (url.endsWith("/api/cookies") ? Response.json({ retried: 0 }) : Response.json(JOB, { status: 201 })) });
  registerListeners(chrome, worker);

  chrome.runtime.onInstalled.fire({ reason: "install" });
  await flush();
  assert.equal(chrome.record.menus.length, 2);
  chrome.runtime.onStartup.fire();
  await flush();
  assert.equal(chrome.record.removedAllMenus, 2);

  chrome.contextMenus.onClicked.fire({ menuItemId: MENU_ID, pageUrl: "https://www.youtube.com/watch?v=x" }, {});
  await flush();
  await flush();
  assert.equal(chrome.record.notifications.at(-1).message, "Queued: A video");

  chrome.alarms.onAlarm.fire({ name: ALARM_NAME });
  await flush();

  let reply;
  const [keepOpen] = chrome.runtime.onMessage.fire({ type: "quick", url: WATCH_URL }, youtubeSender(chrome), (response) => { reply = response; });
  assert.equal(keepOpen, true, "the listener must return true to answer asynchronously");
  await flush();
  await flush();
  assert.equal(reply.ok, true);
  const [ignored] = chrome.runtime.onMessage.fire({ type: "nope" }, pageSender(chrome), () => {});
  assert.equal(ignored, false);
  const [refused] = chrome.runtime.onMessage.fire({ type: "pushCookies", url: "https://vimeo.com/1" }, youtubeSender(chrome), () => {});
  assert.equal(refused, false);
  assert.ok(calls.some((call) => call.url.endsWith("/api/cookies")));
});
