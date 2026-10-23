// One job in the expanded queue: title, status line (stage, progress, speed, time left, or the error),
// a progress bar, and the actions its state offers (queue-model.js jobActions): Cancel, Retry, Change
// folder (picker, then retry into the new folder), Check for update, Reinstall ffmpeg, Clear, Details
// (the raw error text), Open folder. Plus a batch row for a playlist download: "40 of 42 done, 2 failed:
// Retry failed", expanding to its jobs.
//
//   const row = createJobRow({job, api, pickFolder, toast, openFolder});  row.update(job);
//   const batch = createBatchRow({batch, api, pickFolder, toast, openFolder});  batch.update(batch);
//
// openFolder(job): opens a finished job's folder; without it, done jobs have no Open folder button.
// Parts are updated in place, and only when their text changes, so a focused button survives progress
// updates; a batch's rows move only when their order changes.
//
// When a job's state changes its buttons (Retry becomes Cancel, Cancel becomes Open folder), focus on
// one of them moves to the new button for the same action, else the first new button, else the row's
// title. A batch's Retry failed hiding gives focus to Show videos; a video leaving the batch gives it
// to the next video (or the one before), else to the batch's buttons. row.focus() focuses a row's
// first button (its title when it has none).

import { busy } from "./busy.js";
import { h, replace, setText } from "./el.js";
import { uniqueId } from "./ids.js";
import {
  ACTION, batchSummaryText, combinedProgress, jobActions, jobProgress, jobStatusText, reinstallMessage, updateCheckMessage,
} from "./queue-model.js";
import { focusWithin, hiddenWithin, neighbourOf, placeChildren } from "./reconcile.js";
import { JOB_STATE } from "../fields.js";

const LABELS = {
  [ACTION.CANCEL]: "Cancel",
  [ACTION.RETRY]: "Retry",
  [ACTION.CHANGE_FOLDER]: "Change folder",
  [ACTION.CHECK_UPDATE]: "Check for update",
  [ACTION.REINSTALL_FFMPEG]: "Reinstall ffmpeg",
  [ACTION.CLEAR]: "Clear",
  [ACTION.DETAILS]: "Details",
  [ACTION.OPEN_FOLDER]: "Open folder",
};

/** A progress bar element; update it with setProgress. */
export function progressBar(label) {
  const bar = h("div", { class: "progress-bar" });
  const element = h("div", {
    class: "progress", role: "progressbar", "aria-label": label, "aria-valuemin": "0", "aria-valuemax": "100",
  }, bar);
  return element;
}

/** progress: {value, tone, indeterminate} or null (hidden). */
export function setProgress(element, progress) {
  element.hidden = !progress;
  if (!progress) return;
  element.classList.toggle("is-indeterminate", Boolean(progress.indeterminate));
  element.classList.toggle("is-paused", progress.tone === "paused");
  if (progress.indeterminate || progress.value === null) {
    element.removeAttribute("aria-valuenow");
  } else {
    const value = Math.round(progress.value);
    element.setAttribute("aria-valuenow", String(value));
    element.style.setProperty("--value", `${progress.value}%`);
  }
}

function runner(toast) {
  return async (button, work) => {
    try {
      await busy(button, work);
    } catch (error) {
      if (toast) toast.show(error?.message || "Something went wrong", { kind: "error" });
      else console.error("Snag:", error);
    }
  };
}

