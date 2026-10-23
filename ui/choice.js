// Pure rules behind the download panel: which format rows to show, what a download request looks
// like, and the extras rules mirrored from the server (snag/jobs/models.py validate_extras, gets_*),
// so the UI can say what a choice will really do before it is sent. No DOM.
//
// Rows are the server's FormatRow JSON and extras its Extras JSON (see fields.js).

import { UNFINISHED_JOB_STATES } from "./fields.js";
import {
  alreadyDownloadedText, formatDuration, languageName, sponsorCategoryName, trimSeconds,
} from "./format.js";

/** The panel's Video / Audio only switch. */
export const MODE = Object.freeze({ VIDEO: "video", AUDIO: "audio" });

/** FormatRow.kind (snag/info/models.py). */
export const FORMAT_KIND = Object.freeze({ VIDEO: "video", AUDIO: "audio", MP3: "mp3" });

// yt-dlp's SponsorBlock categories (snag.jobs.models.SPONSORBLOCK_CATEGORIES); removing can't use the
// highlight point or chapter names (SPONSORBLOCK_REMOVABLE).
export const SPONSORBLOCK_CATEGORIES = Object.freeze([
  "sponsor", "intro", "outro", "selfpromo", "preview", "filler", "interaction", "music_offtopic", "hook",
  "poi_highlight", "chapter",
]);
export const SPONSORBLOCK_REMOVABLE = Object.freeze(
  SPONSORBLOCK_CATEGORIES.filter((category) => category !== "poi_highlight" && category !== "chapter"),
);
export const SPONSORBLOCK_MODE = Object.freeze({ REMOVE: "remove", MARK: "mark" });
export const SUBTITLE_MODE = Object.freeze({ EMBEDDED: "embed", FILE: "file" });

// yt-dlp's live_status values the server refuses to download.
const LIVE_STATES = new Set(["is_live", "is_upcoming"]);

const TRIM_HINT = "Use a time like 90, 1:30 or 1:02:03.5";
/** The longest trim time the server takes (snag.server.schemas MAX_TRIM), in characters. */
export const MAX_TRIM_LENGTH = 32;
const TRIM_TOO_LONG = `Use a time of at most ${MAX_TRIM_LENGTH} characters`;
const isNumber = (value) => typeof value === "number" && Number.isFinite(value);
const list = (value) => (Array.isArray(value) ? value : []);

// --- rows ---

/** The simple view: one row per resolution for video; MP3 then the original-language audio for audio
 * (every track when none is marked original). */
export function simpleRows(formats, mode) {
  if (mode === MODE.VIDEO) return list(formats?.video_simple);
  const audio = list(formats?.audio);
  const tracks = audio.filter((row) => row.kind !== FORMAT_KIND.MP3);
  const original = tracks.filter((row) => row.is_original);
  return [...audio.filter((row) => row.kind === FORMAT_KIND.MP3), ...(original.length ? original : tracks)];
}

/** "Show all formats": every row of the mode. */
export function allRows(formats, mode) {
  return mode === MODE.VIDEO ? list(formats?.video_all) : list(formats?.audio);
}

/** The rows to list. A row picked in the expanded view stays visible after collapsing, so the
 * selection never silently changes. limit (the popup's compact panel): the simple view lists only its
 * first `limit` rows (the best), plus the selected row wherever it is, so that stays visible too. */
export function visibleRows(formats, mode, { expanded = false, selectedId = null, limit = null } = {}) {
  if (expanded) return allRows(formats, mode);
  const all = simpleRows(formats, mode);
  const rows = limit === null ? all : all.filter((row, index) => index < limit || row.format_id === selectedId);
  if (selectedId === null || rows.some((row) => row.format_id === selectedId)) return rows;
  const picked = allRows(formats, mode).find((row) => row.format_id === selectedId);
  return picked ? [...rows, picked] : rows;
}

/** The row picked when the panel opens or the mode changes: the best (first) of the simple view. */
export function defaultRowId(formats, mode) {
  return simpleRows(formats, mode)[0]?.format_id ?? null;
}

/** A row by format id from any list; null when there is none. */
export function findRow(formats, id) {
  if (id === null || id === undefined) return null;
  for (const name of ["video_all", "video_simple", "audio", "audio_tracks"]) {
    const row = list(formats?.[name]).find((item) => item.format_id === id);
    if (row) return row;
  }
  return null;
}

