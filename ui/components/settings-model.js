// Pure rules behind the Settings tab: the max-quality choices, the downloads-at-once range, which fields
// changed (each field saves on its own with PUT /api/settings), the YouTube login line, and the tools'
// rows (versions, update and reinstall progress and results). No DOM. Settings is GET /api/settings;
// status is the store's status; updates is the store's {tool: Progress} from update events.

import { formatAgo, toDate } from "../format.js";
import { toolProgress } from "./setup-model.js";

// snag.store.settings ALLOWED_HEIGHTS, best first; null is no limit.
export const HEIGHT_OPTIONS = Object.freeze([
  { value: null, label: "Best available" },
  ...[2160, 1440, 1080, 720, 480, 360].map((height) => ({ value: height, label: `Up to ${height}p` })),
]);

export const MIN_CONCURRENT = 1;
export const MAX_CONCURRENT = 10;
const DEFAULT_CONCURRENT = 5;

/** A <select> value for a height ("" for the best available). */
export function optionOfHeight(height) {
  return height === null || height === undefined ? "" : String(height);
}

/** The height a <select> value stands for; anything not offered is the best available (null). */
export function heightFromOption(value) {
  const height = Number(value);
  return HEIGHT_OPTIONS.some((option) => option.value === height) ? height : null;
}

export function clampConcurrent(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return DEFAULT_CONCURRENT;
  return Math.min(MAX_CONCURRENT, Math.max(MIN_CONCURRENT, Math.round(number)));
}

function same(a, b) {
  if (a === b) return true;
  if (Array.isArray(a) || Array.isArray(b)) {
    return Array.isArray(a) && Array.isArray(b) && a.length === b.length && a.every((item, i) => same(item, b[i]));
  }
  if (a && b && typeof a === "object" && typeof b === "object") {
    const keys = new Set([...Object.keys(a), ...Object.keys(b)]);
    return [...keys].every((key) => same(a[key], b[key]));
  }
  return false;
}

/** The fields of draft whose values differ from the saved settings: the PUT /api/settings body. */
export function changedFields(saved, draft) {
  const changes = {};
  for (const [key, value] of Object.entries(draft ?? {})) {
    if (!same(saved?.[key], value)) changes[key] = value;
  }
  return changes;
}

/** What a field's save says: "Saved" when the server kept the value sent (asSent), otherwise who
 * changed it (Start with Windows reports what Windows will do, so Windows refused it; any other field
 * Snag tidied or adjusted). */
export function savedText(field, asSent) {
  if (asSent) return "Saved";
  return field === "start_with_windows" ? "Windows didn't keep this" : "Snag adjusted this";
}

/** The message under a field the server refused to save: its own words. */
export function saveErrorText(error) {
  return error?.message || "Couldn't save that setting";
}

/** {available, text}: "From your browser · 12 min ago" (the extension last sent YouTube's login), or
 * how to make one available. The server doesn't say which browser sent it. */
export function loginText(when, { now = new Date() } = {}) {
  if (!toDate(when)) return { available: false, text: "Not available: sign in to YouTube in Chrome or Edge" };
  return { available: true, text: `From your browser · ${formatAgo(when, { now })}` };
}

// --- tools ---

const ACTIVE_PHASES = new Set(["downloading", "verifying", "installing", "waiting"]);
const isActive = (progress) => ACTIVE_PHASES.has(progress?.phase);

const TOOLS = {
  ffmpeg: { label: "ffmpeg", checks: ["ffmpeg", "ffprobe"], missing: "Missing" },
  deno: { label: "Deno", checks: ["deno"], missing: "Missing, so some YouTube formats won't show" },
};

/** The updater's last message, when it is about this tool: {note, noteTone} (empty otherwise). */
function resultFor(message, label) {
  if (typeof message !== "string" || !message) return { note: "", noteTone: null };
  if (label === "yt-dlp") {
    if (message.startsWith("Updated to")) return { note: message, noteTone: "success" };
    if (message.startsWith("Update didn't work")) return { note: message, noteTone: "danger" };
  } else {
    if (message === `Reinstalled ${label}`) return { note: message, noteTone: "success" };
    if (message.startsWith(`Couldn't reinstall ${label}`)) return { note: message, noteTone: "danger" };
  }
  return { note: "", noteTone: null };
}

/** yt-dlp's row: {text: "yt-dlp 2026.08.19", missing, updateLabel: "Update to X" | null, busy, note,
 * noteTone}. note is the update's progress, a check under way, the last result, or when it was checked. */
export function ytdlpRow(status, updates, { now = new Date() } = {}) {
  const tool = status?.tools?.ytdlp;
  const update = status?.updater?.update ?? {};
  const progress = updates?.["yt-dlp"];
  const busy = isActive(progress);
  const missing = tool?.present === false;
  const version = tool?.version ?? update.current;
  const text = missing ? tool.message || "yt-dlp is missing" : version ? `yt-dlp ${version}` : "yt-dlp";
  let { note, noteTone } = resultFor(update.message, "yt-dlp");
  if (busy) ({ note, noteTone } = { note: toolProgress(progress).text, noteTone: null });
  else if (update.state === "checking") ({ note, noteTone } = { note: "Checking for an update…", noteTone: null });
  else if (!note && update.error) ({ note, noteTone } = { note: `Couldn't check for an update: ${update.error}`, noteTone: "danger" });
  else if (!note && toDate(update.last_check)) ({ note, noteTone } = { note: `Checked ${formatAgo(update.last_check, { now })}`, noteTone: null });
  const updateLabel = update.available && update.state !== "installing" ? `Update to ${update.available}` : null;
  return { text, missing, updateLabel, busy, note, noteTone };
}

/** ffmpeg's or Deno's row (name "ffmpeg" | "deno"): {label, text (version or what's wrong), missing, busy,
 * note, noteTone}. */
export function toolRow(status, updates, name) {
  const spec = TOOLS[name];
  const checks = spec.checks.map((key) => status?.tools?.[key]);
  const broken = checks.find((check) => check?.present === false);
  const progress = updates?.[name];
  const busy = isActive(progress);
  const result = resultFor(status?.updater?.update?.message, spec.label);
  return {
    label: spec.label,
    text: broken ? broken.message || spec.missing : checks[0]?.version ?? "",
    missing: Boolean(broken),
    busy,
    note: busy ? toolProgress(progress).text : result.note,
    noteTone: busy ? null : result.noteTone,
  };
}
