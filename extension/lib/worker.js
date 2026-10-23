// What the background service worker does (background.js only wires it to the browser), written
// against an injected `chrome` and API client so it runs in Node's tests:
// - "Download with Snag" on links, pages and videos: the quick-download default, sent without cookies
//   and once more with the site's only when Snag answers that sign-in is required (spec 3.5), then a
//   notification "Queued: <title>" or the plain-English error. A right-clicked video
//   ("Download this video with Snag") downloads the page it is on: an embedded player's own page when
//   it is in one, else the page in the tab.
// - The YouTube cookie snapshot (spec 3.5): pushed when the browser starts, when the extension is
//   installed or updated, and every 30 minutes; failures are silent (Snag may simply not be running).
//   When Snag can't be reached for the startup push (it may still be starting with Windows), it is
//   tried again a minute later, up to 5 times, with one-off alarms whose names carry the attempt
//   (the worker forgets everything when the browser stops it, and alarms survive that).
// - Every start of the worker checks that the 30-minute alarm exists. A menu or alarm that can't be set
//   up is a console warning.
// - Messages: {type: "quick", url} from the YouTube button, taken only from where it belongs (see
//   handleMessage); anything else is left unanswered. The popup sends nothing here: it asks Snag
//   itself, with the page's cookies when sign-in is required (popup/app.js).
// - While a request to Snag is pending, an extension API call every 20 seconds keeps the browser from
//   stopping the idle service worker mid-request (calling an extension API resets its idle timer).

import { OFFLINE } from "../ui/fields.js";
import { YOUTUBE_URL, askWithCookiesIfNeeded, collectYouTubeCookies, isWebUrl } from "./cookies.js";

export const MENU_ID = "snag-download";
export const VIDEO_MENU_ID = "snag-download-video";
export const MENU_TITLE = "Download with Snag";
export const VIDEO_MENU_TITLE = "Download this video with Snag";
export const ALARM_NAME = "snag-youtube-cookies";
export const ALARM_PERIOD_MINUTES = 30;
export const RETRY_ALARM_PREFIX = "snag-youtube-cookies-retry-";
export const RETRY_DELAY_MINUTES = 1;
export const MAX_RETRIES = 5;
export const NOTIFICATION_ICON = "icons/icon-128.png";
export const KEEPALIVE_MS = 20000; // under the 30 seconds of inactivity after which Chrome stops the worker
export const OFFLINE_MESSAGE = "Snag isn't running. It starts with Windows, and you can also start it from the Start menu.";
export const NOT_WEB_MESSAGE = "Snag downloads from web addresses starting with http:// or https://.";
export const PAGE_CHANGED_MESSAGE = "This page changed while Snag was being asked. Try again.";
export const YOUTUBE_ORIGIN = "https://www.youtube.com";
const FALLBACK_MESSAGE = "Snag couldn't add that download.";
const WEB_PATTERNS = ["http://*/*", "https://*/*"];

/** The address a context-menu click is about: for a video, the page it's on (its own src is often a
 * blob: address only that page can use): the frame's page when the video is in a frame with a web
 * address of its own (an embedded player, such as youtube.com/embed/<id>), else the tab's page;
 * otherwise the link right-clicked, else the page. */
export function targetUrl(info, tab) {
  if (info?.menuItemId === VIDEO_MENU_ID) {
    if (isWebUrl(info?.frameUrl)) return info.frameUrl;
    return info?.pageUrl || tab?.url || null;
  }
  return info?.linkUrl || info?.pageUrl || tab?.url || null;
}

/** What to tell the user about a failed request. */
export function errorMessage(error) {
  if (error?.kind === OFFLINE) return OFFLINE_MESSAGE;
  if (error?.name === "ApiError" && typeof error.message === "string" && error.message) return error.message;
  return FALLBACK_MESSAGE;
}

function failed(error) {
  return { ok: false, kind: error?.kind ?? "unknown", message: errorMessage(error) };
}

const notWeb = () => ({ ok: false, kind: "bad_request", message: NOT_WEB_MESSAGE });
const pageChanged = () => ({ ok: false, kind: "bad_request", message: PAGE_CHANGED_MESSAGE });

function originOf(url) {
  try {
    return new URL(url).origin;
  } catch {
    return null;
  }
}

/** True for the YouTube content script in a tab's top frame on https://www.youtube.com: the only
 * sender that may ask for a one-click download, and only of the page it is on (sender.url, which the
 * browser reports, not the page). */
