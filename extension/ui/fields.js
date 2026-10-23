// The server's JSON as the UI relies on it, in one place. The server is the source of truth
// (snag/server/schemas.py and snag/server/hub.py); if a name there changes, change it here.
//
// WebSocket events (snag/server/hub.py; the updater's come from snag/updater/service.py):
//   {type: "snapshot", jobs: [Job], queue: Queue, status: Status, settings: Settings}   on connect
//   {type: "job", job: Job}                    a job was added or changed
//   {type: "job_removed", id}                  a job was cleared
//   {type: "queue", queue: Queue}              the queue changed
//   {type: "status", ...partial Status}        Status fields beside "type", merged into the status,
//                                              e.g. {type: "status", updater: {...}}
//   {type: "settings", settings: Settings}     the settings changed (a save, or a picked folder
//                                              remembered); Settings is GET /api/settings' whole object
//   {type: "setup",  tool, phase, done, total, message}   first-run install progress
//   {type: "update", tool, phase, done, total, message}   update / reinstall progress
//
// Job (snag.jobs.models.job_to_dict + schemas.job_json):
//   id, state, percent (0-100), speed (bytes/s), eta (s), stage, error: ErrorInfo | null,
//   file_path, used_cookies, refetched, subtitles_skipped, created_at, finished_at (wall-clock s),
//   kind ("video" | "audio"), summary, can_retry, can_clear,
//   spec: {url, title, site, video_id, choice, extras, dest_dir, channel, estimated_size,
//          batch_id, playlist_index, playlist_count, playlist_item}
// ErrorInfo (snag.errors.ErrorInfo.to_dict): kind, message, action, can_retry, keep_partial, details
// Queue (snag.jobs.queue.QueueState): paused, held, throttled, throttled_until (wall-clock s | null),
//   current_limit, running, waiting
// Lookup (schemas.video_info_json / playlist_info_json): {type: "video", site, video_id, url, title,
//   channel, duration, thumbnail, upload_date, formats: {video_simple, video_all, audio, audio_tracks:
//   [FormatRow]}, subtitle_langs, auto_subtitle_langs, has_chapters, playlist, live_status,
//   js_runtime_missing, duplicate: HistoryEntry | null} or {type: "playlist", site, playlist_id, url,
//   title, entries: [{index, video_id, url, title, duration, thumbnail, item_index, duplicate}]}
// FormatRow (snag.info.models.FormatRow): format_id, label, ext, vcodec, acodec, height, fps, abr, size,
//   size_is_estimate, audio_id, has_audio, language, is_original, tbr, dynamic_range, upscaled,
//   kind ("video" | "audio" | "mp3"), audio_ext, audio_codec, result_ext
// HistoryEntry (snag.store.history.HistoryEntry): id, site, video_id, url, title, channel, kind,
//   format_summary, file_path, size, finished_at (wall-clock s), status ("done" | "failed"), error_message
// Status (GET /api/status): version, tools: {ytdlp, ffmpeg, ffprobe, deno: {present, version, message},
//   ready} (schemas.tool_status_json), updater: {setup: {state, message, can_retry, tools:
//   {name: Progress}}, update: {current, latest, available, last_check, state, message, error}},
//   youtube_login (ISO time | null), queue: Queue (the store keeps the queue in state.queue only)
// Progress: phase (downloading | verifying | installing | waiting | done | error | null), done, total, message
// HTTP errors: {error: ErrorInfo-like {kind, message, action?, ...}} with a 4xx/5xx status; kind
//   "invalid_extras" with action "edit_extras" (snag/server/routes INVALID_EXTRAS, EDIT_EXTRAS) when a
//   download's or a setting's extras (trim times, SponsorBlock, ...) aren't valid, the message saying
//   which part to fix (POST /api/jobs, /api/jobs/batch, PUT /api/settings).

export const EVENT = Object.freeze({
  SNAPSHOT: "snapshot",
  JOB: "job",
  JOB_REMOVED: "job_removed",
  QUEUE: "queue",
  STATUS: "status",
  SETUP: "setup",
  UPDATE: "update",
  SETTINGS: "settings",
  // Not from the server: the hub's connection state, dispatched into the store by bindHub.
  CONNECTION: "connection",
});

// The hub's connection state (hub.js), mirrored into the store's `connection`.
export const CONNECTION = Object.freeze({
  CONNECTING: "connecting",
  CONNECTED: "connected",
  DISCONNECTED: "disconnected",
});

export const CONNECTION_STATES = Object.freeze(Object.values(CONNECTION));

export const JOB_STATE = Object.freeze({
  WAITING: "waiting",
  FETCHING: "fetching",
  DOWNLOADING: "downloading",
  PROCESSING: "processing",
  DONE: "done",
  FAILED: "failed",
  CANCELLED: "cancelled",
  INTERRUPTED: "interrupted",
});

export const ACTIVE_JOB_STATES = Object.freeze([JOB_STATE.FETCHING, JOB_STATE.DOWNLOADING, JOB_STATE.PROCESSING]);
export const UNFINISHED_JOB_STATES = Object.freeze([...ACTIVE_JOB_STATES, JOB_STATE.WAITING, JOB_STATE.INTERRUPTED]);
export const END_JOB_STATES = Object.freeze([JOB_STATE.DONE, JOB_STATE.FAILED, JOB_STATE.CANCELLED]);

// ErrorInfo.action (snag.errors.Action).
export const ERROR_ACTION = Object.freeze({
  RETRY: "retry",
  CHECK_UPDATE: "check_update",
  CHANGE_FOLDER: "change_folder",
  REINSTALL_FFMPEG: "reinstall_ffmpeg",
  EDIT_EXTRAS: "edit_extras",
});

// ApiError.kind when the server can't be reached at all (api.js).
export const OFFLINE = "offline";

// ApiError.kind when the server refused the extras (400, action "edit_extras"): the extras editor opens
// and shows its message.
export const INVALID_EXTRAS = "invalid_extras";

/** Whether an API error is the server refusing the extras, so the extras editor should show it. */
export function isExtrasError(error) {
  return error?.kind === INVALID_EXTRAS || error?.action === ERROR_ACTION.EDIT_EXTRAS;
}

const isObject = (value) => value !== null && typeof value === "object" && !Array.isArray(value);

const isId = (value) => typeof value === "string" && value !== "";

/** The job inside a job event; null when the event is malformed (no job object with a text id). */
export function jobFromEvent(event) {
  return isObject(event.job) && isId(event.job.id) ? event.job : null;
}

/** The queue inside a queue event; null when the event has none. */
export function queueFromEvent(event) {
  return isObject(event.queue) ? event.queue : null;
}

/** The partial Status a status event carries: its own fields beside "type". */
export function statusFromEvent(event) {
  const { type: _type, ...status } = event;
  return status;
}

/** The settings inside a settings event or a snapshot; null when the event has none. */
export function settingsFromEvent(event) {
  return isObject(event.settings) ? event.settings : null;
}

/** The cleared job's id; null when the event has none. */
export function removedIdFromEvent(event) {
  return isId(event.id) ? event.id : null;
}

/** {tool, progress} from a setup or update event; tool is null when the event names none. */
export function progressFromEvent(event) {
  const { type: _type, tool, ...progress } = event;
  return { tool: isId(tool) ? tool : null, progress };
}

/** The per-tool setup progress inside a status, or null when it has none. */
export function setupToolsFromStatus(status) {
  const tools = status?.updater?.setup?.tools;
  return isObject(tools) ? tools : null;
}
