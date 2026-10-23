// Pure text formatting for everything the UI shows: sizes, times, codecs, languages, format rows,
// the extras summary line and the "already downloaded" note. No DOM, no globals except Intl.
//
// Times from the server are wall-clock seconds (Python's time.time()) or ISO 8601 text.

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const UNITS = ["KB", "MB", "GB", "TB"];

const isNumber = (value) => typeof value === "number" && Number.isFinite(value);

function decimals(value, digits) {
  const text = value.toFixed(digits);
  return text.includes(".") ? text.replace(/\.?0+$/, "") : text;
}

// --- sizes and speeds ---

/** "1 byte", "512 bytes", "4.3 MB", "218 MB", "~1.4 GB" (binary units, as Windows Explorer shows
 * them). Every unit is chosen after rounding, so nothing reads "1024 bytes" or "1024 KB". */
export function formatSize(bytes, { estimate = false } = {}) {
  if (!isNumber(bytes) || bytes < 0) return "";
  const prefix = estimate ? "~" : "";
  const whole = Math.round(bytes);
  if (whole < 1024) return `${prefix}${whole} ${whole === 1 ? "byte" : "bytes"}`;
  let value = bytes;
  for (let unit = 0; unit < UNITS.length; unit += 1) {
    value /= 1024;
    const digits = unit === 0 || value >= 10 ? 0 : 1;
    const shown = Number(value.toFixed(digits));
    if (shown < 1024 || unit === UNITS.length - 1) return `${prefix}${decimals(value, digits)} ${UNITS[unit]}`;
  }
  return "";
}

/** "3.4 MB/s"; empty when unknown or stalled. */
export function formatSpeed(bytesPerSecond) {
  if (!isNumber(bytesPerSecond) || bytesPerSecond <= 0) return "";
  return `${formatSize(bytesPerSecond)}/s`;
}

/** "42%" (rounded down, so a download never reads 100% before it's done). */
export function formatPercent(percent) {
  if (!isNumber(percent)) return "";
  return `${Math.max(0, Math.min(100, Math.floor(percent)))}%`;
}

/** "4.2 Mbps", "135 kbps"; null for no bitrate. */
export function formatBitrate(kbps) {
  if (!isNumber(kbps) || kbps <= 0) return null;
  return kbps >= 1000 ? `${decimals(kbps / 1000, 1)} Mbps` : `${Math.round(kbps)} kbps`;
}

// --- durations ---

const pad2 = (number) => String(number).padStart(2, "0");

/** "0:59", "59:59", "1:00:00". */
export function formatDuration(seconds) {
  if (!isNumber(seconds) || seconds < 0) return "";
  const whole = Math.floor(seconds);
  const hours = Math.floor(whole / 3600);
  const minutes = Math.floor((whole % 3600) / 60);
  const secs = whole % 60;
  return hours > 0 ? `${hours}:${pad2(minutes)}:${pad2(secs)}` : `${minutes}:${pad2(secs)}`;
}

/** "45 sec left", "2 min left", "1 h 5 min left": rounded up, and the unit is chosen after rounding,
 * so 59.2 s reads "1 min left" and 3599 s "1 h left" (never "60 sec" or "60 min"). */
export function formatEta(seconds) {
  if (!isNumber(seconds) || seconds < 0) return "";
  if (seconds < 1) return "Almost done";
  const wholeSeconds = Math.ceil(seconds);
  if (wholeSeconds < 60) return `${wholeSeconds} sec left`;
  const wholeMinutes = Math.ceil(wholeSeconds / 60);
  if (wholeMinutes < 60) return `${wholeMinutes} min left`;
  const hours = Math.floor(wholeMinutes / 60);
  const minutes = wholeMinutes % 60;
  return minutes > 0 ? `${hours} h ${minutes} min left` : `${hours} h left`;
}

// --- dates ---

/** A Date from wall-clock seconds, ISO text or a Date; null when it isn't one. */
export function toDate(value) {
  let date = null;
  if (value instanceof Date) date = new Date(value.getTime());
  else if (isNumber(value)) date = new Date(value * 1000);
  else if (typeof value === "string" && value.trim()) date = new Date(value);
  return date && Number.isFinite(date.getTime()) ? date : null;
}