export function isYouTubeTopFrame(sender) {
  return Boolean(sender?.tab) && sender.frameId === 0 && typeof sender.url === "string"
    && originOf(sender.url) === YOUTUBE_ORIGIN;
}

/** The chrome.notifications options for a quick download's outcome. */
export function notificationFor(outcome, iconUrl) {
  const message = outcome.ok ? `Queued: ${outcome.title || outcome.summary || "your download"}` : outcome.message;
  return { type: "basic", iconUrl, title: "Snag", message };
}

/** Runs tasks while keeping the service worker awake: one interval for however many are pending,
 * cleared when the last ends, however it ends. */
function createKeepAlive(runtime, timers) {
  let pending = 0;
  let interval = null;
  const ping = () => {
    Promise.resolve().then(() => runtime.getPlatformInfo()).catch(() => {});
  };
  return async function whileAwake(task) {
    pending += 1;
    if (interval === null) interval = timers.setInterval(ping, KEEPALIVE_MS);
    try {
      return await task();
    } finally {
      pending -= 1;
      if (pending === 0) {
        timers.clearInterval(interval);
        interval = null;
      }
    }
  };
}

/** The attempt a retry alarm is for (1..MAX_RETRIES), or null for any other alarm. */
export function retryAttempt(name) {
  if (typeof name !== "string" || !name.startsWith(RETRY_ALARM_PREFIX)) return null;
  const digits = name.slice(RETRY_ALARM_PREFIX.length);
  if (!/^[0-9]+$/.test(digits)) return null;
  const attempt = Number(digits);
  return attempt >= 1 && attempt <= MAX_RETRIES ? attempt : null;
}

const reason = (error) => String(error?.message ?? error);

/** chrome: the extension API (or a stand-in); api: ui/api.js's client bound to Snag's server;
 * timers: {setInterval, clearInterval} and log: {warn} (injectable for tests). */
