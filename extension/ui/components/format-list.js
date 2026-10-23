// The format rows of the download panel: the simple view (quality | file | size) and "Show all
// formats" (quality | file | stream: container · codec · fps · bitrate | size, spec 4.2) with the
// Audio track dropdown for video.
//
//   const list = createFormatList({info, mode, selectedId, expanded, audioTrackId,
//                                  onSelect(id), onAudioTrack(id | null), onToggleExpanded(expanded)});
//   list.update({mode, selectedId, expanded, audioTrackId});
//
// Rows are radio buttons (arrow keys move the selection); the rows are rebuilt only when the mode or view
// changes, and the "Show all formats" link is made once, so keyboard focus stays put while choosing.
// A row picked in the expanded view stays listed in the simple view, with its codec to tell it apart.
// Compact (the popup): the simple view lists only the 4 best rows, plus the selected one if it's further
// down; the rest are under "Show all formats".

import { h, replace, setText } from "./el.js";
import { uniqueId } from "./ids.js";
import {
  MODE, FORMAT_KIND, allRows, audioTrackLabel, audioTrackOptions, findRow, resultExt, rowSize, simpleRows,
  takesAudioTrack, visibleRows,
} from "../choice.js";
import { codecName, containerName, formatBitrate, formatSize, languageName, rowLabel } from "../format.js";

const COMPACT_ROWS = 4;

const isNumber = (value) => typeof value === "number" && Number.isFinite(value);

/** The expanded view's Stream column, about the format itself (the File column has the finished
 * file's container): "WEBM · VP9 · 60 fps · 4.2 Mbps · HDR10", "MP4 · H.264 · 30 fps · 950 kbps",
 * "WEBM · Opus · 135 kbps · German". Every frame rate is shown, 30 fps and under too. */
export function formatDetail(row) {
  if (row.kind === FORMAT_KIND.MP3) return "Converted, 192 kbps";
  if (row.kind === FORMAT_KIND.AUDIO) {
    const language = languageName(row.language);
    return [
      containerName(row.ext), codecName(row.acodec), formatBitrate(row.abr),
      language && (row.is_original ? `${language} (original)` : language),
    ].filter(Boolean).join(" · ");
  }
  return [
    containerName(row.ext), codecName(row.vcodec), isNumber(row.fps) && row.fps > 0 ? `${Math.round(row.fps)} fps` : null,
    formatBitrate(row.tbr),
    row.dynamic_range && row.dynamic_range !== "SDR" ? row.dynamic_range : null,
    row.upscaled ? "AI-upscaled" : null,
    row.has_audio ? "includes audio" : null,
  ].filter(Boolean).join(" · ");
}

/** The file column: the finished file's container ("MP4"), not the stream's own; with another audio
 * track, the container the merge with that track gets. */
export function fileLabel(row, audioTrack = null) {
  return containerName(resultExt(row, audioTrack));
}

/** What tells an expanded-view row apart when it is listed in the simple view beside rows of the same
 * quality: its codec ("VP9"), or for audio its codec and language ("Opus · German"). */
export function rowHint(row) {
  if (row.kind === FORMAT_KIND.MP3) return "";
  if (row.kind === FORMAT_KIND.AUDIO) {
    return [codecName(row.acodec), languageName(row.language)].filter(Boolean).join(" · ");
  }
  return codecName(row.vcodec);
}

export function showAllLabel(count, expanded) {
  if (expanded) return "Show fewer formats";
  return count > 1 ? `Show all ${count} formats` : "Show all formats";
}

function sizeText(row, formats, audioTrack) {
  const { bytes, estimate } = rowSize(row, { formats, audioTrack });
  return bytes === null ? "" : formatSize(bytes, { estimate });
}

