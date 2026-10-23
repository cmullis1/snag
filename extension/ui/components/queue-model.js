// Pure rules behind the queue bar, job rows and queue banner: grouping, counts, combined progress,
// batch summaries, the actions each job offers, and banner text. No DOM; jobs are the server's Job JSON
// (see fields.js).

import { ACTIVE_JOB_STATES, ERROR_ACTION, JOB_STATE } from "../fields.js";
import { formatClock, formatEta, formatPercent, formatSpeed, toDate } from "../format.js";

export const JOB_GROUP = Object.freeze({ RUNNING: "running", WAITING: "waiting", FAILED: "failed", FINISHED: "finished" });

const GROUP_ORDER = [JOB_GROUP.RUNNING, JOB_GROUP.WAITING, JOB_GROUP.FAILED, JOB_GROUP.FINISHED];
const GROUP_LABELS = {
  [JOB_GROUP.RUNNING]: "Downloading", [JOB_GROUP.WAITING]: "Waiting", [JOB_GROUP.FAILED]: "Failed",
  [JOB_GROUP.FINISHED]: "Finished",
};

/** What a job row offers. */
export const ACTION = Object.freeze({
  CANCEL: "cancel",
  RETRY: "retry",
  CHANGE_FOLDER: "change_folder",
  CHECK_UPDATE: "check_update",
  REINSTALL_FFMPEG: "reinstall_ffmpeg",
  CLEAR: "clear",
  DETAILS: "details",
  OPEN_FOLDER: "open_folder",
});

const isNumber = (value) => typeof value === "number" && Number.isFinite(value);
const isActive = (state) => ACTIVE_JOB_STATES.includes(state);

/** The group a job state is listed in. Interrupted jobs are re-queued, so they wait. */
export function groupOf(state) {
  if (isActive(state)) return JOB_GROUP.RUNNING;
  if (state === JOB_STATE.WAITING || state === JOB_STATE.INTERRUPTED) return JOB_GROUP.WAITING;
  if (state === JOB_STATE.FAILED) return JOB_GROUP.FAILED;
  return JOB_GROUP.FINISHED;
}

// --- counts and progress ---

/** {downloading, waiting, failed, done, cancelled, total}. */
export function queueCounts(jobs) {
  const counts = { downloading: 0, waiting: 0, failed: 0, done: 0, cancelled: 0, total: jobs.length };
  for (const job of jobs) {
    const group = groupOf(job.state);
    if (group === JOB_GROUP.RUNNING) counts.downloading += 1;
    else if (group === JOB_GROUP.WAITING) counts.waiting += 1;
    else if (group === JOB_GROUP.FAILED) counts.failed += 1;
    else if (job.state === JOB_STATE.DONE) counts.done += 1;
    else counts.cancelled += 1;
  }
  return counts;
}

/** "2 downloading · 1 waiting" while anything is unfinished, else "2 done · 1 failed". */
export function queueSummaryText(counts) {
  const parts = [];
  if (counts.downloading) parts.push(`${counts.downloading} downloading`);
  if (counts.waiting) parts.push(`${counts.waiting} waiting`);
  const busy = parts.length > 0;
  if (!busy && counts.done) parts.push(`${counts.done} done`);
  if (counts.failed) parts.push(`${counts.failed} failed`);
  if (!busy && !parts.length && counts.cancelled) parts.push(`${counts.cancelled} cancelled`);
  return parts.length ? parts.join(" · ") : "No downloads";
}

function jobPercent(job) {
  if (job.state === JOB_STATE.PROCESSING) return 100;
  return isNumber(job.percent) ? Math.max(0, Math.min(100, job.percent)) : 0;
}

/** Overall progress (0-100) of the unfinished jobs, weighted by their estimated sizes when every one is
 * known; null when nothing is unfinished. */
export function combinedProgress(jobs) {
  const open = jobs.filter((job) => groupOf(job.state) === JOB_GROUP.RUNNING || groupOf(job.state) === JOB_GROUP.WAITING);
  if (!open.length) return null;
  const sizes = open.map((job) => job.spec?.estimated_size);
  const bySize = sizes.every((size) => isNumber(size) && size > 0);
  let total = 0;
  let done = 0;
  open.forEach((job, index) => {
    const weight = bySize ? sizes[index] : 1;
    total += weight;
    done += (weight * jobPercent(job)) / 100;
  });
  return (done / total) * 100;
}

// --- batches and grouping ---

/** {total, done, failed, cancelled, running, waiting} for a batch's jobs. */
export function batchCounts(jobs) {
  const counts = queueCounts(jobs);
  return {
    total: counts.total, done: counts.done, failed: counts.failed, cancelled: counts.cancelled,
    running: counts.downloading, waiting: counts.waiting,
  };
}