const startOfDay = (date) => new Date(date.getFullYear(), date.getMonth(), date.getDate());

function daysBefore(date, now) {
  return Math.round((startOfDay(now) - startOfDay(date)) / 86400000);
}

/** "14:02" (24-hour, local time). */
export function formatClock(value) {
  const date = toDate(value);
  return date ? `${pad2(date.getHours())}:${pad2(date.getMinutes())}` : "";
}

/** "today", "yesterday", "Mar 3", or "Mar 3, 2024" in another year: for notes inside a sentence. */
export function formatDay(value, { now = new Date() } = {}) {
  const date = toDate(value);
  if (!date) return "";
  const days = daysBefore(date, now);
  if (days === 0) return "today";
  if (days === 1) return "yesterday";
  const day = `${MONTHS[date.getMonth()]} ${date.getDate()}`;
  return date.getFullYear() === now.getFullYear() ? day : `${day}, ${date.getFullYear()}`;
}

/** "Today 14:02", "Yesterday 09:05", "Mar 3 09:05", or "Dec 31, 2025" in another year. */
export function formatDate(value, { now = new Date() } = {}) {
  const date = toDate(value);
  if (!date) return "";
  const day = formatDay(date, { now });
  if (date.getFullYear() !== now.getFullYear() && daysBefore(date, now) > 1) return day;
  return `${day.charAt(0).toUpperCase()}${day.slice(1)} ${formatClock(date)}`;
}

/** "just now", "12 min ago", "3 h ago" (today), then calendar days like formatDay: "yesterday",
 * "5 days ago". Within the last hour it's minutes, even across midnight. */
export function formatAgo(value, { now = new Date() } = {}) {
  const date = toDate(value);
  if (!date) return "";
  const seconds = (now.getTime() - date.getTime()) / 1000;
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  const days = daysBefore(date, now);
  if (days <= 0) return `${Math.floor(seconds / 3600)} h ago`;
  if (days === 1) return "yesterday";
  return `${days} days ago`;
}

// --- codecs, containers, languages ---

const CODECS = {
  av01: "AV1", vp09: "VP9", vp9: "VP9", vp8: "VP8", avc1: "H.264", avc3: "H.264", h264: "H.264",
  hev1: "HEVC", hvc1: "HEVC", hevc: "HEVC", h265: "HEVC", mp4a: "AAC", aac: "AAC", opus: "Opus",
  vorbis: "Vorbis", mp3: "MP3", flac: "FLAC", "ac-3": "AC-3", "ec-3": "E-AC-3", alac: "ALAC",
};

/** "av01.0.08M.08" → "AV1", "avc1.64002A" → "H.264", "mp4a.40.2" → "AAC"; empty for none, or for
 * anything that isn't text. */
export function codecName(codec) {
  if (typeof codec !== "string" || !codec || codec === "none") return "";
  const name = codec.split(".")[0].toLowerCase();
  return Object.hasOwn(CODECS, name) ? CODECS[name] : codec;
}

/** "mp4" → "MP4". */
export function containerName(ext) {
  return ext ? String(ext).toUpperCase() : "";
}

let languageNames = null;

/** "en" → "English", "pt-BR" → "Brazilian Portuguese", "und" → "Unknown language"; the code itself
 * when there's no name; empty for anything that isn't text. */
export function languageName(code) {
  if (typeof code !== "string" || !code) return "";
  if (code.toLowerCase() === "und") return "Unknown language";
  try {
    languageNames ??= new Intl.DisplayNames(["en"], { type: "language", fallback: "code" });
    return languageNames.of(code) ?? code;
  } catch {
    return code;
  }
}

// --- format rows ---

/** The row's label from the server ("1080p60", "Opus 130k", "MP3"), or one made from its fields. */
export function rowLabel(row) {
  if (row.label) return row.label;
  if (row.kind === "mp3") return "MP3";
  if (row.kind === "audio") {
    const name = codecName(row.acodec) || containerName(row.ext);
    return isNumber(row.abr) ? `${name} ${Math.round(row.abr)}k` : name;
  }
  const fps = isNumber(row.fps) && row.fps > 30 ? Math.round(row.fps) : "";
  return isNumber(row.height) ? `${row.height}p${fps}` : containerName(row.ext);
}