// --- audio track ---

/** Only a video format without its own audio can use another audio track (choice_for_row). */
export function takesAudioTrack(row) {
  return Boolean(row) && row.kind === FORMAT_KIND.VIDEO && !row.has_audio;
}

/** "English (original) · m4a 129k". */
export function audioTrackLabel(row) {
  const language = languageName(row.language) || "Unknown language";
  return `${row.is_original ? `${language} (original)` : language} · ${row.label}`;
}

/** The Audio track dropdown: "auto" (id null: the row's own pairing) first, then every track. */
export function audioTrackOptions(formats) {
  return [
    { id: null, label: "Auto (original language)" },
    ...list(formats?.audio_tracks).map((row) => ({ id: row.format_id, label: audioTrackLabel(row) })),
  ];
}

// --- the finished file's container ---

// yt-dlp's utils.get_compatible_ext, as snag/info/containers.py mirrors it: COMPATIBLE_CODECS and
// COMPATIBLE_EXTS.
const COMPATIBLE_CODECS = [
  ["mp4", new Set(["av1", "hevc", "avc1", "mp4a", "ac-4", "h264", "aacl", "ec-3"])],
  ["webm", new Set(["av1", "vp9", "vp8", "opus", "vrbs", "vp9x", "vp8x"])],
];
const COMPATIBLE_EXTS = [
  new Set(["mp3", "mp4", "m4a", "m4p", "m4b", "m4r", "m4v", "ismv", "isma", "mov"]),
  new Set(["webm", "weba"]),
];

/** yt-dlp's sanitize_codec: "av01.0.08M.08" -> "av1", "vp09.00.40.08" -> "vp9", "mp4a.40.2" -> "mp4a". */
function sanitizeCodec(codec) {
  return typeof codec === "string" ? codec.split(".")[0].replaceAll("0", "").toLowerCase() : null;
}

/** The container yt-dlp merges one video and one audio stream into (containers.merged_ext): e.g.
 * H.264 (mp4) + AAC (m4a) -> mp4, AV1 (mp4) + Opus -> webm, H.264 + Opus -> mkv. */
export function mergedExt(vcodec, vext, acodec, aext) {
  const video = sanitizeCodec(vcodec);
  const audio = sanitizeCodec(acodec);
  for (const [ext, codecs] of COMPATIBLE_CODECS) {
    if (codecs.has(video) && codecs.has(audio)) return ext;
  }
  const exts = new Set([vext, aext]);
  if (vext === "mkv" || exts.size === 1 || COMPATIBLE_EXTS.some((group) => [...exts].every((ext) => group.has(ext)))) {
    return vext;
  }
  return "mkv";
}

/** The container the finished file gets ("mp4"), or null when it can't be known in advance: the row's
 * own result_ext, or, for a video row given another audio track, the container the merge with that
 * track gets (the server's snag.jobs.command.result_exts). */
export function resultExt(row, audioTrack = null) {
  if (!row) return null;
  if (!takesAudioTrack(row) || !audioTrack || audioTrack.format_id === row.audio_id) return row.result_ext ?? null;
  const ext = String(row.ext ?? "").toLowerCase();
  if (!ext || typeof audioTrack.ext !== "string") return null;
  return mergedExt(row.vcodec, ext, audioTrack.acodec, audioTrack.ext.toLowerCase());
}

// --- sizes ---

/** {bytes, estimate} for a row; with another audio track, the paired track's size is swapped for it.
 * bytes is null when it can't be known. */
export function rowSize(row, { formats = null, audioTrack = null } = {}) {
  const bytes = isNumber(row?.size) ? row.size : null;
  const own = { bytes, estimate: bytes !== null && Boolean(row.size_is_estimate) };
  if (!takesAudioTrack(row) || !audioTrack || audioTrack.format_id === row.audio_id || bytes === null) return own;
  const paired = list(formats?.audio_tracks).find((track) => track.format_id === row.audio_id);
  const pairedSize = row.audio_id ? paired?.size : 0;
  if (!isNumber(pairedSize) || !isNumber(audioTrack.size)) return { bytes: null, estimate: false };
  return {
    bytes: bytes - pairedSize + audioTrack.size,
    estimate: Boolean(row.size_is_estimate || audioTrack.size_is_estimate),
  };
}

