// Pure rules for first-run setup and the tools' health: when the setup screen replaces the tabs, the
// per-tool progress text, and the dot on the Settings tab. No DOM. Status is GET /api/status (or the
// store's status); progress is the store's setup map {tool: Progress} (see fields.js).

import { formatSize } from "../format.js";

/** The tools setup installs, in its order (snag.updater.service.SOURCES), with their names for people. */
export const TOOL_LABELS = Object.freeze({ "yt-dlp": "yt-dlp", ffmpeg: "ffmpeg", deno: "Deno" });
const SETUP_TOOLS = Object.keys(TOOL_LABELS);

const SHOWN_STATES = new Set(["running", "error"]);
const isNumber = (value) => typeof value === "number" && Number.isFinite(value);

/** Whether a tool's executables answered the startup check (ffmpeg needs ffprobe too). */
function toolPresent(tools, name) {
  if (!tools) return false;
  if (name === "yt-dlp") return tools.ytdlp?.present === true;
  if (name === "ffmpeg") return tools.ffmpeg?.present === true && tools.ffprobe?.present === true;
  return tools.deno?.present === true;
}

/** {text, value (0-100) | null, indeterminate, tone: null | "done" | "danger"} for one tool's install.
 * present: the tool was already there (setup leaves it alone, so it has no phase). */
export function toolProgress(progress, { present = false } = {}) {
  const phase = progress?.phase ?? null;
  const ready = { text: "Ready", value: 100, indeterminate: false, tone: "done" };
  switch (phase) {
    case "downloading": {
      const done = isNumber(progress.done) ? progress.done : 0;
      const total = isNumber(progress.total) && progress.total > 0 ? progress.total : null;
      if (total === null) {
        return { text: done > 0 ? `Downloading · ${formatSize(done)}` : "Downloading", value: null, indeterminate: true, tone: null };
      }
      return {
        text: `Downloading · ${formatSize(done)} of ${formatSize(total)}`,
        value: Math.min(100, Math.round((done / total) * 100)), indeterminate: false, tone: null,
      };
    }
    case "verifying":
      return { text: "Checking the download", value: null, indeterminate: true, tone: null };
    case "installing":
      return { text: "Installing", value: null, indeterminate: true, tone: null };
    case "waiting":
      return { text: "Waiting for downloads to finish", value: null, indeterminate: true, tone: null };
    case "done":
      return ready;
    case "error":
      return { text: progress.message || "Couldn't install it", value: null, indeterminate: false, tone: "danger" };
    default:
      return present ? ready : { text: "Waiting", value: null, indeterminate: false, tone: null };
  }
}

/** What the setup screen shows, or null when the tabs show instead (setup isn't running and didn't
 * fail). {state, title, message, canRetry, tools: [{name, label, ...toolProgress}]} */
export function setupScreen(status, progress) {
  const setup = status?.updater?.setup;
  if (!setup || !SHOWN_STATES.has(setup.state)) return null;
  const failed = setup.state === "error";
  const byTool = progress ?? setup.tools ?? {};
  return {
    state: setup.state,
    title: failed ? "Snag couldn't finish setting up" : "Setting up Snag…",
    message: failed
      ? setup.message || "Snag couldn't finish setting up."
      : "Snag is downloading the tools it needs (yt-dlp, ffmpeg and Deno). This happens once.",
    canRetry: Boolean(setup.can_retry),
    tools: SETUP_TOOLS.map((name) => ({
      name, label: TOOL_LABELS[name], ...toolProgress(byTool[name], { present: toolPresent(status.tools, name) }),
    })),
  };
}

/** The dot on the Settings tab: {tone: "danger" | "warning" | "accent", label} or null. A missing
 * tool Snag needs comes first, then Deno (optional), then a yt-dlp update. */
export function settingsAttention(status) {
  const tools = status?.tools;
  if (tools) {
    if (tools.ytdlp?.present === false) return { tone: "danger", label: "yt-dlp is missing" };
    if (tools.ffmpeg?.present === false || tools.ffprobe?.present === false) return { tone: "danger", label: "ffmpeg is missing" };
    if (tools.deno?.present === false) return { tone: "warning", label: "Deno is missing" };
  }
  const available = status?.updater?.update?.available;
  return available ? { tone: "accent", label: `yt-dlp ${available} is available` } : null;
}
