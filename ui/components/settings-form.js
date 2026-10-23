// The Settings tab: Folders (video, audio), One-click download (max quality, extras), Defaults for new
// downloads (extras), Downloads (at once, 1-10), and Snag (start with Windows, daily update check,
// yt-dlp version with Update / Check now, ffmpeg and Deno with Reinstall, YouTube login). Each field
// saves on its own (PUT /api/settings with just that field), shows "Saved" or the server's message
// beside it, and then shows the value the server saved (which can differ from what was sent: then it
// says "Windows didn't keep this" for Start with Windows, "Snag adjusted this" for any other field).
//
//   const form = createSettingsForm({api, store, toast, pickFolder, onSettings(settings), now});
//   container.append(form.element);  await form.load();  ...  form.destroy();
//
// store: createStore() fed by the hub (tool status, update progress, YouTube login, and settings changed
// elsewhere: the popup, another tab, a remembered folder; only the fields that changed are redrawn);
// onSettings: told the saved settings after each load, save and outside change (the Download tab uses
// them). Extras the server refuses open their editor with its message.
//
// An answer (to a PUT or a load) never overwrites settings newer than it: when a settings event from
// the store arrived after the request was sent, that event's settings stand, and the answer is used
// only to say "Saved". The server sends an event for every change, so the event for this save follows.

import { busy, holdWhileInert, isInert, setInert } from "./busy.js";
import { h, replace } from "./el.js";
import { uniqueId } from "./ids.js";
import { icon } from "./icons.js";
import { createExtrasEditor } from "./extras-editor.js";
import { createFolderLine } from "./folder-line.js";
import {
  HEIGHT_OPTIONS, MAX_CONCURRENT, MIN_CONCURRENT, changedFields, clampConcurrent, heightFromOption, loginText,
  optionOfHeight, saveErrorText, savedText, toolRow, ytdlpRow,
} from "./settings-model.js";
import { cleanExtras } from "../choice.js";
import { extrasSummary } from "../format.js";
import { isExtrasError } from "../fields.js";

const SAVED_FOR = 2500; // ms "Saved" stays
const EXTRAS_DELAY = 500; // ms after the last extras change before saving
const STEPPER_DELAY = 400;
const CLOCK_TICK = 30000; // ms between refreshes of "12 min ago"