// --- requests ---

function cleanTrim(value) {
  if (typeof value !== "string") return null;
  const text = value.trim();
  return text || null;
}

/** Extras as the server takes them: blank trim times are null; lists are copied. */
export function cleanExtras(extras) {
  if (!extras) return null;
  return {
    ...extras,
    sponsorblock_categories: [...list(extras.sponsorblock_categories)],
    trim_start: cleanTrim(extras.trim_start),
    trim_end: cleanTrim(extras.trim_end),
  };
}

/** The POST /api/jobs body (schemas.parse_job_request). audioTrack: another audio track for a video
 * row (left out when it is the row's own pairing or the row can't take one); destDir: a folder the
 * person picked, or null for the server's default (video or audio folder). */
export function buildJobRequest({ url, row, audioTrack = null, extras = null, destDir = null }) {
  if (!row?.format_id) throw new Error("Pick a format first");
  const body = { url, format_id: row.format_id };
  if (audioTrack && takesAudioTrack(row) && audioTrack.format_id !== row.audio_id) body.audio_id = audioTrack.format_id;
  const cleaned = cleanExtras(extras);
  if (cleaned) body.extras = cleaned;
  if (destDir) body.dest_dir = destDir;
  return body;
}

// --- extras rules ---

/** Inline trim errors, worded for people: {start, end}, each a message or null. A time longer than
 * MAX_TRIM_LENGTH (outer spaces aren't sent, so they don't count) is refused, as the server does. */
export function trimErrors(start, end) {
  const startText = cleanTrim(start);
  const endText = cleanTrim(end);
  const startSeconds = startText === null ? null : trimSeconds(startText);
  const endSeconds = endText === null ? null : trimSeconds(endText);
  const problem = (text, seconds) => {
    if (text === null) return null;
    if ([...text].length > MAX_TRIM_LENGTH) return TRIM_TOO_LONG;
    return seconds === null ? TRIM_HINT : null;
  };
  const errors = { start: problem(startText, startSeconds), end: problem(endText, endSeconds) };
  if (!errors.end && startSeconds !== null && endSeconds !== null && endSeconds <= startSeconds) {
    errors.end = "The end must come after the start";
  }
  return errors;
}

/** The note under the trim fields: what the trim makes unavailable (empty without a trim). */
export function trimNote(extras, { audioOnly = false } = {}) {
  const start = cleanTrim(extras?.trim_start);
  const end = cleanTrim(extras?.trim_end);
  if (!start && !end) return "";
  const startsLate = start !== null && (trimSeconds(start) ?? 0) > 0;
  if (startsLate && !audioOnly) {
    return "Chapters, SponsorBlock and subtitles aren't available when trimming from a start time.";
  }
  return "Chapters and SponsorBlock aren't available when trimming.";
}

/** Which extras this download can really get (snag.jobs.models gets_*): {subtitles, chapters,
 * sponsorblock}. Audio has no subtitles; any trim drops chapters and SponsorBlock; a trim from a start
 * time drops subtitles too. */
export function extrasAvailability(extras, { audioOnly = false } = {}) {
  const start = cleanTrim(extras?.trim_start);
  const trimmed = Boolean(start || cleanTrim(extras?.trim_end));
  const startsLate = start !== null && (trimSeconds(start) ?? 0) > 0;
  return { subtitles: !audioOnly && !startsLate, chapters: !trimmed, sponsorblock: !trimmed };
}

/** SponsorBlock categories a mode can use. */
export function sponsorCategoriesFor(mode) {
  return mode === SPONSORBLOCK_MODE.MARK ? SPONSORBLOCK_CATEGORIES : SPONSORBLOCK_REMOVABLE;
}

/** Extras with another SponsorBlock mode, dropping categories the new mode can't use. */
export function withSponsorMode(extras, mode) {
  const allowed = sponsorCategoriesFor(mode);
  return {
    ...extras,
    sponsorblock_mode: mode,
    sponsorblock_categories: list(extras.sponsorblock_categories).filter((category) => allowed.includes(category)),
  };
}

/** {valid, errors: {trim_start?, trim_end?, sponsorblock?}} as the server's validate_extras would
 * judge the extras, in plain words. */
