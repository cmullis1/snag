// The slim queue bar at the bottom of the download screen: "2 downloading · 1 waiting", a thin combined
// progress bar and a chevron. Expanded (remembered in localStorage), it lists every job grouped by state
// (downloading, waiting, failed, finished; newest first), playlists as one batch row each, with
// "Clear finished". Queue banners (paused, held, throttled) sit above it.
//
//   const bar = createQueueBar({api, store, pickFolder, toast, openFolder, storage, now});
//   document.body.append(bar.element);  ...  bar.destroy();
//
// store: createStore() fed by the hub (its jobs, queue and status); pickFolder(initial) → path | null
// (defaults to api.pickFolder); openFolder(job): opens a finished job's folder (defaults to
// api.openJobFolder); storage: defaults to localStorage when the page may use it.
//
// The summary inside the toggle isn't a live region (a button's name is re-read on every change); a
// separate status region announces the summary when its counts change, not on every progress tick.
// Rows are updated in place and moved only when their order changes, so focus stays on a row's button.
// When focus has nowhere to stay, it moves to a neighbour: a row that leaves gives it to the next row
// (or the one before); Clear finished, a banner's button or the toggle hiding give it to the first row,
// else the toggle, else the bar itself (focusable from script only) once the queue is empty.

import { busy } from "./busy.js";
import { h, setText, svg } from "./el.js";
import { uniqueId } from "./ids.js";
import { createBanner } from "./banner.js";
import { createBatchRow, createJobRow, progressBar, setProgress } from "./job-row.js";
import { focusWithin, hiddenWithin, keepFocus, neighbourOf, placeChildren } from "./reconcile.js";
import {
  combinedProgress, groupJobs, loadExpanded, queueBanners, queueCounts, queueSummaryText, saveExpanded,
} from "./queue-model.js";
import { jobList } from "../store.js";

function pageStorage() {
  try {
    return globalThis.localStorage ?? null;
  } catch {
    return null;
  }
}

function chevron() {
  return svg("svg", { class: "icon queue-chevron", viewBox: "0 0 16 16", "aria-hidden": "true" },
    svg("path", { d: "M4 10l4-4 4 4" }));
}

