// Pure rules behind the playlist picker: which videos start ticked, the counts line, each entry's
// label, the quality chips, the folder the videos go to, and the POST /api/jobs/batch body. No DOM.
// A playlist is the lookup's playlist JSON (see fields.js); entries carry their History duplicate.
// Videos in the queue this session (the store's jobs) are marked "in queue" and start unticked too.

import { cleanExtras } from "../choice.js";
import { JOB_STATE } from "../fields.js";
import { alreadyDownloadedText, formatDuration } from "../format.js";

/** The quality chips, in order: Best available (the default), height caps, MP3, the original audio. */
export const QUALITY_CHIPS = Object.freeze([
  { id: "best", label: "Best available", quality: { kind: "quick", height: null }, audio: false },
  ...[2160, 1440, 1080, 720, 480].map((height) => ({
    id: String(height), label: `≤${height}p`, quality: { kind: "quick", height }, audio: false,
  })),
  { id: "mp3", label: "MP3", quality: { kind: "mp3" }, audio: true },
  { id: "best_audio", label: "Original audio", quality: { kind: "best_audio" }, audio: true },
]);

export const DEFAULT_QUALITY = "best";

export function qualityChip(id) {
  return QUALITY_CHIPS.find((chip) => chip.id === id) ?? QUALITY_CHIPS[0];
}

/** The quality as the batch request takes it ({kind, height?}); a fresh copy. */
export function qualityFor(id) {
  return { ...qualityChip(id).quality };
}

export function isAudioQuality(id) {
  return qualityChip(id).audio;
}

const list = (value) => (Array.isArray(value) ? value : []);

// Jobs whose video is still coming or already came this session; a failed or cancelled one isn't.
const LEFT_OUT = new Set([JOB_STATE.FAILED, JOB_STATE.CANCELLED]);

/** The video ids of a site's videos in the queue this session (waiting, under way, interrupted or
 * done): the same video as a History duplicate, by site and video id. */
export function queuedVideos(jobs, site) {
  const ids = new Set();
  for (const job of list(jobs)) {
    const spec = job?.spec;
    if (!spec || LEFT_OUT.has(job.state) || spec.site !== site || typeof spec.video_id !== "string") continue;
    ids.add(spec.video_id);
  }
  return ids;
}

/** The entries ticked when the picker opens: every video neither downloaded before nor in the queue
 * (queued: queuedVideos). */
export function defaultSelection(entries, queued = new Set()) {
  return new Set(list(entries).filter((entry) => !entry.duplicate && !queued.has(entry.video_id)).map((entry) => entry.index));
}

const plural = (count, one, many = `${one}s`) => `${count} ${count === 1 ? one : many}`;

/** "18 videos · 5 selected". */
export function countText(total, selected) {
  return `${plural(total, "video")} · ${selected} selected`;
}

/** The primary button: "Download 5 videos", "Download 1 video", "Download" with nothing ticked. */
export function downloadLabel(selected) {
  return selected > 0 ? `Download ${plural(selected, "video")}` : "Download";
}

/** "01 · Title": the position padded like the files' names (at least two digits, as wide as the count). */
export function entryLabel(entry, count) {
  const width = Math.max(2, String(count || entry.index).length);
  return `${String(entry.index).padStart(width, "0")} · ${entry.title || "Untitled"}`;
}

/** {duration, haveIt, haveItTitle, inQueue} for an entry's meta line; haveItTitle explains the "have
 * it" mark, inQueue is true for a video in the queue this session (queued: queuedVideos). */
export function entryMeta(entry, { now = new Date(), queued = new Set() } = {}) {
  return {
    duration: formatDuration(entry.duration),
    haveIt: Boolean(entry.duplicate),
    haveItTitle: alreadyDownloadedText(entry.duplicate, { now }),
    inQueue: queued.has(entry.video_id),
  };
}

/** The ticked indexes in playlist order. */
export function selectedIndexes(entries, selection) {
  return list(entries).filter((entry) => selection.has(entry.index)).map((entry) => entry.index);
}

// --- folder ---

// snag/jobs/naming.py safe_name: characters Windows refuses become "_", trailing dots and spaces go,
// names stop at 150 characters, and device names (CON, COM1, …) get a leading "_".
const ILLEGAL = /[<>:"/\\|?*\u0000-\u001f]/g;
const MAX_NAME = 150;
const RESERVED = new Set([
  "CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
  ...["COM", "LPT"].flatMap((port) => [..."0123456789", "¹", "²", "³"].map((suffix) => `${port}${suffix}`)),
]);
const TRAILING = /[. ]+$/;

/** The folder name the server gives a playlist (snag.jobs.naming.playlist_dest). */
export function playlistFolderName(title) {
  const trimmed = String(title ?? "").replace(ILLEGAL, "_").trim().replace(TRAILING, "");
  // Python counts characters by code point: an emoji is one, never two UTF-16 halves to split apart.
  const cleaned = Array.from(trimmed).slice(0, MAX_NAME).join("").replace(TRAILING, "");
  if (!cleaned) return "Playlist";
  return RESERVED.has(cleaned.split(".", 1)[0].replace(/ +$/, "").toUpperCase()) ? `_${cleaned}` : cleaned;
}

/** base\name, with one separator. */
export function joinFolder(base, name) {
  if (!base) return name;
  return /[\\/]$/.test(base) ? `${base}${name}` : `${base}\\${name}`;
}

/** The folder the videos are saved in: a folder named after the playlist inside the picked folder, or
 * inside the settings' video or audio folder (by the quality). */
export function playlistFolder({ settings, qualityId, title, pickedBase = null }) {
  const base = pickedBase || (isAudioQuality(qualityId) ? settings?.audio_dir : settings?.video_dir) || "";
  return joinFolder(base, playlistFolderName(title));
}

// --- request ---

/** The POST /api/jobs/batch body. dest_dir is the picked base folder (the server adds the playlist's
 * folder), left out for the default. Throws when nothing is ticked. */
export function buildBatchRequest({ url, entries, selection, qualityId, extras = null, pickedBase = null }) {
  const picked = selectedIndexes(entries, selection);
  if (!picked.length) throw new Error("Tick at least one video");
  const body = { url, entries: picked, quality: qualityFor(qualityId) };
  const cleaned = cleanExtras(extras);
  if (cleaned) body.extras = cleaned;
  if (pickedBase) body.dest_dir = pickedBase;
  return body;
}
