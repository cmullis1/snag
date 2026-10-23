// The playlist picker: the playlist's videos with checkboxes (thumbnail, "01 · Title", length, a
// "have it" mark for videos downloaded before, "in queue" for those in the queue this session; both
// start unticked), All / None, one quality for every video (chips), the
// extras, the folder ("<video or audio folder>\<playlist>"), and "Download N videos" →
// POST /api/jobs/batch.
//
//   const picker = createPlaylistPicker({api, url, info, settings, jobs, toast, pickFolder, onQueued(result), onBack, now});
//   container.append(picker.element);
//
// url: the address that was looked up, which the batch asks for (Snag reuses that lookup);
// info: the lookup's playlist JSON; settings: GET /api/settings (folders, default extras); jobs: the
// store's jobs when the picker opens (jobList);
// pickFolder(initial) → path | null (defaults to api.pickFolder); onQueued({batch_id, jobs}) after
// the batch was added; onBack: shows a Back link when given.

import { isInert, setInert } from "./busy.js";
import { h, replace, thumbnailUrl } from "./el.js";
import { uniqueId } from "./ids.js";
import { icon } from "./icons.js";
import { createExtrasEditor } from "./extras-editor.js";
import { createFolderLine } from "./folder-line.js";
import {
  DEFAULT_QUALITY, QUALITY_CHIPS, buildBatchRequest, countText, defaultSelection, downloadLabel, entryLabel, entryMeta,
  isAudioQuality, playlistFolder, queuedVideos, selectedIndexes,
} from "./playlist-model.js";
import { cleanExtras, validateExtras } from "../choice.js";
import { extrasSummary } from "../format.js";
import { isExtrasError } from "../fields.js";

const ARROWS = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 };

