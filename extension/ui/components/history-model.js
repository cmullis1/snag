// Pure rules behind the History tab: the search and kind filter, paging with Load more, and each row's
// text. No DOM. Entries are the server's HistoryEntry JSON (see fields.js); GET /api/history answers
// {entries, total}, newest first.

import { formatDate, formatSize } from "../format.js";

export const PAGE_SIZE = 50;
const MAX_LIMIT = 500; // the server's most per request

export const KIND_FILTERS = Object.freeze([
  { value: null, label: "All" },
  { value: "video", label: "Video" },
  { value: "audio", label: "Audio" },
]);

/** search: the trimmed search text; kind: null | "video" | "audio"; loaded: the first page arrived. */
export function initialHistory() {
  return { search: "", kind: null, entries: [], total: 0, loaded: false };
}

/** The state for another search or filter: the list starts over. The same state when nothing changed. */
export function withQuery(state, { search = state.search, kind = state.kind } = {}) {
  const text = String(search ?? "").trim();
  if (text === state.search && kind === state.kind) return state;
  return { ...initialHistory(), search: text, kind };
}

/** The GET /api/history query for the first page, or (more) the page after what's shown. */
export function historyRequest(state, { more = false } = {}) {
  return { search: state.search, kind: state.kind, limit: PAGE_SIZE, offset: more ? state.entries.length : 0 };
}

/** Reload what's shown (after a download finishes or an entry changes): from the top, as many as are
 * shown, at least a page. */
export function refreshRequest(state) {
  return {
    search: state.search, kind: state.kind, limit: Math.min(MAX_LIMIT, Math.max(PAGE_SIZE, state.entries.length)), offset: 0,
  };
}

/** The state after a page arrived: a first page (or a refresh) replaces the list, Load more appends,
 * skipping entries already shown (a download finishing meanwhile moves everything down a place). */
export function applyPage(state, page, { more = false } = {}) {
  const entries = Array.isArray(page?.entries) ? page.entries : [];
  const total = Number.isInteger(page?.total) && page.total >= 0 ? page.total : entries.length;
  if (!more) return { ...state, entries: [...entries], total, loaded: true };
  const shown = new Set(state.entries.map((entry) => entry.id));
  return { ...state, entries: [...state.entries, ...entries.filter((entry) => !shown.has(entry.id))], total, loaded: true };
}

export function removeEntry(state, id) {
  if (!state.entries.some((entry) => entry.id === id)) return state;
  return { ...state, entries: state.entries.filter((entry) => entry.id !== id), total: Math.max(0, state.total - 1) };
}

export function canLoadMore(state) {
  return state.loaded && state.entries.length < state.total;
}

/** "Showing 50 of 120"; empty before the first page or when nothing is shown. */
export function pagingText(state) {
  return state.loaded && state.entries.length ? `Showing ${state.entries.length} of ${state.total}` : "";
}

/** {title, when, detail, failed, canOpen}: "Title · Today 14:02" over "1080p MP4 · 218 MB", or
 * "Failed · <message>" for a failed download (which has no file to show). */
export function rowText(entry, { now = new Date() } = {}) {
  const failed = entry.status === "failed";
  const detail = failed
    ? ["Failed", entry.error_message].filter(Boolean).join(" · ")
    : [entry.format_summary, formatSize(entry.size)].filter(Boolean).join(" · ");
  return {
    title: entry.title || "Untitled",
    when: formatDate(entry.finished_at, { now }),
    detail,
    failed,
    canOpen: !failed && Boolean(entry.file_path),
  };
}

/** Why the list is empty. */
export function emptyText(state) {
  const what = state.kind ? `${state.kind} downloads` : "downloads";
  if (state.search) return `No ${what} match “${state.search}”.`;
  if (state.kind) return `No ${what} yet.`;
  return "Nothing downloaded yet. Finished downloads show up here.";
}
