// The extras editor: subtitles (on/off, language, embedded or a separate file), thumbnail and tags,
// chapters, SponsorBlock (on/off, remove or mark, categories) and trim (from/to), with inline notes for
// what a trim or the video itself makes unavailable, and trim checks matching the server's.
//
//   const editor = createExtrasEditor({extras, info, audioOnly, onChange(extras, {valid, errors})});
//   editor.getExtras(); editor.setAudioOnly(bool); editor.setExtras(extras);
//   editor.showServerError(message)   // the server still refused: its message, shown as-is (not announced:
//                                     // the caller's own alert does that)
//
// info may be null (Settings edits defaults for every video); then video-specific notes are skipped.
// Errors found while typing are polite live regions, and every note and error is rewritten only when its
// text changes, so a screen reader hears each message once instead of on every keystroke.

import { h, replace, setText } from "./el.js";
import { uniqueId } from "./ids.js";
import {
  MAX_TRIM_LENGTH, SPONSORBLOCK_MODE, SUBTITLE_MODE, chaptersNote, cleanExtras, extrasAvailability, sponsorCategoriesFor,
  subtitleLanguages, subtitleNote, trimErrors, trimNote, validateExtras, withSponsorMode,
} from "../choice.js";
import { sponsorCategoryName } from "../format.js";

const DEFAULT_EXTRAS = Object.freeze({
  subtitles: false, subtitle_lang: "en", subtitle_mode: SUBTITLE_MODE.EMBEDDED, thumbnail_tags: false, chapters: false,
  sponsorblock: false, sponsorblock_mode: SPONSORBLOCK_MODE.REMOVE,
  sponsorblock_categories: ["sponsor", "selfpromo", "interaction"], trim_start: null, trim_end: null,
});

function checkbox(text, checked, onChange) {
  const input = h("input", { type: "checkbox", checked, onChange: () => onChange(input.checked) });
  return { input, label: h("label", { class: "check extras-check" }, input, h("span", null, text)) };
}

function select(id, options, value, onChange) {
  const node = h("select", { id, class: "select extras-select", onChange: () => onChange(node.value) },
    options.map((option) => h("option", { value: option.value, selected: option.value === value }, option.label)));
  node.value = value;
  return node;
}