/** "40 of 42 done, 2 failed", with what is still going: "3 of 10 done · 2 downloading · 4 waiting". */
export function batchSummaryText(counts) {
  let text = `${counts.done} of ${counts.total} done`;
  const ended = [];
  if (counts.failed) ended.push(`${counts.failed} failed`);
  if (counts.cancelled) ended.push(`${counts.cancelled} cancelled`);
  if (ended.length) text += `, ${ended.join(", ")}`;
  const going = [];
  if (counts.running) going.push(`${counts.running} downloading`);
  if (counts.waiting) going.push(`${counts.waiting} waiting`);
  return [text, ...going].join(" · ");
}

function folderName(path) {
  const parts = String(path ?? "").split(/[\\/]+/).filter(Boolean);
  return parts.at(-1) ?? "";
}

/** A batch's name: its folder (a playlist saves into a folder named after it), else its first video. */
export function batchTitle(jobs) {
  const folder = folderName(jobs[0]?.spec?.dest_dir);
  if (folder && !/^[A-Za-z]:$/.test(folder)) return folder;
  const first = jobs[0]?.spec?.title ?? "Playlist";
  return jobs.length > 1 ? `${first} and ${jobs.length - 1} more` : first;
}

function batchGroup(jobs) {
  const groups = new Set(jobs.map((job) => groupOf(job.state)));
  return GROUP_ORDER.find((group) => groups.has(group)) ?? JOB_GROUP.FINISHED;
}

const newest = (jobs) => Math.max(...jobs.map((job) => job.created_at ?? 0));
const byPlaylistIndex = (a, b) => (a.spec?.playlist_index ?? 0) - (b.spec?.playlist_index ?? 0) || (a.created_at ?? 0) - (b.created_at ?? 0);

/** The expanded list: [{group, label, items}] for the non-empty groups in order (running, waiting,
 * failed, finished), each item {type: "job", job} or {type: "batch", batch: {id, title, jobs, counts}},
 * newest first. A batch sits in the group of its most active job. */
export function groupJobs(jobs) {
  const batches = new Map();
  const items = [];
  for (const job of jobs) {
    const batchId = job.spec?.batch_id;
    if (batchId) {
      if (!batches.has(batchId)) batches.set(batchId, []);
      batches.get(batchId).push(job);
    } else {
      items.push({ type: "job", job, group: groupOf(job.state), created: job.created_at ?? 0 });
    }
  }
  for (const [id, members] of batches) {
    const sorted = [...members].sort(byPlaylistIndex);
    items.push({
      type: "batch",
      batch: { id, title: batchTitle(sorted), jobs: sorted, counts: batchCounts(sorted) },
      group: batchGroup(sorted),
      created: newest(sorted),
    });
  }
  return GROUP_ORDER.map((group) => ({
    group,
    label: GROUP_LABELS[group],
    items: items
      .filter((item) => item.group === group)
      .sort((a, b) => b.created - a.created)
      .map(({ group: _group, created: _created, ...item }) => item),
  })).filter((group) => group.items.length > 0);
}

// --- one job ---

const ERROR_ACTIONS = {
  [ERROR_ACTION.CHANGE_FOLDER]: ACTION.CHANGE_FOLDER,
  [ERROR_ACTION.CHECK_UPDATE]: ACTION.CHECK_UPDATE,
  [ERROR_ACTION.REINSTALL_FFMPEG]: ACTION.REINSTALL_FFMPEG,
};

/** The actions a job offers, in display order. canOpenFolder: the host can open a finished job's folder. */
export function jobActions(job, { canOpenFolder = false } = {}) {
  const group = groupOf(job.state);
  if (group === JOB_GROUP.RUNNING || group === JOB_GROUP.WAITING) return [ACTION.CANCEL];
  const actions = [];
  if (job.state === JOB_STATE.DONE) {
    if (canOpenFolder && job.file_path) actions.push(ACTION.OPEN_FOLDER);
    if (job.can_clear) actions.push(ACTION.CLEAR);
    return actions;
  }
  if (job.can_retry) actions.push(ACTION.RETRY);
  const extra = ERROR_ACTIONS[job.error?.action];
  if (job.state === JOB_STATE.FAILED && extra) actions.push(extra);
  if (job.can_clear) actions.push(ACTION.CLEAR);
  if (job.state === JOB_STATE.FAILED && job.error?.details) actions.push(ACTION.DETAILS);
  return actions;
}