// --- extras ---

const TRIM_TIME = /^[0-9]+(:[0-5]?[0-9]){0,2}(\.[0-9]+)?$/;

/** "90", "1:30", "1:02:03.5" → seconds (the server's rule: minutes and seconds after the first field
 * are 0-59); null when the text isn't a time. */
export function trimSeconds(text) {
  if (typeof text !== "string" || !TRIM_TIME.test(text)) return null;
  const [whole, fraction] = text.split(".");
  let seconds = 0;
  for (const part of whole.split(":")) seconds = seconds * 60 + Number(part);
  return fraction ? seconds + Number(`0.${fraction}`) : seconds;
}

const SPONSOR_CATEGORIES = {
  sponsor: "Sponsor", selfpromo: "Self-promotion", interaction: "Interaction reminder", intro: "Intro",
  outro: "End cards and credits", preview: "Preview or recap", filler: "Filler", music_offtopic: "Non-music section",
  hook: "Hook", poi_highlight: "Highlight", chapter: "Chapter",
};

/** "selfpromo" → "Self-promotion". */
export function sponsorCategoryName(category) {
  return Object.hasOwn(SPONSOR_CATEGORIES, category) ? SPONSOR_CATEGORIES[category] : category;
}

function listText(items) {
  if (items.length <= 1) return items.join("");
  return `${items.slice(0, -1).join(", ")} and ${items.at(-1)}`;
}

const capitalize = (text) => (text ? text.charAt(0).toUpperCase() + text.slice(1) : text);

/** One line describing extras (the server's Extras as JSON), naming what a trim makes unavailable:
 * "English subtitles, thumbnail and tags, chapters". Any trim drops chapters and SponsorBlock; only a
 * trim from a start time drops subtitles. audioOnly: subtitles don't apply. */
export function extrasSummary(extras, { audioOnly = false } = {}) {
  if (!extras) return "No extras";
  const start = extras.trim_start || null;
  const end = extras.trim_end || null;
  const trimmed = Boolean(start || end);
  const startsLate = start !== null && (trimSeconds(start) ?? 0) > 0;
  const parts = [];
  const unavailable = [];
  let subtitlesDropped = false;

  if (extras.subtitles && !audioOnly) {
    if (startsLate) subtitlesDropped = true;
    else {
      const file = extras.subtitle_mode === "file" ? " as a file" : "";
      parts.push(`${languageName(extras.subtitle_lang) || "Default"} subtitles${file}`);
    }
  }
  if (extras.thumbnail_tags) parts.push("thumbnail and tags");
  if (extras.chapters) {
    if (trimmed) unavailable.push("chapters");
    else parts.push("chapters");
  }
  const categories = extras.sponsorblock_categories ?? [];
  if (extras.sponsorblock && categories.length) {
    if (trimmed) unavailable.push("SponsorBlock");
    else {
      const what = categories.length === 1
        ? `${sponsorCategoryName(categories[0]).toLowerCase()} segments`
        : `${categories.length} kinds of segment`;
      parts.push(extras.sponsorblock_mode === "mark" ? `SponsorBlock marks ${what} as chapters` : `SponsorBlock removes ${what}`);
    }
  }
  if (start && end) parts.push(`trim ${start}–${end}`);
  else if (start) parts.push(`trim from ${start}`);
  else if (end) parts.push(`trim to ${end}`);

  const notes = [];
  if (unavailable.length) notes.push(`${listText(unavailable)} not available when trimming`);
  if (subtitlesDropped) notes.push("subtitles not available when trimming from a start time");
  if (!parts.length && !notes.length) return "No extras";
  let line = parts.join(", ");
  if (notes.length) {
    const note = notes.join("; ");
    line = line ? `${line} (${note})` : note;
  }
  return capitalize(line);
}

// --- notes and labels ---

/** "Already downloaded Mar 3 (1080p MP4)" from a History entry, leaving out what it lacks; empty
 * without one. */
export function alreadyDownloadedText(entry, { now = new Date() } = {}) {
  if (!entry) return "";
  const words = ["Already downloaded", formatDay(entry.finished_at, { now })];
  if (entry.format_summary) words.push(`(${entry.format_summary})`);
  return words.filter(Boolean).join(" ");
}