export function createFormatList({
  info, mode = MODE.VIDEO, selectedId = null, expanded = false, audioTrackId = null, compact = false,
  onSelect = () => {}, onAudioTrack = () => {}, onToggleExpanded = () => {},
}) {
  const formats = info?.formats ?? {};
  const name = uniqueId("format");
  const state = { mode, selectedId, expanded, audioTrackId };
  // Made once and kept as the last child, so it keeps focus while the rows above it are rebuilt.
  const toggle = h("button", {
    type: "button", class: "link-button format-toggle small", "aria-expanded": "false",
    onClick: () => onToggleExpanded(!state.expanded),
  });
  const element = h("div", { class: ["format-list", compact && "format-list-compact"] }, toggle);
  let rows = new Map(); // format id -> {label, input, file, size, row}
  let trackSelect = null;
  let trackHint = null;

  const audioTrack = () => (state.audioTrackId ? findRow(formats, state.audioTrackId) : null);
  const listed = () => visibleRows(formats, state.mode, {
    expanded: state.expanded, selectedId: state.selectedId, limit: compact ? COMPACT_ROWS : null,
  });

  function rowElement(row, { appended }) {
    const input = h("input", {
      type: "radio", name, value: row.format_id, class: "visually-hidden", checked: row.format_id === state.selectedId,
      onChange: () => {
        if (input.checked) onSelect(row.format_id);
      },
    });
    const size = h("span", { class: "format-size num" }, sizeText(row, formats, audioTrack()));
    const file = h("span", { class: "format-file" }, fileLabel(row, audioTrack()));
    const hint = appended ? rowHint(row) : "";
    const quality = h("span", { class: "format-quality truncate" }, rowLabel(row),
      hint ? h("span", { class: "format-hint" }, hint) : null,
      !state.expanded && row.dynamic_range && row.dynamic_range !== "SDR" ? h("span", { class: "badge" }, "HDR") : null);
    const cells = state.expanded
      ? [quality, file, h("span", { class: "format-detail truncate" }, formatDetail(row)), size]
      : [quality, file, size];
    const label = h("label", {
      class: ["row", "format-row", state.expanded && "format-row-full", row.format_id === state.selectedId && "is-selected"],
    }, input, cells);
    rows.set(row.format_id, { label, input, file, size, row });
    return label;
  }

  function trackControls() {
    const id = uniqueId("audio-track");
    trackSelect = h("select", {
      id, class: "select format-track-select",
      onChange: () => onAudioTrack(trackSelect.value || null),
    });
    trackHint = h("span", { class: "field-hint" });
    return h("div", { class: "format-track" },
      h("label", { class: "format-track-label", for: id }, "Audio track"), trackSelect, trackHint);
  }

  function refreshTrack() {
    if (!trackSelect) return;
    const selected = findRow(formats, state.selectedId);
    const paired = selected?.audio_id ? findRow(formats, selected.audio_id) : null;
    const options = audioTrackOptions(formats).map((option) => [
      option.id ?? "", option.id === null && paired ? `Auto: ${audioTrackLabel(paired)}` : option.label,
    ]);
    // Rebuilt only when an option's text changes (the Auto line follows the selected row), so an open or
    // focused dropdown isn't disturbed by updates that change nothing.
    const shown = [...trackSelect.children].map((node) => [node.value, node.textContent]);
    if (JSON.stringify(shown) !== JSON.stringify(options)) {
      replace(trackSelect, options.map(([value, text]) => h("option", { value }, text)));
    }
    trackSelect.value = state.audioTrackId ?? "";
    const takes = takesAudioTrack(selected);
    trackSelect.disabled = !takes;
    setText(trackHint, takes || !selected ? "" : "This format includes its own audio");
  }

  // The Audio track dropdown: in the expanded video view, and in the simple view too while another
  // track is chosen (so a choice made there never hides).
  function wantsTrack() {
    return state.mode === MODE.VIDEO && (formats.audio_tracks ?? []).length > 0
      && (state.expanded || state.audioTrackId !== null);
  }

  function refreshToggle() {
    const shown = listed().length;
    const total = allRows(formats, state.mode).length;
    toggle.hidden = !(total > shown || state.expanded);
    toggle.setAttribute("aria-expanded", String(state.expanded));
    setText(toggle, showAllLabel(total, state.expanded));
  }

  function render() {
    rows = new Map();
    trackSelect = null;
    trackHint = null;
    const shown = listed();
    const simple = new Set(simpleRows(formats, state.mode).map((row) => row.format_id));
    const header = state.expanded
      ? h("div", { class: "format-head format-row format-row-full", "aria-hidden": "true" },
        h("span", null, "Quality"), h("span", null, "File"), h("span", null, "Stream"), h("span", null, "Size"))
      : null;
    const list = shown.length
      ? h("fieldset", { class: "format-rows" },
        h("legend", { class: "visually-hidden" }, state.mode === MODE.VIDEO ? "Video format" : "Audio format"),
        h("div", { class: "rows" }, shown.map((row) => rowElement(row, {
          appended: !state.expanded && !simple.has(row.format_id),
        }))))
      : h("p", { class: "note" }, "No formats to choose from.");
    const track = wantsTrack() ? trackControls() : null;
    for (const child of [...element.childNodes]) if (child !== toggle) child.remove();
    for (const node of [header, list, track]) if (node) element.insertBefore(node, toggle);
    refreshTrack();
    refreshToggle();
  }

  function refreshSelection() {
    const track = audioTrack();
    for (const [id, item] of rows) {
      const selected = id === state.selectedId;
      item.label.classList.toggle("is-selected", selected);
      if (item.input.checked !== selected) item.input.checked = selected;
      setText(item.size, sizeText(item.row, formats, track));
      setText(item.file, fileLabel(item.row, track));
    }
  }

  render();

  return {
    element,
    update(next) {
      const rebuild = (next.mode !== undefined && next.mode !== state.mode)
        || (next.expanded !== undefined && next.expanded !== state.expanded)
        || (next.selectedId !== undefined && next.selectedId !== null && !rows.has(next.selectedId));
      Object.assign(state, Object.fromEntries(Object.entries(next).filter(([, value]) => value !== undefined)));
      // A dropdown that appears is drawn now; one going back to Auto stays until the view changes, so
      // focus isn't lost from it.
      if (rebuild || (wantsTrack() && !trackSelect)) render();
      else {
        refreshSelection();
        refreshTrack();
        refreshToggle();
      }
    },
  };
}
