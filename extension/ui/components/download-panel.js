// The download screen for one looked-up video, shared by the full tab and the extension popup:
// thumbnail, title, "channel · length · already downloaded …", notes (live stream, Deno missing, already
// in the queue, part of a playlist), the Video / Audio only switch, the format rows, then the one-line
// extras ("Edit" opens the editor) and folder lines beside the Download button.
//
//   const panel = createDownloadPanel({api, store, url, info, settings, compact, toast, onQueued(job),
//                                      onChoosePlaylist(playlistRef), pickFolder, cookies, now});
//   container.append(panel.element);  ...  panel.setPlaylistName(name);  ...  panel.destroy();
//
// api: createApi(); store: createStore() (for "already in the queue", and newer settings; optional);
// url: the address that was looked up, which the download asks for (Snag reuses that lookup and
// queues the job with its page URL; asking for info.url instead would look up again, since
// watch?v=x&list=PL1 and x's own page are different lookups); info: the lookup's video JSON;
// settings: GET /api/settings (folders and default extras; the store's settings replace them as they
// change, so the folder line follows a changed default folder); toast: createToaster() (optional);
// pickFolder(initial) → path | null (defaults to api.pickFolder); cookies: passed on to POST /api/jobs
// (the popup's sign-in handoff); compact: the popup's 380 px layout.

import { busy, isInert, setInert } from "./busy.js";
import { h, replace, setText, thumbnailUrl } from "./el.js";
import { uniqueId } from "./ids.js";
import { createFormatList } from "./format-list.js";
import { createExtrasEditor } from "./extras-editor.js";
import { createFolderLine } from "./folder-line.js";
import {
  MODE, buildJobRequest, cleanExtras, defaultFolder, defaultRowId, findRow, isLive, lookupNotes, metaLine,
  queuedJobFor, simpleRows, validateExtras,
} from "../choice.js";
import { reinstallMessage } from "./queue-model.js";
import { extrasSummary } from "../format.js";
import { isExtrasError } from "../fields.js";

function defaultPicker(api) {
  return async (initial) => (await api.pickFolder(initial))?.path ?? null;
}