export function createWorker({ chrome, api, timers = globalThis, log = globalThis.console }) {
  const whileAwake = createKeepAlive(chrome.runtime, {
    setInterval: (callback, ms) => timers.setInterval(callback, ms),
    clearInterval: (id) => timers.clearInterval(id),
  });

  async function quick(url) {
    if (!isWebUrl(url)) return notWeb();
    try {
      const { answer: job } = await askWithCookiesIfNeeded(
        url, (cookies) => api.quickJob(url, cookies ? { cookies } : {}), chrome.cookies);
      return { ok: true, jobId: job?.id ?? null, title: job?.spec?.title ?? "", summary: job?.summary ?? "" };
    } catch (error) {
      return failed(error);
    }
  }

  async function send(url, cookies) {
    try {
      const answer = await api.sendCookies(url, cookies);
      return { ok: true, retried: answer?.retried ?? 0 };
    } catch (error) {
      return failed(error);
    }
  }

  /** The quick-download default for url, with its site's cookies only if sign-in is required. */
  function quickDownload(url) {
    return whileAwake(() => quick(url));
  }

  /** Every youtube.com cookie to the server; never throws. */
  function pushYouTubeCookies() {
    return whileAwake(youTubeSnapshot);
  }

  async function youTubeSnapshot() {
    let cookies;
    try {
      cookies = await collectYouTubeCookies(chrome.cookies);
    } catch (error) {
      return { ok: false, kind: "cookies", message: String(error?.message ?? error) };
    }
    return send(YOUTUBE_URL, cookies);
  }

  /** Adds a menu item; rejects with the browser's error, except for an id that's already there (two
   * set-ups at once: the item exists, which is what was wanted). */
  function createMenuItem(properties) {
    return new Promise((resolve, reject) => {
      chrome.contextMenus.create(properties, () => {
        const error = chrome.runtime.lastError; // read, so it isn't logged as unchecked
        if (error && !/duplicate id/i.test(error.message ?? "")) reject(new Error(error.message ?? String(error)));
        else resolve();
      });
    });
  }

  async function createMenu() {
    await chrome.contextMenus.removeAll();
    await createMenuItem({
      id: MENU_ID,
      title: MENU_TITLE,
      contexts: ["link", "page"],
      documentUrlPatterns: WEB_PATTERNS,
      targetUrlPatterns: WEB_PATTERNS,
    });
    // Videos get their own item: targetUrlPatterns filters a video's src, which is often blob: (it is on
    // YouTube), so the link item's patterns would hide it there. Right-clicking a video shows only
    // the "video" context, not "page"; only a video inside a link shows both items.
    await createMenuItem({
      id: VIDEO_MENU_ID,
      title: VIDEO_MENU_TITLE,
      contexts: ["video"],
      documentUrlPatterns: WEB_PATTERNS,
    });
  }

  async function ensureAlarm() {
    // An existing alarm keeps its schedule; browsers may drop alarms over a restart, so check each time.
    if (await chrome.alarms.get(ALARM_NAME)) return;
    await chrome.alarms.create(ALARM_NAME, { periodInMinutes: ALARM_PERIOD_MINUTES, delayInMinutes: ALARM_PERIOD_MINUTES });
  }

  /** A one-off alarm for snapshot retry `attempt`, replacing any retry still pending (so a second
   * set-up starts the series over rather than running two). */
  async function scheduleRetry(attempt) {
    try {
      for (const alarm of await chrome.alarms.getAll()) {
        if (alarm?.name?.startsWith(RETRY_ALARM_PREFIX)) await chrome.alarms.clear(alarm.name);
      }
      await chrome.alarms.create(`${RETRY_ALARM_PREFIX}${attempt}`, { delayInMinutes: RETRY_DELAY_MINUTES });
    } catch (error) {
      log.warn(`Snag: couldn't set the alarm to retry sending the YouTube login: ${reason(error)}`);
    }
  }

  /** Pushes the snapshot; when Snag couldn't be reached, schedules the next attempt if any are left. */
  async function pushOrRetry(attempt) {
    const outcome = await pushYouTubeCookies();
    if (outcome.kind === OFFLINE && attempt <= MAX_RETRIES) await scheduleRetry(attempt);
    return outcome;
  }

  /** On install, update and browser startup. */
  async function setUp() {
    const [menu, alarm] = await Promise.allSettled([createMenu(), ensureAlarm()]);
    if (menu.status === "rejected") log.warn(`Snag: couldn't add the "${MENU_TITLE}" menu: ${reason(menu.reason)}`);
    if (alarm.status === "rejected") log.warn(`Snag: couldn't set the YouTube login alarm: ${reason(alarm.reason)}`);
    await pushOrRetry(1);
  }

  /** On every start of the service worker (it is stopped when idle and started for each event). */
  async function onWorkerStart() {
    try {
      await ensureAlarm();
    } catch (error) {
      log.warn(`Snag: couldn't set the YouTube login alarm: ${reason(error)}`);
    }
  }

  async function notify(outcome) {
    try {
      await chrome.notifications.create(notificationFor(outcome, chrome.runtime.getURL(NOTIFICATION_ICON)));
    } catch {
      // Notifications turned off for the browser: nothing else to tell the user with.
    }
  }

  async function onContextMenuClicked(info, tab) {
    if (info?.menuItemId !== MENU_ID && info?.menuItemId !== VIDEO_MENU_ID) return;
    await notify(await quickDownload(targetUrl(info, tab)));
  }

  async function onAlarm(alarm) {
    if (alarm?.name === ALARM_NAME) {
      await pushYouTubeCookies();
      return;
    }
    const attempt = retryAttempt(alarm?.name);
    if (attempt !== null) await pushOrRetry(attempt + 1);
  }

  /** A promise of the answer to a message, or null for one that isn't answered (left for other
   * listeners). Only this extension's senders count, and each message only from where it belongs:
   * - quick: the YouTube content script in a top frame on https://www.youtube.com; it downloads the
   *   page the browser says sent it, and message.url must be that same page (else "page changed").
   * Never rejects. */
  function handleMessage(message, sender) {
    if (sender?.id !== chrome.runtime.id || message === null || typeof message !== "object") return null;
    if (message.type === "quick" && isYouTubeTopFrame(sender)) {
      return message.url === sender.url ? quickDownload(sender.url) : Promise.resolve(pageChanged());
    }
    return null;
  }

  return {
    setUp, onWorkerStart, quickDownload, pushYouTubeCookies, onContextMenuClicked, onAlarm, handleMessage,
  };
}

/** Wires the worker to the browser's events. Called once, synchronously, when the service worker
 * starts, so events that woke it are delivered. */
export function registerListeners(chrome, worker) {
  chrome.runtime.onInstalled.addListener(() => void worker.setUp());
  chrome.runtime.onStartup.addListener(() => void worker.setUp());
  chrome.contextMenus.onClicked.addListener((info, tab) => void worker.onContextMenuClicked(info, tab));
  chrome.alarms.onAlarm.addListener((alarm) => void worker.onAlarm(alarm));
  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    const answer = worker.handleMessage(message, sender);
    if (answer === null) return false;
    answer.then(sendResponse);
    return true; // answers asynchronously
  });
}