export function createJobRow({ job, api, pickFolder = null, toast = null, openFolder = null }) {
  let current = job;
  let actionKey = "";
  const run = runner(toast);
  const detailsId = uniqueId("job-details");

  // Focusable from script only: where focus goes when the row is left without buttons.
  const title = h("div", { class: "job-title truncate", dir: "auto", tabindex: "-1" });
  const status = h("div", { class: "job-status small" });
  const progress = progressBar("Progress");
  const actions = h("div", { class: "job-actions cluster" });
  const buttons = new Map(); // action -> button, for their job-specific labels
  const details = h("pre", { id: detailsId, class: "job-details mono", hidden: true });
  const element = h("li", { class: "job-row" },
    h("div", { class: "job-body" }, h("div", { class: "job-main" }, title, status, progress), actions),
    details);

  const handlers = {
    [ACTION.CANCEL]: () => api.cancelJob(current.id),
    [ACTION.RETRY]: () => api.retryJob(current.id),
    [ACTION.CHANGE_FOLDER]: async () => {
      if (!pickFolder) return;
      const path = await pickFolder(current.spec?.dest_dir ?? null);
      if (path) await api.retryJob(current.id, { destDir: path });
    },
    [ACTION.CHECK_UPDATE]: async () => {
      const { text, kind } = updateCheckMessage(await api.checkForUpdate());
      toast?.show(text, { kind });
    },
    [ACTION.REINSTALL_FFMPEG]: async () => {
      const result = await api.reinstallTool("ffmpeg");
      toast?.show(reinstallMessage(result, "ffmpeg"));
    },
    [ACTION.CLEAR]: () => api.clearJob(current.id),
    [ACTION.OPEN_FOLDER]: () => openFolder(current),
  };

  function actionButton(action) {
    if (action === ACTION.DETAILS) {
      const button = h("button", {
        type: "button", class: "btn btn-ghost btn-sm", "aria-expanded": String(!details.hidden), "aria-controls": detailsId,
        "aria-label": `${LABELS[action]}: ${current.spec?.title ?? "download"}`,
        onClick: () => {
          details.hidden = !details.hidden;
          button.setAttribute("aria-expanded", String(!details.hidden));
        },
      }, LABELS[action]);
      return button;
    }
    const primary = action === ACTION.RETRY;
    const button = h("button", {
      type: "button", class: ["btn", "btn-sm", primary ? "btn-secondary" : "btn-ghost", action === ACTION.CANCEL && "btn-danger"],
      "aria-label": `${LABELS[action]}: ${current.spec?.title ?? "download"}`,
      onClick: () => run(button, handlers[action]),
    }, LABELS[action]);
    return button;
  }

  function render() {
    const spec = current.spec ?? {};
    const name = spec.title ?? "download";
    setText(title, spec.title ?? "Download");
    if (title.title !== (spec.title ?? "")) title.title = spec.title ?? "";
    setText(status, jobStatusText(current));
    status.classList.toggle("danger-text", current.state === JOB_STATE.FAILED);
    status.classList.toggle("muted", current.state !== JOB_STATE.FAILED);
    setProgress(progress, jobProgress(current));
    if (progress.getAttribute("aria-label") !== `Progress: ${name}`) progress.setAttribute("aria-label", `Progress: ${name}`);
    element.dataset.state = current.state;

    const list = jobActions(current, { canOpenFolder: Boolean(openFolder) });
    const key = list.join(",");
    if (key !== actionKey) {
      actionKey = key;
      const held = focusWithin(actions);
      const heldAction = held ? [...buttons].find(([, button]) => button.contains(held))?.[0] : undefined;
      buttons.clear();
      replace(actions, list.map((action) => {
        const button = actionButton(action);
        buttons.set(action, button);
        return button;
      }));
      if (held) (buttons.get(heldAction) ?? firstButton() ?? title).focus();
    }
    for (const [action, button] of buttons) {
      const label = `${LABELS[action]}: ${name}`;
      if (button.getAttribute("aria-label") !== label) button.setAttribute("aria-label", label);
    }
    setText(details, current.error?.details ?? "");
    if (!list.includes(ACTION.DETAILS)) details.hidden = true;
  }

  function firstButton() {
    return [...buttons.values()].find((button) => !button.hidden) ?? null;
  }

  render();
  return {
    element,
    update(next) {
      current = next;
      render();
    },
    /** Focus the row's first button, or its title when it has none. */
    focus() {
      (firstButton() ?? title).focus();
    },
  };
}

export function createBatchRow({ batch, api, pickFolder = null, toast = null, openFolder = null }) {
  let current = batch;
  const run = runner(toast);
  const listId = uniqueId("batch-jobs");
  const rows = new Map();

  const title = h("div", { class: "job-title truncate", dir: "auto" });
  const status = h("div", { class: "job-status small muted" });
  const progress = progressBar("Playlist progress");
  const retryFailed = h("button", {
    type: "button", class: "btn btn-sm", onClick: () => run(retryFailed, () => api.retryFailed(current.id)),
  }, "Retry failed");
  const toggle = h("button", {
    type: "button", class: "btn btn-ghost btn-sm", "aria-expanded": "false", "aria-controls": listId,
    onClick: () => {
      const open = toggle.getAttribute("aria-expanded") !== "true";
      toggle.setAttribute("aria-expanded", String(open));
      replace(toggle, open ? "Hide videos" : "Show videos");
      list.hidden = !open;
    },
  }, "Show videos");
  const list = h("ul", { id: listId, class: "rows job-list batch-jobs", role: "list", hidden: true });
  const element = h("li", { class: "job-row batch-row" },
    h("div", { class: "job-body" },
      h("div", { class: "job-main" }, title, status, progress),
      h("div", { class: "job-actions cluster" }, retryFailed, toggle)),
    list);

  function focusBatch() {
    (retryFailed.hidden ? toggle : retryFailed).focus();
  }

  function render() {
    setText(title, current.title);
    if (title.title !== current.title) title.title = current.title;
    setText(status, batchSummaryText(current.counts));
    const value = combinedProgress(current.jobs);
    setProgress(progress, value === null ? null : { value, tone: null, indeterminate: false });

    const seen = new Set(current.jobs.map((job) => job.id));
    const nodes = current.jobs.map((job) => {
      let row = rows.get(job.id);
      if (row) row.update(job);
      else {
        row = createJobRow({ job, api, pickFolder, toast, openFolder });
        rows.set(job.id, row);
      }
      return row.element;
    });
    // Where focus goes if its button hides or its video leaves (found before either happens).
    const held = focusWithin(element);
    const staying = new Set(nodes);
    const neighbour = held ? neighbourOf([...list.children], held, (node) => staying.has(node)) : null;
    const leaving = held !== null && [...rows].some(([id, row]) => !seen.has(id) && row.element.contains(held));

    retryFailed.hidden = current.counts.failed === 0;
    for (const [id, row] of rows) {
      if (!seen.has(id)) {
        row.element.remove();
        rows.delete(id);
      }
    }
    placeChildren(list, nodes);

    if (neighbour) [...rows.values()].find((row) => row.element === neighbour)?.focus();
    else if (leaving || (held && hiddenWithin(held, element))) focusBatch();
  }

  render();
  return {
    element,
    update(next) {
      current = next;
      render();
    },
    /** Focus the batch's first button (Retry failed, else Show videos). */
    focus: focusBatch,
  };
}
