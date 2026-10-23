// The popup's pure rules (spec 4.10): which view shows for what is known so far, the notice above it,
// the links into the full tab, the paste box, the playlist card and the slim queue line. No DOM, no
// browser APIs: popup/app.js draws what these return.

import { isWebUrl } from "../lib/cookies.js";
import { SNAG_APP_URL } from "../lib/server.js";
import { settingsAttention, setupScreen } from "../ui/components/setup-model.js";
import { combinedProgress, queueCounts, queueSummaryText } from "../ui/components/queue-model.js";
import { jobList } from "../ui/store.js";

export const VIEW = Object.freeze({
  LOADING: "loading",
  VIDEO: "video",
  PLAYLIST: "playlist",
  NOTHING: "nothing",
  OFFLINE: "offline",
  SETUP: "setup",
  ERROR: "error",
});

export const OFFLINE_TEXT = "Snag isn't running. It starts with Windows, and you can also start it from the Start menu.";
export const NOTHING_TEXT = "Nothing to download on this page";
export const SNAG_PAGE_TEXT = "This is Snag";
export const UNSUPPORTED_LINK_TEXT = "Snag can't download from that link";
const LOOKUP_FAILED = "Snag couldn't look this page up";

// The addresses Snag answers on, and its full tab at either; tabs.query({url}) matches these through
// the extension's host permissions (no "tabs" permission needed).
const SNAG_ORIGINS = Object.freeze(["http://127.0.0.1:47823", "http://localhost:47823"]);
export const APP_TAB_PATTERNS = Object.freeze(SNAG_ORIGINS.map((origin) => `${origin}/*`));

// The full tab's routes that carry a link (web/router.js parseHash, which a test keeps this in step with).
const LINK_ROUTES = new Set(["download", "playlist"]);
const URL_PARAM = /(?:^|&)url=/;

/** The link Snag's full tab shows for its hash ("#download?url=…", "#playlist?url=…"), read as
 * web/router.js parseHash reads it, or null. */