/** The line under a job's title. */
export function jobStatusText(job) {
  switch (job.state) {
    case JOB_STATE.WAITING:
      return "Waiting";
    case JOB_STATE.INTERRUPTED:
      return "Interrupted, will resume";
    case JOB_STATE.FETCHING:
      return job.stage || "Starting…";
    case JOB_STATE.DOWNLOADING:
      return [formatPercent(job.percent), job.stage, formatSpeed(job.speed), formatEta(job.eta)].filter(Boolean).join(" · ")
        || "Downloading…";
    case JOB_STATE.PROCESSING:
      return job.stage || "Finishing…";
    case JOB_STATE.DONE:
      // The summary already says "saved without subtitles" when they were skipped (the server writes it).
      return ["Done", job.summary].filter(Boolean).join(" · ");
    case JOB_STATE.FAILED:
      return job.error?.message || "Something went wrong";
    case JOB_STATE.CANCELLED:
      return "Cancelled";
    default:
      return String(job.state ?? "");
  }
}

/** {value (0-100) | null, tone: null | "paused", indeterminate} for a job's bar; null when it has none. */
export function jobProgress(job) {
  switch (job.state) {
    case JOB_STATE.DOWNLOADING:
      return isNumber(job.percent)
        ? { value: Math.max(0, Math.min(100, job.percent)), tone: null, indeterminate: false }
        : { value: null, tone: null, indeterminate: true };
    case JOB_STATE.FETCHING:
    case JOB_STATE.PROCESSING:
      return { value: null, tone: null, indeterminate: true };
    case JOB_STATE.WAITING:
    case JOB_STATE.INTERRUPTED:
      return { value: isNumber(job.percent) ? job.percent : 0, tone: "paused", indeterminate: false };
    default:
      return null;
  }
}

// --- banners ---

/** Banners about the queue as a whole: [{kind, text, action: "resume" | "retry_setup" | null}], paused first. */
export function queueBanners(queue, status, { now = new Date() } = {}) {
  const banners = [];
  if (!queue) return banners;
  const updater = status?.updater ?? {};
  if (queue.paused) banners.push({ kind: "paused", text: "Downloads are paused", action: "resume" });
  if (queue.held) {
    const setup = updater.setup?.state;
    if (setup === "running") {
      banners.push({ kind: "setup", text: "Setting up Snag… downloads will start when it's ready", action: null });
    } else if (setup === "error") {
      banners.push({
        kind: "setup", text: updater.setup?.message || "Snag couldn't finish setting up.",
        action: updater.setup?.can_retry ? "retry_setup" : null,
      });
    } else if (updater.update?.state === "installing") {
      banners.push({ kind: "held", text: "Updating yt-dlp… downloads will continue when it's done", action: null });
    } else {
      banners.push({ kind: "held", text: "Downloads are on hold for a moment", action: null });
    }
  }
  if (queue.throttled) {
    const until = toDate(queue.throttled_until);
    const text = until && until > now
      ? `YouTube is slowing you down. Resuming at ${formatClock(until)}`
      : "YouTube is slowing you down. Downloading one at a time for now";
    banners.push({ kind: "throttled", text, action: null });
  }
  return banners;
}

// --- remembered expansion ---

const EXPANDED_KEY = "snag.queue.expanded";

/** Whether the queue was left expanded; false when storage is missing or refuses. */
export function loadExpanded(storage) {
  try {
    return storage?.getItem(EXPANDED_KEY) === "1";
  } catch {
    return false;
  }
}

export function saveExpanded(storage, expanded) {
  try {
    storage?.setItem(EXPANDED_KEY, expanded ? "1" : "0");
  } catch {
    // Private windows and blocked storage: the bar just starts collapsed next time.
  }
}

// --- what a job row's tool actions report ---

/** The toast after Check for update, from the Status POST /api/update/check returns: {text, kind}. */
export function updateCheckMessage(status) {
  const update = status?.updater?.update;
  if (!update) return { text: "Checked for a yt-dlp update. See Settings", kind: "info" };
  if (update.error) return { text: `Couldn't check for an update: ${update.error}`, kind: "error" };
  if (update.available) return { text: `yt-dlp ${update.available} is available. Update it in Settings`, kind: "info" };
  return { text: update.current ? `yt-dlp is up to date (${update.current})` : "yt-dlp is up to date", kind: "success" };
}

/** The toast after Reinstall, from POST /api/tools/{tool}/reinstall's {started}: started is false when
 * a reinstall was already running. label: "ffmpeg" | "Deno". */
export function reinstallMessage(result, label) {
  return result?.started === false ? `${label} is already being reinstalled` : `Reinstalling ${label}…`;
}