export function createDownloadPanel({
  api, store = null, url, info, settings: initialSettings = null, compact = false, toast = null, onQueued = null, onChoosePlaylist = null,
  pickFolder = null, cookies = null, now = () => new Date(),
}) {
  if (typeof url !== "string" || !url) throw new Error("createDownloadPanel needs the looked-up address (url)");
  const formats = info.formats ?? {};
  let settings = store?.getState().settings ?? initialSettings;
  const hasVideo = simpleRows(formats, MODE.VIDEO).length > 0;
  const state = {
    mode: hasVideo ? MODE.VIDEO : MODE.AUDIO,
    selectedId: null,
    audioTrackId: null,
    expanded: false,
    extras: cleanExtras(settings?.default_extras) ?? null,
    extrasValid: true,
    pickedFolder: null,
    busy: false,
  };
  state.selectedId = defaultRowId(formats, state.mode);
  const live = isLive(info);
  const audioOnly = () => state.mode === MODE.AUDIO;

  // --- head ---

  const meta = metaLine(info, { now: now() });
  const thumbnail = thumbnailUrl(info.thumbnail); // anything but https: or data:image/: no image
  const head = h("div", { class: "panel-head" },
    thumbnail ? h("img", {
      class: "panel-thumb", src: thumbnail, alt: "", loading: "lazy", referrerpolicy: "no-referrer",
      onError: (event) => { event.currentTarget.hidden = true; },
    }) : null,
    h("div", { class: "panel-heading" },
      h("h2", { class: "panel-title", title: info.title, dir: "auto" }, info.title),
      h("p", { class: "panel-meta small" },
        // Each part isolated, so a right-to-left channel name doesn't reorder the length beside it.
        h("span", { class: "muted" }, meta.parts.map((part, index) => [index ? " · " : null, h("bdi", null, part)])),
        meta.duplicate ? [meta.parts.length ? h("span", { class: "muted" }, " · ") : null,
          h("span", { class: "warning-text" }, meta.duplicate)] : null)));

  const notes = lookupNotes(info).map((note) => {
    if (note.kind !== "deno") return h("p", { class: "note note-warning", role: "note" }, note.text);
    const reinstall = h("button", {
      type: "button", class: "btn btn-sm",
      onClick: async () => {
        try {
          await busy(reinstall, async () => {
            const result = await api.reinstallTool("deno");
            toast?.show(reinstallMessage(result, "Deno"));
          });
        } catch (error) {
          toast?.show(error.message, { kind: "error" });
        }
      },
    }, "Reinstall Deno");
    return h("div", { class: "note note-warning spread" }, h("span", null, note.text), reinstall);
  });
  const queuedNote = h("p", { class: "note note-accent", hidden: true });
  const playlistText = h("span", { class: "panel-playlist-text" });
  const playlistNote = info.playlist && onChoosePlaylist
    ? h("p", { class: "note spread" }, playlistText,
      h("button", { type: "button", class: "link-button", onClick: () => onChoosePlaylist(info.playlist) }, "Choose videos"))
    : null;
  setPlaylistName(null);

  // --- Video / Audio only ---

  const modeButtons = {};
  const modeSwitch = h("div", { class: "segmented panel-mode", role: "radiogroup", "aria-label": "Download" },
    [[MODE.VIDEO, "Video"], [MODE.AUDIO, "Audio only"]].map(([mode, text]) => {
      const button = h("button", {
        type: "button", role: "radio", "aria-checked": String(state.mode === mode), tabindex: state.mode === mode ? "0" : "-1",
        disabled: mode === MODE.VIDEO && !hasVideo,
        onClick: () => setMode(mode),
        onKeydown: (event) => {
          if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) return;
          event.preventDefault();
          const next = state.mode === MODE.VIDEO ? MODE.AUDIO : MODE.VIDEO;
          if (next === MODE.VIDEO && !hasVideo) return;
          setMode(next);
          modeButtons[next].focus();
        },
      }, text);
      modeButtons[mode] = button;
      return button;
    }));

  // --- formats ---

  const formatList = createFormatList({
    info, mode: state.mode, selectedId: state.selectedId, compact,
    onSelect: (id) => {
      state.selectedId = id;
      formatList.update({ selectedId: id });
      refresh();
    },
    onAudioTrack: (id) => {
      state.audioTrackId = id;
      formatList.update({ audioTrackId: id });
    },
    onToggleExpanded: (expanded) => {
      state.expanded = expanded;
      formatList.update({ expanded });
    },
  });

  // --- extras, folder, download ---

  const editorId = uniqueId("extras-editor");
  const extrasText = h("span", { class: "panel-line-value" });
  const editButton = h("button", {
    type: "button", class: "link-button", "aria-expanded": "false", "aria-controls": editorId,
    onClick: () => setEditorOpen(editButton.getAttribute("aria-expanded") !== "true"),
  }, "Edit");
  const editor = createExtrasEditor({
    extras: state.extras, info, audioOnly: audioOnly(), compact,
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
    folder: defaultFolder(settings, state.mode),
    pickFolder: pickFolder ?? defaultPicker(api),
    onChange: (path) => { state.pickedFolder = path; },
  });

  const downloadButton = h("button", { type: "button", class: "btn btn-primary panel-download", onClick: download }, "Download");
  const error = h("p", { class: "field-error panel-error", role: "alert" });

  const element = h("section", { class: ["panel", compact && "panel-compact"], "aria-label": info.title },
    head, notes, queuedNote, playlistNote,
    h("div", { class: "panel-switch" }, modeSwitch),
    formatList.element,
    h("hr", { class: "divider" }),
    h("div", { class: "panel-foot" },
      h("div", { class: "panel-lines" },
        h("div", { class: "panel-line" }, h("span", { class: "muted" }, "Extras:"), extrasText, editButton),
        editorBox,
        folderLine.element),
      downloadButton),
    error);

  /** The playlist line's text: "From playlist <name>" (the name isolated, so a right-to-left one
   * doesn't reorder the words around it), or "This video is part of a playlist" without a name. */
  function setPlaylistName(name) {
    const text = typeof name === "string" ? name.trim() : "";
    if (text) replace(playlistText, "From playlist ", h("bdi", { class: "panel-playlist-name" }, text));
    else replace(playlistText, "This video is part of a playlist");
  }

  function setEditorOpen(open) {
    editButton.setAttribute("aria-expanded", String(open));
    replace(editButton, open ? "Done" : "Edit");
    editorBox.hidden = !open;
  }

  function setMode(mode) {
    if (mode === state.mode) return;
    state.mode = mode;
    state.selectedId = defaultRowId(formats, mode);
    state.audioTrackId = null;
    for (const [key, button] of Object.entries(modeButtons)) {
      button.setAttribute("aria-checked", String(key === mode));
      button.setAttribute("tabindex", key === mode ? "0" : "-1");
    }
    formatList.update({ mode, selectedId: state.selectedId, audioTrackId: null });
    editor.setAudioOnly(audioOnly());
    if (!state.pickedFolder) folderLine.setFolder(defaultFolder(settings, mode));
    refresh();
  }

  function refresh() {
    replace(extrasText, extrasSummary(state.extras, { audioOnly: audioOnly() }));
    const row = findRow(formats, state.selectedId);
    downloadButton.disabled = live || !row;
    setInert(downloadButton, state.busy);
    replace(downloadButton, state.busy ? "Adding…" : "Download");
    if (state.extrasValid && error.dataset.source === "extras") {
      replace(error);
      error.dataset.source = "";
    }
  }

  function followSettings(storeState) {
    const next = storeState?.settings;
    if (!next || next === settings) return;
    settings = next;
    if (!state.pickedFolder) folderLine.setFolder(defaultFolder(settings, state.mode));
  }

  function refreshQueued(storeState) {
    const job = queuedJobFor(Object.values(storeState?.jobs ?? {}), info);
    if (queuedNote.hidden !== !job) queuedNote.hidden = !job;
    setText(queuedNote, job ? "This video is already in the queue" : "");
  }

  async function download() {
    const row = findRow(formats, state.selectedId);
    if (!row || live || isInert(downloadButton)) return;
    const { valid } = validateExtras(state.extras);
    if (!valid) {
      setEditorOpen(true);
      editor.revealErrors();
      replace(error, "Fix the extras above to download");
      error.dataset.source = "extras";
      return;
    }
    const body = buildJobRequest({
      url, row, audioTrack: state.audioTrackId ? findRow(formats, state.audioTrackId) : null,
      extras: state.extras, destDir: state.pickedFolder,
    });
    if (cookies) body.cookies = cookies;
    state.busy = true;
    replace(error);
    error.dataset.source = "";
    refresh();
    try {
      const job = await api.createJob(body);
      toast?.show("Queued", { kind: "success" });
      onQueued?.(job);
    } catch (failure) {
      const message = failure?.message || "Snag couldn't add the download";
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

  const unsubscribe = store ? store.subscribe((storeState) => {
    refreshQueued(storeState);
    followSettings(storeState);
  }) : () => {};
  if (store) refreshQueued(store.getState());
  refresh();

  return {
    element,
    /** The POST /api/jobs body for the current choices (or null when nothing can be downloaded). */
    getRequest() {
      const row = findRow(formats, state.selectedId);
      if (!row || live) return null;
      return buildJobRequest({
        url, row, audioTrack: state.audioTrackId ? findRow(formats, state.audioTrackId) : null,
        extras: state.extras, destDir: state.pickedFolder,
      });
    },
    /** Names the playlist on the "part of a playlist" line (a blank or missing name goes back to the
     * unnamed line); does nothing when the panel shows no such line. */
    setPlaylistName,
    destroy() {
      unsubscribe();
    },
  };
}