export function createPlaylistPicker({
  api, url, info, settings = null, jobs = [], toast = null, pickFolder = null, onQueued = null, onBack = null,
  now = () => new Date(),
}) {
  if (typeof url !== "string" || !url) throw new Error("createPlaylistPicker needs the looked-up address (url)");
  const entries = Array.isArray(info.entries) ? info.entries : [];
  const queued = queuedVideos(jobs, info.site);
  const picker = pickFolder ?? (async (initial) => (await api.pickFolder(initial))?.path ?? null);
  const state = {
    selection: defaultSelection(entries, queued),
    qualityId: DEFAULT_QUALITY,
    extras: cleanExtras(settings?.default_extras) ?? null,
    extrasValid: true,
    pickedBase: null,
    busy: false,
  };
  const audioOnly = () => isAudioQuality(state.qualityId);

  // --- head: title, counts, All / None ---

  const counts = h("span", { class: "picker-counts", "aria-live": "polite" });
  const all = h("button", { type: "button", class: "link-button", onClick: () => { if (!isInert(all)) selectAll(true); } }, "All");
  const none = h("button", { type: "button", class: "link-button", onClick: () => { if (!isInert(none)) selectAll(false); } }, "None");
  const back = onBack ? h("button", { type: "button", class: "link-button picker-back", onClick: onBack },
    icon("back"), h("span", null, "Back")) : null;
  const title = h("h2", { class: "picker-title", dir: "auto", tabindex: "-1" }, info.title || "Playlist");
  const head = h("div", { class: "picker-head" },
    back,
    title,
    h("p", { class: "picker-summary small muted" }, counts, " · ", all, " · ", none));

  // --- the videos ---

  const boxes = new Map(); // index -> checkbox
  const meta = { now: now(), queued };
  const list = h("ul", { class: "rows picker-list", role: "list", "aria-label": "Videos" },
    entries.map((entry) => {
      const box = h("input", {
        type: "checkbox", class: "picker-check", checked: state.selection.has(entry.index),
        onChange: () => {
          if (box.checked) state.selection.add(entry.index);
          else state.selection.delete(entry.index);
          refresh();
        },
      });
      boxes.set(entry.index, box);
      const details = entryMeta(entry, meta);
      const src = thumbnailUrl(entry.thumbnail);
      return h("li", null, h("label", { class: "row picker-row" },
        box,
        src ? h("img", {
          class: "picker-thumb", src, alt: "", loading: "lazy", referrerpolicy: "no-referrer",
          onError: (event) => { event.currentTarget.classList.add("is-broken"); },
        }) : h("span", { class: "picker-thumb is-broken", "aria-hidden": "true" }),
        h("span", { class: "row-main" },
          h("span", { class: "row-title picker-entry-title", dir: "auto" }, entryLabel(entry, entries.length)),
          h("span", { class: "row-meta" },
            details.duration,
            details.haveIt ? h("span", { class: "warning-text", title: details.haveItTitle },
              details.duration ? " · have it" : "have it") : null,
            details.haveIt ? h("span", { class: "visually-hidden" }, ` (${details.haveItTitle})`) : null,
            details.inQueue ? h("span", { class: "warning-text", title: "Already in Snag's queue" },
              details.duration || details.haveIt ? " · in queue" : "in queue") : null))));
    }));

  // --- quality chips ---

  const chips = new Map();
  const chipGroup = h("div", { class: "chips", role: "radiogroup", "aria-label": "Quality" },
    QUALITY_CHIPS.map((chip) => {
      const button = h("button", {
        type: "button", class: "chip", role: "radio",
        onClick: () => setQuality(chip.id),
        onKeydown: (event) => moveQuality(event, chip.id),
      }, chip.label);
      chips.set(chip.id, button);
      return button;
    }));

  // --- extras, folder, download ---

  const editorId = uniqueId("picker-extras");
  const extrasText = h("span", { class: "panel-line-value" });
  const editButton = h("button", {
    type: "button", class: "link-button", "aria-expanded": "false", "aria-controls": editorId,
    onClick: () => setEditorOpen(editButton.getAttribute("aria-expanded") !== "true"),
  }, "Edit");
  const editor = createExtrasEditor({
    extras: state.extras, info: null, audioOnly: audioOnly(),
    onChange: (extras, { valid }) => {
      state.extras = extras;
      state.extrasValid = valid;
      refresh();
    },
  });
  state.extras = editor.getExtras();
  state.extrasValid = editor.validation().valid;
  const editorBox = h("div", { id: editorId, class: "panel-editor", hidden: true }, editor.element);

  const folderLine = createFolderLine({
    folder: currentFolder(),
    pickFolder: () => picker(state.pickedBase || baseFolder()),
    onChange: (path) => {
      state.pickedBase = path;
      folderLine.setFolder(currentFolder());
    },
  });

  const downloadButton = h("button", { type: "button", class: "btn btn-primary picker-download", onClick: download });
  const error = h("p", { class: "field-error panel-error", role: "alert" });

  const element = h("section", { class: "picker", "aria-label": `Playlist: ${info.title || "Playlist"}` },
    head,
    entries.length ? list : h("p", { class: "note" }, "This playlist has no videos Snag can download."),
    h("hr", { class: "divider" }),
    h("div", { class: "picker-quality" }, h("span", { class: "picker-label", "aria-hidden": "true" }, "Quality"), chipGroup),
    h("div", { class: "panel-foot" },
      h("div", { class: "panel-lines" },
        h("div", { class: "panel-line" }, h("span", { class: "muted" }, "Extras:"), extrasText, editButton),
        editorBox,
        folderLine.element),
      downloadButton),
    error);

  function baseFolder() {
    return (audioOnly() ? settings?.audio_dir : settings?.video_dir) ?? "";
  }

  function currentFolder() {
    return playlistFolder({ settings, qualityId: state.qualityId, title: info.title, pickedBase: state.pickedBase });
  }

  function setEditorOpen(open) {
    editButton.setAttribute("aria-expanded", String(open));
    replace(editButton, open ? "Done" : "Edit");
    editorBox.hidden = !open;
  }

  function selectAll(on) {
    state.selection = on ? new Set(entries.map((entry) => entry.index)) : new Set();
    for (const [index, box] of boxes) box.checked = state.selection.has(index);
    refresh();
  }

  function setQuality(id) {
    if (id === state.qualityId) return;
    state.qualityId = id;
    editor.setAudioOnly(audioOnly());
    folderLine.setFolder(currentFolder());
    refresh();
  }

  function moveQuality(event, id) {
    let next = null;
    const ids = QUALITY_CHIPS.map((chip) => chip.id);
    if (event.key in ARROWS) next = ids[(ids.indexOf(id) + ARROWS[event.key] + ids.length) % ids.length];
    else if (event.key === "Home") next = ids[0];
    else if (event.key === "End") next = ids.at(-1);
    if (!next) return;
    event.preventDefault();
    setQuality(next);
    chips.get(next).focus();
  }

  function refresh() {
    const selected = selectedIndexes(entries, state.selection).length;
    replace(counts, countText(entries.length, selected));
    setInert(all, selected === entries.length);
    setInert(none, selected === 0);
    for (const [id, chip] of chips) {
      chip.setAttribute("aria-checked", String(id === state.qualityId));
      chip.setAttribute("tabindex", id === state.qualityId ? "0" : "-1");
    }
    replace(extrasText, extrasSummary(state.extras, { audioOnly: audioOnly() }));
    replace(downloadButton, state.busy ? "Adding…" : downloadLabel(selected));
    downloadButton.disabled = selected === 0;
    setInert(downloadButton, state.busy);
    if (state.extrasValid && error.dataset.source === "extras") {
      replace(error);
      error.dataset.source = "";
    }
  }

  async function download() {
    if (state.busy || isInert(downloadButton)) return;
    if (!validateExtras(state.extras).valid) {
      setEditorOpen(true);
      editor.revealErrors();
      replace(error, "Fix the extras above to download");
      error.dataset.source = "extras";
      return;
    }
    let body;
    try {
      body = buildBatchRequest({
        url, entries, selection: state.selection, qualityId: state.qualityId, extras: state.extras,
        pickedBase: state.pickedBase,
      });
    } catch (failure) {
      replace(error, failure.message);
      return;
    }
    state.busy = true;
    replace(error);
    error.dataset.source = "";
    refresh();
    try {
      const result = await api.createBatch(body);
      const count = result?.jobs?.length ?? body.entries.length;
      toast?.show(`Queued ${count === 1 ? "1 video" : `${count} videos`}`, { kind: "success" });
      onQueued?.(result);
    } catch (failure) {
      const message = failure?.message || "Snag couldn't add the downloads";
      replace(error, message);
      if (isExtrasError(failure)) {
        setEditorOpen(true);
        editor.showServerError(message);
      }
    } finally {
      state.busy = false;
      refresh();
    }
  }

  refresh();

  return {
    element,
    /** Focus the playlist's title (after "Choose videos" opens the picker). */
    focus() {
      title.focus();
    },
    /** The POST /api/jobs/batch body for the current choices; null when nothing is ticked. */
    getRequest() {
      try {
        return buildBatchRequest({
          url, entries, selection: state.selection, qualityId: state.qualityId, extras: state.extras,
          pickedBase: state.pickedBase,
        });
      } catch {
        return null;
      }
    },
  };
}
