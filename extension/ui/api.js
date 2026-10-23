// One function per HTTP endpoint of Snag's local server (Plan 2a Task 6, snag/server/routes/).
// Request bodies use the server's JSON names. Failures throw ApiError:
//   - the server answered with an error: its {error: {...}} object's message, kind and action
//   - the server can't be reached: kind "offline", message "Snag isn't running"
// An aborted request rejects with its signal's reason (an AbortError, a TimeoutError from
// AbortSignal.timeout(), or whatever abort(reason) was given), never as offline.

import { OFFLINE } from "./fields.js";

export class ApiError extends Error {
  /** error: the server's error object (kind, message, action, can_retry, details…), when it sent one. */
  constructor(message, kind = "unknown", action = null, { status = null, error = null } = {}) {
    super(message);
    this.name = "ApiError";
    this.kind = kind;
    this.action = action;
    this.status = status;
    this.error = error;
  }
}

const isObject = (value) => value !== null && typeof value === "object" && !Array.isArray(value);

function offline() {
  return new ApiError("Snag isn't running", OFFLINE);
}

function queryString(params) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== null && value !== undefined && value !== "") search.append(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

function failure(status, data) {
  const error = isObject(data) && isObject(data.error) ? data.error : null;
  if (error && typeof error.message === "string" && error.message) {
    return new ApiError(error.message, error.kind ?? "http", error.action ?? null, { status, error });
  }
  const detail = isObject(data) && typeof data.detail === "string" && data.detail ? data.detail : null;
  return new ApiError(detail ?? `Snag couldn't do that (error ${status})`, "http", null, { status, error });
}

const id = (value) => encodeURIComponent(String(value));

/** baseUrl: "" for the page's own origin (the full tab), or "http://127.0.0.1:47823" (the popup).
 * fetch: injectable for tests. */
export function createApi(baseUrl = "", { fetch = (...args) => globalThis.fetch(...args) } = {}) {
  const base = baseUrl.replace(/\/+$/, "");

  async function request(method, path, { body, query, signal } = {}) {
    const init = { method, headers: { Accept: "application/json" } };
    if (body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    if (signal) init.signal = signal;
    let response;
    let text;
    try {
      response = await fetch(`${base}${path}${query ? queryString(query) : ""}`, init);
      text = await response.text();
    } catch (error) {
      // The caller gave up (abort(), abort(reason), AbortSignal.timeout()): pass that on as it is,
      // whatever error fetch chose to report it with.
      if (signal?.aborted) throw signal.reason ?? error;
      if (error?.name === "AbortError") throw error;
      throw offline();
    }
    let data = null;
    if (text) {
      try {
        data = JSON.parse(text);
      } catch {
        data = null;
      }
    }
    if (!response.ok) throw failure(response.status, data);
    return data;
  }

  const post = (path, body, options) => request("POST", path, { ...options, body });
  const withCookies = (fields, cookies) => (cookies ? { ...fields, cookies } : fields);

  return {
    // Lookups and downloads
    lookup: (url, { cookies, signal } = {}) => post("/api/lookup", withCookies({ url }, cookies), { signal }),
    /** body: {url, format_id, audio_id?, extras?, dest_dir?, cookies?} */
    createJob: (body) => post("/api/jobs", body),
    quickJob: (url, { cookies } = {}) => post("/api/jobs/quick", withCookies({ url }, cookies)),
    /** body: {url, entries: [index…], quality: {kind: "quick", height} | {kind: "mp3"} | {kind: "best_audio"},
     * extras?, dest_dir?} */
    createBatch: (body) => post("/api/jobs/batch", body),

    // Jobs and batches
    listJobs: () => request("GET", "/api/jobs"),
    cancelJob: (jobId) => post(`/api/jobs/${id(jobId)}/cancel`),
    retryJob: (jobId, { destDir } = {}) => post(`/api/jobs/${id(jobId)}/retry`, destDir ? { dest_dir: destDir } : {}),
    clearJob: (jobId) => post(`/api/jobs/${id(jobId)}/clear`),
    clearFinished: () => post("/api/jobs/clear-finished"),
    /** Explorer with a finished job's file selected → {opened: path} */
    openJobFolder: (jobId) => post(`/api/jobs/${id(jobId)}/open-folder`),
    getBatch: (batchId) => request("GET", `/api/batches/${id(batchId)}`),
    retryFailed: (batchId) => post(`/api/batches/${id(batchId)}/retry-failed`),

    // Queue
    pauseQueue: () => post("/api/queue/pause"),
    resumeQueue: () => post("/api/queue/resume"),
    getQueue: () => request("GET", "/api/queue"),

    // History
    /** → {entries, total} */
    listHistory: ({ search, kind, limit, offset } = {}) =>
      request("GET", "/api/history", { query: { search, kind, limit, offset } }),
    removeHistoryEntry: (entryId) => request("DELETE", `/api/history/${id(entryId)}`),
    clearHistory: () => request("DELETE", "/api/history"),
    openHistoryFolder: (entryId) => post(`/api/history/${id(entryId)}/open-folder`),

    // Settings
    getSettings: () => request("GET", "/api/settings"),
    /** changes: just the fields to change, e.g. {max_concurrent: 3} */
    updateSettings: (changes) => request("PUT", "/api/settings", { body: changes }),

    // System
    sendCookies: (url, cookies) => post("/api/cookies", { url, cookies }),
    getStatus: () => request("GET", "/api/status"),
    /** → {path: text | null} (null when the dialog was cancelled) */
    pickFolder: (initial) => post("/api/pick-folder", initial ? { initial } : {}),
    checkForUpdate: () => post("/api/update/check"),
    installUpdate: () => post("/api/update/install"),
    /** tool: "ffmpeg" | "deno" */
    reinstallTool: (tool) => post(`/api/tools/${id(tool)}/reinstall`),
    /** First-run setup again, after it failed → {started, status} */
    retrySetup: () => post("/api/setup/retry"),
  };
}