export function validateExtras(extras) {
  const errors = {};
  const trim = trimErrors(extras?.trim_start, extras?.trim_end);
  if (trim.start) errors.trim_start = trim.start;
  if (trim.end) errors.trim_end = trim.end;
  const categories = list(extras?.sponsorblock_categories);
  const mode = extras?.sponsorblock_mode ?? SPONSORBLOCK_MODE.REMOVE;
  const allowed = sponsorCategoriesFor(mode);
  const refused = categories.find((category) => !allowed.includes(category));
  if (refused) errors.sponsorblock = `SponsorBlock can't ${mode} ${sponsorCategoryName(refused)}`;
  else if (extras?.sponsorblock && !categories.length) errors.sponsorblock = "Pick at least one SponsorBlock category";
  return { valid: Object.keys(errors).length === 0, errors };
}

// --- subtitles and chapters ---

// Offered when there is no video to ask (the defaults in Settings).
const COMMON_SUBTITLE_LANGS = [
  "en", "es", "fr", "de", "it", "pt", "nl", "sv", "pl", "ru", "uk", "tr", "ar", "hi", "ja", "ko", "zh-Hans", "zh-Hant",
];

/** The subtitle language choices for a video: made by people first, then auto-generated ones, then
 * the current choice when the video doesn't offer it. Without a video: common languages. [{code, label}] */
export function subtitleLanguages(info, current) {
  const options = [];
  const seen = new Set();
  const add = (code, suffix = "") => {
    if (!code || seen.has(code)) return;
    seen.add(code);
    options.push({ code, label: `${languageName(code)}${suffix}` });
  };
  if (!info) {
    for (const code of COMMON_SUBTITLE_LANGS) add(code);
    add(current);
    return options;
  }
  for (const code of list(info?.subtitle_langs)) add(code);
  for (const code of list(info?.auto_subtitle_langs)) add(code, " (auto-generated)");
  if (current && !seen.has(current)) add(current, " (not offered)");
  return options;
}

/** A note when the chosen subtitles don't exist for this video (empty otherwise). */
export function subtitleNote(info, extras, { audioOnly = false } = {}) {
  if (!info || !extras?.subtitles || audioOnly) return "";
  const code = extras.subtitle_lang;
  const offered = [...list(info.subtitle_langs), ...list(info.auto_subtitle_langs)];
  return offered.includes(code) ? "" : `This video has no ${languageName(code)} subtitles.`;
}

/** A note when chapters are on but the video has none (empty otherwise). */
export function chaptersNote(info, extras) {
  return info && extras?.chapters && info.has_chapters === false ? "This video has no chapters." : "";
}

// --- folder, notes, meta line ---

/** The folder a download goes to when none is picked: the settings' video or audio folder. */
export function defaultFolder(settings, mode) {
  return (mode === MODE.AUDIO ? settings?.audio_dir : settings?.video_dir) ?? "";
}

/** A live or upcoming stream: the server refuses to download it. */
export function isLive(info) {
  return LIVE_STATES.has(info?.live_status);
}

/** Notes shown under the title: [{kind: "live" | "deno", text}]. */
export function lookupNotes(info) {
  const notes = [];
  if (info?.live_status === "is_live") {
    notes.push({ kind: "live", text: "This is a live stream. Snag can download it once it has ended." });
  } else if (info?.live_status === "is_upcoming") {
    notes.push({ kind: "live", text: "This stream hasn't started yet. Snag can download it once it has ended." });
  }
  if (info?.js_runtime_missing) notes.push({ kind: "deno", text: "Deno is missing, so some YouTube formats won't show" });
  return notes;
}

/** The line under the title: {parts: [channel, length], duplicate: "already downloaded Mar 3 (1080p MP4)"}. */
export function metaLine(info, { now = new Date() } = {}) {
  const parts = [info?.channel, formatDuration(info?.duration)].filter(Boolean);
  const text = alreadyDownloadedText(info?.duplicate, { now });
  return { parts, duplicate: text ? text.charAt(0).toLowerCase() + text.slice(1) : "" };
}

/** The unfinished job already downloading this video (same site and id), or null. */
export function queuedJobFor(jobs, info) {
  if (!info) return null;
  return jobs.find((job) => UNFINISHED_JOB_STATES.includes(job.state)
    && job.spec?.site === info.site && job.spec?.video_id === info.video_id) ?? null;
}