export function createExtrasEditor({ extras, info = null, audioOnly = false, compact = false, onChange = () => {} }) {
  let current = { ...DEFAULT_EXTRAS, ...(extras ?? {}) };
  current.sponsorblock_categories = [...(current.sponsorblock_categories ?? [])];
  let onlyAudio = audioOnly;
  let showTrimErrors = { start: false, end: false };

  // --- controls ---

  const subtitles = checkbox("Subtitles", current.subtitles, (on) => change({ subtitles: on }));
  const langId = uniqueId("subtitle-lang");
  const langSelect = select(langId, subtitleLanguages(info, current.subtitle_lang)
    .map((option) => ({ value: option.code, label: option.label })), current.subtitle_lang,
  (code) => change({ subtitle_lang: code }));
  const modeId = uniqueId("subtitle-mode");
  const subtitleMode = select(modeId, [
    { value: SUBTITLE_MODE.EMBEDDED, label: "Embedded" }, { value: SUBTITLE_MODE.FILE, label: "Separate file" },
  ], current.subtitle_mode, (mode) => change({ subtitle_mode: mode }));
  const subtitleOptions = h("div", { class: "extras-options cluster" },
    h("label", { class: "visually-hidden", for: langId }, "Subtitle language"), langSelect,
    h("label", { class: "visually-hidden", for: modeId }, "How subtitles are saved"), subtitleMode);
  const subtitleNoteNode = h("p", { class: "extras-note" });

  const thumbnail = checkbox("Thumbnail and tags", current.thumbnail_tags, (on) => change({ thumbnail_tags: on }));
  const chapters = checkbox("Chapters", current.chapters, (on) => change({ chapters: on }));
  const chaptersNoteNode = h("p", { class: "extras-note" });

  const sponsor = checkbox("SponsorBlock", current.sponsorblock, (on) => change({ sponsorblock: on }));
  const sponsorModeId = uniqueId("sponsor-mode");
  const sponsorMode = select(sponsorModeId, [
    { value: SPONSORBLOCK_MODE.REMOVE, label: "Remove segments" },
    { value: SPONSORBLOCK_MODE.MARK, label: "Mark as chapters" },
  ], current.sponsorblock_mode, (mode) => {
    current = withSponsorMode(current, mode);
    renderCategories();
    emit();
  });
  const categories = h("fieldset", { class: "extras-categories" },
    h("legend", { class: "visually-hidden" }, "SponsorBlock categories"));
  const sponsorError = h("p", { class: "field-error", "aria-live": "polite" });
  const sponsorOptions = h("div", { class: "extras-options stack" },
    h("label", { class: "visually-hidden", for: sponsorModeId }, "What SponsorBlock does"), sponsorMode, categories, sponsorError);

  const startId = uniqueId("trim-start");
  const endId = uniqueId("trim-end");
  const trimInput = (id, key, placeholder, name) => {
    const input = h("input", {
      id, class: "input input-short extras-trim-input", type: "text", "aria-label": name, inputmode: "decimal", autocomplete: "off",
      maxlength: String(MAX_TRIM_LENGTH),
      spellcheck: "false", placeholder, value: current[key] ?? "",
      onInput: () => change({ [key]: input.value }),
      onChange: () => {
        showTrimErrors = { ...showTrimErrors, [key === "trim_start" ? "start" : "end"]: true };
        refresh();
      },
    });
    return input;
  };
  const trimStart = trimInput(startId, "trim_start", "0:00", "Trim from");
  const trimEnd = trimInput(endId, "trim_end", "end", "Trim to");
  const startError = h("span", { class: "field-error", id: uniqueId("trim-start-error"), "aria-live": "polite" });
  const endError = h("span", { class: "field-error", id: uniqueId("trim-end-error"), "aria-live": "polite" });
  trimStart.setAttribute("aria-describedby", startError.id);
  trimEnd.setAttribute("aria-describedby", endError.id);
  const trimNoteNode = h("p", { class: "extras-note" });
  // Not a live region: whoever sent the extras to the server announces its refusal (the panel's or a
  // setting's alert); this copy keeps the message beside the controls it is about.
  const serverError = h("p", { class: "field-error" });

  function renderCategories() {
    const allowed = sponsorCategoriesFor(current.sponsorblock_mode);
    sponsorMode.value = current.sponsorblock_mode;
    replace(categories, h("legend", { class: "visually-hidden" }, "SponsorBlock categories"),
      h("div", { class: "extras-category-grid" }, allowed.map((category) => {
        const input = h("input", {
          type: "checkbox", checked: current.sponsorblock_categories.includes(category),
          onChange: () => {
            const chosen = new Set(current.sponsorblock_categories);
            if (input.checked) chosen.add(category);
            else chosen.delete(category);
            change({ sponsorblock_categories: allowed.filter((item) => chosen.has(item)) });
          },
        });
        return h("label", { class: "check small" }, input, h("span", null, sponsorCategoryName(category)));
      })));
  }

  const row = (...children) => h("div", { class: "extras-row" }, ...children);
  const subtitlesRow = row(subtitles.label, subtitleOptions, subtitleNoteNode);
  const chaptersRow = row(chapters.label, chaptersNoteNode);
  const sponsorRow = row(sponsor.label, sponsorOptions);
  const element = h("div", { class: ["extras-editor", compact && "extras-editor-compact"] },
    subtitlesRow,
    row(thumbnail.label),
    chaptersRow,
    sponsorRow,
    h("div", { class: "extras-row" },
      h("span", { class: "extras-row-title" }, "Trim"),
      h("div", { class: "extras-trim cluster" },
        h("label", { for: startId, class: "small muted" }, "From"), trimStart,
        h("label", { for: endId, class: "small muted" }, "to"), trimEnd),
      startError, endError, trimNoteNode),
    serverError);

  // --- state ---

  function validation() {
    return validateExtras(current);
  }

  function refresh() {
    const available = extrasAvailability(current, { audioOnly: onlyAudio });
    subtitles.input.checked = current.subtitles;
    subtitles.input.disabled = onlyAudio;
    subtitleOptions.hidden = !current.subtitles || onlyAudio;
    subtitlesRow.classList.toggle("is-unavailable", current.subtitles && !available.subtitles);
    setText(subtitleNoteNode, onlyAudio ? "Subtitles aren't saved with audio." : subtitleNote(info, current));
    thumbnail.input.checked = current.thumbnail_tags;
    chapters.input.checked = current.chapters;
    chaptersRow.classList.toggle("is-unavailable", current.chapters && !available.chapters);
    setText(chaptersNoteNode, chaptersNote(info, current));
    sponsor.input.checked = current.sponsorblock;
    sponsorOptions.hidden = !current.sponsorblock;
    sponsorRow.classList.toggle("is-unavailable", current.sponsorblock && !available.sponsorblock);

    const { errors } = validation();
    setText(sponsorError, current.sponsorblock ? errors.sponsorblock ?? "" : "");
    const trim = trimErrors(current.trim_start, current.trim_end);
    const startMessage = showTrimErrors.start ? trim.start : null;
    const endMessage = showTrimErrors.end || (showTrimErrors.start && trim.end && !trim.start) ? trim.end : null;
    setText(startError, startMessage ?? "");
    setText(endError, endMessage ?? "");
    trimStart.setAttribute("aria-invalid", String(Boolean(startMessage)));
    trimEnd.setAttribute("aria-invalid", String(Boolean(endMessage)));
    setText(trimNoteNode, trimNote(current, { audioOnly: onlyAudio }));
  }

  function emit() {
    setText(serverError, "");
    refresh();
    onChange(cleanExtras(current), validation());
  }

  function change(fields) {
    current = { ...current, ...fields };
    emit();
  }

  renderCategories();
  refresh();

  return {
    element,
    getExtras: () => cleanExtras(current),
    validation,
    setAudioOnly(value) {
      onlyAudio = Boolean(value);
      refresh();
    },
    setExtras(next) {
      current = { ...DEFAULT_EXTRAS, ...(next ?? {}) };
      current.sponsorblock_categories = [...(current.sponsorblock_categories ?? [])];
      trimStart.value = current.trim_start ?? "";
      trimEnd.value = current.trim_end ?? "";
      langSelect.value = current.subtitle_lang;
      subtitleMode.value = current.subtitle_mode;
      showTrimErrors = { start: false, end: false };
      renderCategories();
      refresh();
    },
    /** Show every pending error (e.g. when Download is pressed with an invalid trim). */
    revealErrors() {
      showTrimErrors = { start: true, end: true };
      refresh();
    },
    showServerError(message) {
      setText(serverError, message ?? "");
    },
  };
}