export function createSettingsForm({
  api, store = null, toast = null, pickFolder = null, onSettings = () => {}, now = () => new Date(),
  setTimer = (fn, ms) => setTimeout(fn, ms), clearTimer = (id) => clearTimeout(id),
  setRepeat = (fn, ms) => setInterval(fn, ms), clearRepeat = (id) => clearInterval(id),
}) {
  const picker = pickFolder ?? (async (initial) => (await api.pickFolder(initial))?.path ?? null);
  let saved = null;
  let saveNumber = 0; // PUTs sent, in order
  let answeredNumber = 0; // the latest PUT whose answer was taken: an older answer arriving later is dropped
  let outsideChanges = 0; // settings the store brought that this form didn't save or load itself
  let storeSettings = store?.getState().settings ?? null;
  const unsaved = new Map(); // field -> its status, for a field the server refused (its draft is kept)
  const timers = new Set();
  const later = (fn, ms) => {
    const id = setTimer(() => {
      timers.delete(id);
      fn();
    }, ms);
    timers.add(id);
    return id;
  };
  const cancel = (id) => {
    if (id === null || id === undefined) return;
    clearTimer(id);
    timers.delete(id);
  };

  /** "Saving…" then "Saved" beside a section's heading, so nothing below moves while it shows. */
  function savedNote() {
    const node = h("span", { class: "settings-saved small muted", role: "status", "aria-live": "polite" });
    let clearing = null;
    return {
      node,
      show(text, { stay = false } = {}) {
        cancel(clearing);
        replace(node, text);
        if (!stay) clearing = later(() => replace(node), SAVED_FOR);
      },
      clear() {
        cancel(clearing);
        replace(node);
      },
    };
  }
  const notes = {
    folders: savedNote(), quick: savedNote(), defaults: savedNote(), downloads: savedNote(), snag: savedNote(),
  };

  /** A field's save status: progress and "Saved" go to its section's note; a refusal (the server's
   * message) shows under the field until the next save. */
  function fieldStatus(note) {
    const node = h("span", { class: "settings-status small danger-text", role: "alert" });
    return {
      node,
      saving() {
        replace(node);
        note.show("Saving…", { stay: true });
      },
      saved(text) {
        note.show(text);
      },
      failed(message) {
        note.clear();
        replace(node, message);
      },
      clear() {
        replace(node);
      },
    };
  }

  /** PUT just this field when it differs from what's saved. Returns true when saved (or unchanged).
   * onError(failure): told why the server refused it. */
  async function save(field, value, status, onError = null) {
    const changes = changedFields(saved, { [field]: value });
    if (!Object.keys(changes).length) {
      // Back to what's saved: nothing to send, and a refusal shown for this field no longer applies.
      status.clear();
      unsaved.delete(field);
      return true;
    }
    const number = ++saveNumber;
    const changesBefore = outsideChanges;
    status.saving();
    try {
      const answer = await api.updateSettings(changes);
      // Snag may save something other than what was sent (Start with Windows reports what Windows
      // will do, a folder comes back tidied): then the note says so instead of "Saved".
      const asSent = !Object.keys(changedFields({ [field]: value }, { [field]: answer?.[field] })).length;
      unsaved.delete(field);
      status.saved(savedText(field, asSent));
      if (outsideChanges !== changesBefore) {
        answeredNumber = Math.max(answeredNumber, number); // older answers are stale too
      } else if (number > answeredNumber) {
        answeredNumber = number;
        saved = answer;
        // Show what Snag saved when it differs from what was sent. When it matches, the field already
        // shows it (and may hold newer edits that aren't sent yet), so it's left alone.
        if (!asSent) apply(answer, [field]);
        onSettings(saved);
      }
      return true;
    } catch (failure) {
      unsaved.set(field, status);
      status.failed(saveErrorText(failure));
      onError?.(failure);
      return false;
    }
  }

  let firstHeading = null;
  const section = (title, hint, note, ...children) => {
    const headingId = uniqueId("settings-heading");
    const heading = h("h2", { id: headingId, class: "settings-heading", tabindex: "-1" }, title);
    firstHeading ??= heading;
    return h("section", { class: "settings-section", "aria-labelledby": headingId },
      h("div", { class: "settings-heading-line" }, heading, note.node),
      hint ? h("p", { class: "small muted" }, hint) : null,
      h("div", { class: "rows settings-rows" }, ...children));
  };
  const row = (...children) => h("div", { class: "row settings-row" }, ...children);

  // --- folders ---

  function folderField(field, label) {
    const status = fieldStatus(notes.folders);
    const line = createFolderLine({
      folder: "", label, pickFolder: (initial) => picker(initial),
      onChange: async (path) => {
        if (!(await save(field, path, status))) line.setFolder(saved?.[field] ?? "");
      },
    });
    return { line, status, element: row(h("div", { class: "settings-field" }, line.element, status.node)) };
  }
  const videoFolder = folderField("video_dir", "Video");
  const audioFolder = folderField("audio_dir", "Audio");

  // --- extras (one-click and defaults) ---

  function extrasField(field, label, note, name) {
    const status = fieldStatus(note);
    const editorId = uniqueId("settings-extras");
    const summary = h("span", { class: "panel-line-value" });
    let pending = null;
    const editor = createExtrasEditor({
      extras: null, info: null,
      onChange: (extras, { valid }) => {
        replace(summary, extrasSummary(extras));
        cancel(pending);
        if (!valid) {
          status.clear();
          return;
        }
        pending = later(() => save(field, cleanExtras(extras), status, (failure) => {
          if (!isExtrasError(failure)) return;
          setOpen(true);
          editor.showServerError(failure.message);
        }), EXTRAS_DELAY);
      },
    });
    const box = h("div", { id: editorId, class: "panel-editor settings-editor", hidden: true }, editor.element);
    const edit = h("button", {
      type: "button", class: "link-button", "aria-expanded": "false", "aria-controls": editorId, "aria-label": `Edit ${name}`,
      onClick: () => setOpen(edit.getAttribute("aria-expanded") !== "true"),
    }, "Edit");
    function setOpen(open) {
      edit.setAttribute("aria-expanded", String(open));
      replace(edit, open ? "Done" : "Edit");
      box.hidden = !open;
    }
    return {
      set(extras) {
        editor.setExtras(extras);
        replace(summary, extrasSummary(extras));
      },
      element: row(h("div", { class: "settings-field" },
        h("div", { class: "panel-line" }, h("span", { class: "muted" }, `${label}:`), summary, edit), box, status.node)),
    };
  }

  // --- one-click download ---

  const heightId = uniqueId("settings-height");
  const heightStatus = fieldStatus(notes.quick);
  const height = h("select", {
    id: heightId, class: "select settings-select",
    onChange: async () => {
      if (!(await save("quick_max_height", heightFromOption(height.value), heightStatus))) {
        height.value = optionOfHeight(saved?.quick_max_height);
      }
    },
  }, HEIGHT_OPTIONS.map((option) => h("option", { value: optionOfHeight(option.value) }, option.label)));
  const quickExtras = extrasField("quick_extras", "Extras", notes.quick, "one-click extras");
  const defaultExtras = extrasField("default_extras", "Extras", notes.defaults, "default extras");

  // --- downloads at once ---

  const concurrentStatus = fieldStatus(notes.downloads);
  const concurrentLabelId = uniqueId("settings-concurrent");
  let concurrent = 5;
  let concurrentTimer = null;
  const count = h("output", { class: "stepper-value num", "aria-live": "polite", "aria-labelledby": concurrentLabelId });
  const minus = h("button", { type: "button", class: "btn btn-sm btn-icon", "aria-label": "Fewer at once", onClick: () => step(-1) }, icon("minus"));
  const plus = h("button", { type: "button", class: "btn btn-sm btn-icon", "aria-label": "More at once", onClick: () => step(1) }, icon("plus"));

  function showConcurrent(value) {
    concurrent = clampConcurrent(value);
    replace(count, String(concurrent));
    setInert(minus, concurrent <= MIN_CONCURRENT);
    setInert(plus, concurrent >= MAX_CONCURRENT);
  }

  function step(by) {
    if (isInert(by < 0 ? minus : plus)) return;
    showConcurrent(concurrent + by);
    cancel(concurrentTimer);
    concurrentTimer = later(async () => {
      if (!(await save("max_concurrent", concurrent, concurrentStatus))) showConcurrent(saved?.max_concurrent);
    }, STEPPER_DELAY);
  }

  // --- switches ---

  function switchField(field, label, hint) {
    const status = fieldStatus(notes.snag);
    const id = uniqueId("settings-switch");
    const input = h("input", {
      id, type: "checkbox", role: "switch", class: "switch",
      onChange: () => busy(input, async () => {
        if (!(await save(field, input.checked, status))) input.checked = Boolean(saved?.[field]);
      }),
    });
    holdWhileInert(input);
    return {
      input,
      element: row(h("div", { class: "settings-field" },
        h("div", { class: "spread" },
          h("label", { for: id, class: "settings-label" }, label, hint ? h("span", { class: "small muted settings-hint" }, hint) : null),
          input),
        status.node)),
    };
  }
  const startWithWindows = switchField("start_with_windows", "Start with Windows");
  const dailyCheck = switchField("check_updates_daily", "Check for yt-dlp updates daily");

  // --- tools ---

  function busyButton(label, run) {
    const button = h("button", {
      type: "button", class: "btn btn-sm",
      onClick: async () => {
        try {
          await busy(button, run);
        } catch (failure) {
          toast?.show(failure?.message || "Something went wrong", { kind: "error" });
        } finally {
          renderTools(store?.getState());
        }
      },
    }, label);
    return button;
  }

  const ytdlpText = h("span", { class: "settings-tool-name" });
  const ytdlpNote = h("span", { class: "settings-status small" });
  const update = busyButton("Update", () => api.installUpdate());
  update.classList.add("btn-primary");
  const check = busyButton("Check now", () => api.checkForUpdate());
  const ytdlpRowNode = row(h("div", { class: "settings-field" },
    h("div", { class: "spread" }, ytdlpText, h("span", { class: "cluster" }, update, check)),
    ytdlpNote));

  function reinstallRow(name) {
    const label = h("span", { class: "settings-tool-name" });
    const version = h("span", { class: "muted small truncate" });
    const note = h("span", { class: "settings-status small" });
    const button = busyButton("Reinstall", async () => {
      await api.reinstallTool(name);
    });
    return {
      name, label, version, note, button,
      element: row(h("div", { class: "settings-field" },
        h("div", { class: "spread" }, h("span", { class: "settings-tool" }, label, version), button), note)),
    };
  }
  const toolRows = [reinstallRow("ffmpeg"), reinstallRow("deno")];

  const login = h("span", { class: "small" });
  const loginRow = row(h("div", { class: "settings-field" },
    h("div", { class: "spread" }, h("span", { class: "settings-label" }, "YouTube login"), login)));

  function setNote(node, text, tone) {
    replace(node, text);
    node.classList.toggle("danger-text", tone === "danger");
    node.classList.toggle("success-text", tone === "success");
    node.classList.toggle("muted", !tone);
  }

  function renderTools(state) {
    const status = state?.status ?? {};
    const updates = state?.updates ?? {};
    const yt = ytdlpRow(status, updates, { now: now() });
    replace(ytdlpText, yt.text);
    ytdlpText.classList.toggle("danger-text", yt.missing);
    update.hidden = !yt.updateLabel;
    if (yt.updateLabel) replace(update, yt.updateLabel);
    const updating = status.updater?.update?.state === "installing";
    setInert(check, updating || status.updater?.update?.state === "checking");
    setNote(ytdlpNote, yt.note, yt.noteTone);
    for (const tool of toolRows) {
      const info = toolRow(status, updates, tool.name);
      replace(tool.label, info.label);
      replace(tool.version, info.text);
      tool.version.classList.toggle("danger-text", info.missing);
      tool.version.classList.toggle("muted", !info.missing);
      setInert(tool.button, updating || info.busy);
      tool.button.setAttribute("aria-label", `Reinstall ${info.label}`);
      setNote(tool.note, info.note, info.noteTone);
    }
    const signedIn = loginText(status.youtube_login, { now: now() });
    replace(login, signedIn.text);
    login.classList.toggle("muted", !signedIn.available);
  }

  // --- the form ---

  const formError = h("p", { class: "field-error", role: "alert" });
  const element = h("div", { class: "settings" },
    formError,
    section("Folders", null, notes.folders, videoFolder.element, audioFolder.element),
    section("One-click download", "Used by right-click and the YouTube button.", notes.quick,
      row(h("div", { class: "settings-field" },
        h("div", { class: "spread" }, h("label", { for: heightId, class: "settings-label" }, "Max quality"), height),
        heightStatus.node)),
      quickExtras.element),
    section("Defaults for new downloads", null, notes.defaults, defaultExtras.element),
    section("Downloads", null, notes.downloads,
      row(h("div", { class: "settings-field" },
        h("div", { class: "spread" },
          h("span", { id: concurrentLabelId, class: "settings-label" }, "At once"),
          h("span", { class: "stepper", role: "group", "aria-labelledby": concurrentLabelId }, minus, count, plus)),
        concurrentStatus.node))),
    section("Snag", null, notes.snag, startWithWindows.element, dailyCheck.element, ytdlpRowNode,
      ...toolRows.map((tool) => tool.element), loginRow));

  // Each field's control, given the settings.
  const appliers = {
    video_dir: (settings) => videoFolder.line.setFolder(settings.video_dir ?? ""),
    audio_dir: (settings) => audioFolder.line.setFolder(settings.audio_dir ?? ""),
    quick_max_height: (settings) => { height.value = optionOfHeight(settings.quick_max_height); },
    quick_extras: (settings) => quickExtras.set(settings.quick_extras),
    default_extras: (settings) => defaultExtras.set(settings.default_extras),
    max_concurrent: (settings) => showConcurrent(settings.max_concurrent),
    start_with_windows: (settings) => { startWithWindows.input.checked = Boolean(settings.start_with_windows); },
    check_updates_daily: (settings) => { dailyCheck.input.checked = Boolean(settings.check_updates_daily); },
  };

  /** Show the settings: every field, or only `fields` (those that changed). */
  function apply(settings, fields = Object.keys(appliers)) {
    for (const field of fields) appliers[field]?.(settings);
  }

  /** Settings changed elsewhere (the store's settings event): redraw only what differs from what this
   * form last saved or loaded, so a field being edited here isn't reset by its own save. */
  function follow(state) {
    const next = state?.settings;
    if (!next || !saved || next === saved) return;
    const changed = Object.keys(changedFields(saved, next));
    saved = next;
    if (!changed.length) return;
    apply(saved, changed);
    onSettings(saved);
  }

  /** Count settings the store brought that aren't this form's own (it passes those on via onSettings). */
  function countOutside(state) {
    const next = state?.settings ?? null;
    if (next === storeSettings) return;
    storeSettings = next;
    if (next && next !== saved) outsideChanges += 1;
  }

  const unsubscribe = store ? store.subscribe((state) => {
    renderTools(state);
    countOutside(state);
    follow(state);
  }) : () => {};
  const ticker = setRepeat(() => renderTools(store?.getState()), CLOCK_TICK);
  renderTools(store?.getState());
  showConcurrent(concurrent);

  return {
    element,
    /** Load the settings (again) and show what changed since, and the saved value of any field whose
     * draft the server refused. */
    async load() {
      const changesBefore = outsideChanges;
      try {
        const answer = await api.getSettings();
        // Settings the store brought meanwhile are newer than this answer.
        const next = outsideChanges !== changesBefore ? store.getState().settings : answer;
        replace(formError);
        const refused = [...unsaved.keys()];
        for (const status of unsaved.values()) status.clear();
        unsaved.clear();
        if (saved && !Object.keys(changedFields(saved, next)).length) {
          apply(saved, refused);
          return saved;
        }
        saved = next;
        apply(saved);
        onSettings(saved);
        return saved;
      } catch (failure) {
        replace(formError, failure?.message || "Couldn't load the settings");
        return null;
      }
    },
    /** Focus the first section's heading (after the Settings tab is opened). */
    focus() {
      firstHeading?.focus();
    },
    destroy() {
      unsubscribe();
      clearRepeat(ticker);
      for (const id of timers) clearTimer(id);
      timers.clear();
    },
  };
}