export function createQueueBar({
  api, store, pickFolder = null, toast = null, openFolder = null, storage = pageStorage(), now = () => new Date(),
}) {
  const picker = pickFolder ?? (async (initial) => (await api.pickFolder(initial))?.path ?? null);
  const opener = openFolder ?? (api.openJobFolder ? (job) => api.openJobFolder(job.id) : null);
  let expanded = loadExpanded(storage);
  const listId = uniqueId("queue-list");
  const rows = new Map(); // "job:<id>" | "batch:<id>" -> row component
  const groupNodes = new Map(); // group -> {section, list}
  const sectionLists = new Map(); // a group's section -> its list

  const banner = createBanner({ onResume: () => api.resumeQueue(), onRetrySetup: () => api.retrySetup(), toast });
  const summary = h("span", { class: "queue-summary" });
  const announcer = h("span", { class: "visually-hidden", role: "status" });
  let announced = null; // the summary last announced; null until the first render
  const progress = progressBar("All downloads");
  const toggle = h("button", {
    type: "button", class: "queue-toggle", "aria-controls": listId,
    onClick: () => setExpanded(!expanded),
  }, summary, progress, chevron());
  const clearFinished = h("button", {
    type: "button", class: "btn btn-ghost btn-sm",
    onClick: async () => {
      try {
        await busy(clearFinished, () => api.clearFinished());
      } catch (error) {
        toast?.show(error?.message || "Couldn't clear the finished downloads", { kind: "error" });
      }
    },
  }, "Clear finished");
  const groupsBox = h("div", { class: "queue-groups" });
  const panel = h("div", { id: listId, class: "queue-panel" },
    h("div", { class: "queue-panel-head spread" }, h("h2", { class: "queue-heading" }, "Downloads"), clearFinished),
    groupsBox);
  const element = h("section", { class: "queue-bar", "aria-label": "Downloads", tabindex: "-1" },
    banner.element, panel, toggle, announcer);

  function setExpanded(value) {
    expanded = value;
    saveExpanded(storage, value);
    toggle.setAttribute("aria-expanded", String(value));
    element.classList.toggle("is-expanded", value);
    panel.hidden = !value;
  }

  function groupNode(group, label) {
    let node = groupNodes.get(group);
    if (!node) {
      const headingId = uniqueId("queue-group");
      const list = h("ul", { class: "rows job-list", role: "list", "aria-labelledby": headingId });
      node = { section: h("div", { class: "queue-group" }, h("h3", { id: headingId, class: "queue-group-label" }, label), list), list };
      groupNodes.set(group, node);
      sectionLists.set(node.section, list);
    }
    return node;
  }

  function rowFor(item) {
    const key = item.type === "batch" ? `batch:${item.batch.id}` : `job:${item.job.id}`;
    let row = rows.get(key);
    if (row) row.update(item.type === "batch" ? item.batch : item.job);
    else {
      const deps = { api, pickFolder: picker, toast, openFolder: opener };
      row = item.type === "batch" ? createBatchRow({ ...deps, batch: item.batch }) : createJobRow({ ...deps, job: item.job });
      rows.set(key, row);
    }
    return { key, row };
  }

  function announce(text) {
    // The first summary is what the page shows on arrival, not news.
    if (announced !== null && text !== announced) setText(announcer, text);
    announced = text;
  }

  /** Focus for when its own place went: the first row shown, else the toggle, else the bar. */
  function focusFallback(groups) {
    const first = panel.hidden ? null : groups[0]?.rows[0]?.row;
    if (first) first.focus();
    else if (!toggle.hidden) toggle.focus();
    else element.focus();
  }

  function render(state) {
    const jobs = jobList(state);
    const counts = queueCounts(jobs);
    // Rows are updated first (a row moves focus between its own buttons), then focus is noted, before
    // anything it might be on hides or leaves.
    const groups = groupJobs(jobs).map((group) => ({
      node: groupNode(group.group, group.label),
      rows: group.items.map(rowFor),
    }));
    const seen = new Set(groups.flatMap((group) => group.rows.map(({ key }) => key)));
    const held = focusWithin(element);
    const shown = [...groupsBox.children].flatMap((section) => [...(sectionLists.get(section)?.children ?? [])]);
    const staying = new Set(groups.flatMap((group) => group.rows.map(({ row }) => row.element)));
    const neighbour = held ? neighbourOf(shown, held, (node) => staying.has(node)) : null;
    const leaving = held !== null && [...rows].some(([key, row]) => !seen.has(key) && row.element.contains(held));

    banner.update(queueBanners(state.queue, state.status, { now: now() }));
    const text = queueSummaryText(counts);
    setText(summary, text);
    announce(text);
    const value = combinedProgress(jobs);
    setProgress(progress, value === null ? null : { value, tone: state.queue?.paused ? "paused" : null, indeterminate: false });
    toggle.hidden = counts.total === 0;
    if (counts.total === 0 && expanded) panel.hidden = true;
    else panel.hidden = !expanded;
    clearFinished.hidden = counts.done + counts.cancelled === 0;

    keepFocus(element, () => {
      // Rows that left go first, so the ones staying aren't moved past them.
      for (const [key, row] of rows) {
        if (!seen.has(key)) {
          row.element.remove();
          rows.delete(key);
        }
      }
      placeChildren(groupsBox, groups.map((group) => group.node.section));
      for (const group of groups) placeChildren(group.node.list, group.rows.map(({ row }) => row.element));
    });

    const stayingRow = neighbour ? [...rows.values()].find((row) => row.element === neighbour) : null;
    if (stayingRow && !panel.hidden) stayingRow.focus();
    else if (leaving || (held && hiddenWithin(held, element))) focusFallback(groups);
  }

  setExpanded(expanded);
  const unsubscribe = store.subscribe((state) => render(state));
  render(store.getState());

  return {
    element,
    setExpanded,
    destroy() {
      unsubscribe();
    },
  };
}