function snagTabLink(hash) {
  const text = hash.replace(/^#/, "");
  const mark = text.indexOf("?");
  const route = (mark < 0 ? text : text.slice(0, mark)).replace(/\/+$/, "").toLowerCase();
  if (mark < 0 || !LINK_ROUTES.has(route)) return null;
  const query = text.slice(mark + 1);
  const match = URL_PARAM.exec(query);
  if (!match) return null;
  const raw = query.slice(match.index + match[0].length);
  try {
    return decodeURIComponent(raw).trim() || null;
  } catch {
    return raw.trim() || null;
  }
}

/** What to look up for the active tab: {target, snag}. target is its address when it is a web page
 * (http or https), else null (chrome://, edge://, file://, the new tab page, or a tab whose address the
 * popup can't see). Snag's own full tab is never looked up: target is the web link it shows
 * (#download?url=…, #playlist?url=…) if any, and snag is true when it shows none. */
export function pageTarget(tabUrl) {
  if (!isWebUrl(tabUrl)) return { target: null, snag: false };
  const address = new URL(tabUrl);
  if (!SNAG_ORIGINS.includes(address.origin)) return { target: tabUrl, snag: false };
  const link = snagTabLink(address.hash);
  return isWebUrl(link) ? { target: link, snag: false } : { target: null, snag: true };
}

/** A link into the full tab: the app itself, or a route with a link in it ("#playlist?url=…"). The
 * same rule as web/router.js buildHash, which reads it back exactly (a test keeps the two in step). */
export function appLink(route = null, url = null) {
  if (!route) return SNAG_APP_URL;
  return url ? `${SNAG_APP_URL}#${route}?url=${encodeURIComponent(url)}` : `${SNAG_APP_URL}#${route}`;
}

/** The view for what is known so far:
 * reading: the tab's address isn't known yet;
 * target: the address being looked up (the tab's, or one pasted), or null when there is none;
 * pasted: target came from the paste box; snag: the tab is Snag's own page (pageTarget);
 * offline: Snag didn't answer; status: GET /api/status (or the hub's), undefined/null while unknown;
 * lookup: null while under way, {info} (POST /api/lookup's answer) or {error} (its ApiError).
 * → {view, ...what that view shows}; the nothing view's pasteError is the paste box's field error, and
 * the error view's canRetry says whether to offer Try again (lookupCanRetry). */
export function selectView({
  reading = false, target = null, pasted = false, snag = false, offline = false, status = null, lookup = null,
} = {}) {
  if (offline) return { view: VIEW.OFFLINE, text: OFFLINE_TEXT };
  const setup = status ? setupScreen(status, null) : null;
  if (setup) {
    return { view: VIEW.SETUP, title: setup.title, message: setup.message, failed: setup.state === "error", canRetry: setup.canRetry };
  }
  if (reading) return { view: VIEW.LOADING };
  const nothing = (pasteError = null) => ({ view: VIEW.NOTHING, text: snag ? SNAG_PAGE_TEXT : NOTHING_TEXT, pasteError });
  if (!target) return nothing();
  if (!lookup) return { view: VIEW.LOADING };
  if (lookup.error) {
    if (lookup.error.kind === "unsupported") return nothing(pasted ? UNSUPPORTED_LINK_TEXT : null);
    return { view: VIEW.ERROR, text: lookup.error.message || LOOKUP_FAILED, canRetry: lookupCanRetry(lookup.error) };
  }
  const info = lookup.info;
  if (info?.type === "playlist") {
    return {
      view: VIEW.PLAYLIST, info, summary: playlistSummary(info), chooseLink: appLink("playlist", info.url),
      singleVideo: singleVideoUrl(target, info),
    };
  }
  return { view: VIEW.VIDEO, info, playlistLink: info?.playlist?.url ? appLink("playlist", info.playlist.url) : null };
}

/** Whether asking again could help after a failed lookup: Snag's own verdict (its ErrorInfo's
 * can_retry, false for a video that needs payment or was removed) when it gave one, else true (a
 * timeout, or an answer that wasn't Snag's error JSON). */
function lookupCanRetry(error) {
  const verdict = error?.error?.can_retry;
  return typeof verdict === "boolean" ? verdict : true;
}

/** The small notice above the view: a yt-dlp update, or a tool Snag needs that is missing (the same
 * as the dot on the full tab's Settings tab), or null. */
export function noticeFor(status) {
  const attention = settingsAttention(status);
  if (!attention) return null;
  return { text: attention.label, tone: attention.tone, action: attention.tone === "accent" ? "Update in Snag" : "Fix in Snag" };
}

// --- the paste box: the same rule as the full tab's (web/download-model.js lookupTarget) ---

const WITH_SCHEME = /[a-z][a-z0-9+.-]*:\/\/[^\s"'<>]+/i;
const BARE_HOST = /^(?:[a-z0-9-]+\.)+[a-z]{2,}(?:[/?#][^\s"'<>]*)?$/i;
const NOT_A_LINK = "That isn't a link. Paste the address of a video or playlist.";

/** {url} to look up from the box's text: the first link in it (a bare "youtube.com/…" gets https://);
 * {url: null, error: null} when it's empty; {url: null, error} when it holds no link. */
export function pastedTarget(text) {
  const value = typeof text === "string" ? text.trim() : "";
  if (!value) return { url: null, error: null };
  const linked = WITH_SCHEME.exec(value);
  if (linked) return { url: linked[0] };
  const bare = value.split(/\s+/).find((word) => BARE_HOST.test(word));
  if (bare) return { url: `https://${bare}` };
  return { url: null, error: NOT_A_LINK };
}

// --- the playlist card: the full tab's summary (web/download-model.js playlistSummary) ---

const plural = (count, one, many = `${one}s`) => `${count} ${count === 1 ? one : many}`;

/** {title, text: "18 videos · 3 downloaded before"}. */
export function playlistSummary(info) {
  const entries = Array.isArray(info?.entries) ? info.entries : [];
  const before = entries.filter((entry) => entry?.duplicate).length;
  const parts = [entries.length ? plural(entries.length, "video") : "No videos"];
  if (before) parts.push(`${before} downloaded before`);
  return { title: info?.title || "Playlist", text: parts.join(" · ") };
}

// --- Just this video ---

const YOUTUBE_HOSTS = new Set(["youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"]);
const VIDEO_ID = /^[A-Za-z0-9_-]{1,64}$/;

/** For a playlist looked up from an address that also names one YouTube video (watch?v=…&list=…,
 * youtu.be/<id>?list=…): that video's own address, so "Just this video" can look it up; else null. */
export function singleVideoUrl(target, info) {
  if (info?.type !== "playlist" || !isWebUrl(target)) return null;
  const address = new URL(target);
  const host = address.hostname.toLowerCase();
  let id = null;
  if (YOUTUBE_HOSTS.has(host) && address.pathname === "/watch") id = address.searchParams.get("v");
  else if (host === "youtu.be") id = address.pathname.slice(1);
  return id && VIDEO_ID.test(id) ? `https://www.youtube.com/watch?v=${id}` : null;
}

// --- the slim queue line ---

/** {hidden, text: "2 downloading · 1 waiting", progress: 0-100 | null, paused} from the store's state. */
export function queueLine(state) {
  const jobs = jobList(state);
  const counts = queueCounts(jobs);
  return {
    hidden: counts.total === 0,
    text: queueSummaryText(counts),
    progress: combinedProgress(jobs),
    paused: Boolean(state.queue?.paused),
  };
}
