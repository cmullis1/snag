// The format list on a fake DOM: focus on "Show all formats", the File column with another audio track,
// and an expanded-view row kept in the simple view.
import "./fake-dom.js";
import test from "node:test";
import assert from "node:assert/strict";

import { byClass, byText, find, findAll } from "./fake-dom.js";
import { createFormatList, rowHint } from "../../ui/components/format-list.js";

const h264 = {
  format_id: "137", label: "1080p", ext: "mp4", vcodec: "avc1.640028", kind: "video", height: 1080, size: 3000,
  audio_id: "140", has_audio: false, result_ext: "mp4", audio_ext: "m4a", audio_codec: "mp4a.40.2",
};
const vp9 = { ...h264, format_id: "248", ext: "webm", vcodec: "vp9", audio_id: "251", result_ext: "webm", audio_ext: "webm", audio_codec: "opus" };
const v720 = { ...h264, format_id: "136", label: "720p", height: 720 };
const aac = { format_id: "140", label: "m4a 129k", ext: "m4a", acodec: "mp4a.40.2", kind: "audio", has_audio: true, size: 100, result_ext: "m4a", language: "en", is_original: true };
const opus = { format_id: "251", label: "opus 135k", ext: "webm", acodec: "opus", kind: "audio", has_audio: true, size: 120, result_ext: "opus", language: "en", is_original: true };

const INFO = {
  formats: { video_simple: [h264, v720], video_all: [h264, vp9, v720], audio: [aac, opus], audio_tracks: [aac, opus] },
};

/** A list wired like the download panel wires it: callbacks feed straight back into update(). */
function wired(options = {}) {
  const list = createFormatList({
    info: INFO, selectedId: "137", ...options,
    onSelect: (id) => list.update({ selectedId: id }),
    onAudioTrack: (id) => list.update({ audioTrackId: id }),
    onToggleExpanded: (expanded) => list.update({ expanded }),
  });
  return list;
}

const toggleOf = (list) => byClass(list.element, "format-toggle")[0];
const rowOf = (list, id) => find(list.element, (node) => node.tagName === "INPUT" && node.value === id)?.parentNode;
const cell = (row, name) => byClass(row, name)[0];

test("the Show all formats link keeps focus and is the same button while the view changes", () => {
  const list = wired();
  const toggle = toggleOf(list);
  assert.equal(toggle.textContent, "Show all 3 formats");
  assert.equal(toggle.getAttribute("aria-expanded"), "false");
  toggle.focus();
  toggle.click();
  assert.equal(toggleOf(list), toggle);
  assert.equal(document.activeElement, toggle, "focus stays on the link");
  assert.equal(toggle.textContent, "Show fewer formats");
  assert.equal(toggle.getAttribute("aria-expanded"), "true");
  assert.ok(rowOf(list, "248"), "the expanded view lists every format");
  assert.equal(list.element.childNodes.at(-1), toggle, "it stays below the rows");
  toggle.click();
  assert.equal(document.activeElement, toggle);
  assert.equal(toggle.textContent, "Show all 3 formats");
  assert.equal(rowOf(list, "248"), undefined);
});

test("the File column follows the chosen audio track", () => {
  const list = wired({ expanded: true });
  assert.equal(cell(rowOf(list, "137"), "format-file").textContent, "MP4");
  assert.equal(cell(rowOf(list, "248"), "format-file").textContent, "WEBM");
  list.update({ audioTrackId: "251" });
  assert.equal(cell(rowOf(list, "137"), "format-file").textContent, "MKV", "H.264 + Opus merge into MKV");
  assert.equal(cell(rowOf(list, "248"), "format-file").textContent, "WEBM", "Opus is VP9's own pairing");
  list.update({ audioTrackId: "140" });
  assert.equal(cell(rowOf(list, "137"), "format-file").textContent, "MP4");
  assert.equal(cell(rowOf(list, "248"), "format-file").textContent, "MKV", "VP9 + AAC merge into MKV");
  list.update({ audioTrackId: null });
  assert.equal(cell(rowOf(list, "248"), "format-file").textContent, "WEBM");
});

test("a row picked in the expanded view keeps its codec beside its quality in the simple view", () => {
  const list = wired({ expanded: true });
  list.update({ selectedId: "248" });
  list.update({ expanded: false });
  const picked = rowOf(list, "248");
  assert.ok(picked, "the picked row stays listed");
  assert.equal(cell(picked, "format-hint").textContent, "VP9");
  assert.equal(byClass(rowOf(list, "137"), "format-hint").length, 0, "the simple view's own rows have no hint");
});

test("the hint names the codec, and an audio track's language", () => {
  assert.equal(rowHint(vp9), "VP9");
  assert.equal(rowHint({ ...opus, language: "de", is_original: false }), "Opus · German");
  assert.equal(rowHint({ kind: "mp3" }), "");
});

test("updates that change nothing leave the Audio track dropdown alone", () => {
  const list = wired({ expanded: true });
  const select = find(list.element, (node) => node.tagName === "SELECT");
  select.focus();
  const before = select.mutations;
  list.update({ selectedId: "137" });
  list.update({ audioTrackId: null });
  assert.equal(select.mutations, before);
  assert.equal(document.activeElement, select);
  list.update({ selectedId: "248" });
  assert.ok(byText(select, "Auto: English (original) · opus 135k"), "the Auto line follows the selected row");
});

// --- compact (the popup): the simple view lists only the 4 best rows ---

const eight = [2160, 1440, 1080, 720, 480, 360, 240, 144].map((height) => ({
  ...h264, format_id: String(height), label: `${height}p`, height,
}));
const EIGHT = { formats: { video_simple: eight, video_all: [...eight, vp9], audio: [aac, opus], audio_tracks: [aac, opus] } };
const shownIds = (list) => findAll(list.element, (node) => node.tagName === "INPUT").map((node) => node.value);

function wiredEight(options = {}) {
  const list = createFormatList({
    info: EIGHT, selectedId: "2160", compact: true, ...options,
    onSelect: (id) => list.update({ selectedId: id }),
    onToggleExpanded: (expanded) => list.update({ expanded }),
  });
  return list;
}

test("compact lists the 4 best simple rows; the rest wait under Show all formats", () => {
  const list = wiredEight();
  assert.deepEqual(shownIds(list), ["2160", "1440", "1080", "720"]);
  assert.equal(toggleOf(list).hidden, false);
  assert.equal(toggleOf(list).textContent, "Show all 9 formats");
  const full = wired({ info: EIGHT, selectedId: "2160" });
  assert.equal(shownIds(full).length, 8, "the full tab is unchanged");
});

test("compact keeps a selected row beyond the 4 listed, in its place, whether picked in the simple or the expanded view", () => {
  const list = wiredEight({ selectedId: "480" });
  assert.deepEqual(shownIds(list), ["2160", "1440", "1080", "720", "480"]);
  const later = wiredEight({ selectedId: "144" });
  assert.deepEqual(shownIds(later), ["2160", "1440", "1080", "720", "144"]);
  const expanded = wiredEight({ expanded: true });
  expanded.update({ selectedId: "248" }); // only in the expanded view
  toggleOf(expanded).click();
  assert.deepEqual(shownIds(expanded), ["2160", "1440", "1080", "720", "248"]);
});

test("compact: Show all expands to every format, and Show fewer goes back to the 4", () => {
  const list = wiredEight();
  const toggle = toggleOf(list);
  toggle.focus();
  toggle.click();
  assert.deepEqual(shownIds(list), [...eight.map((row) => row.format_id), "248"]);
  assert.equal(document.activeElement, toggle);
  toggle.click();
  assert.deepEqual(shownIds(list), ["2160", "1440", "1080", "720"]);
  assert.equal(document.activeElement, toggle);
});

test("compact: choosing a listed row keeps focus and the list stable; Show all is hidden when nothing is held back", () => {
  const list = wiredEight();
  const radio = (id) => find(list.element, (node) => node.tagName === "INPUT" && node.value === id);
  radio("1080").focus();
  radio("1080").click();
  assert.equal(document.activeElement, radio("1080"));
  assert.deepEqual(shownIds(list), ["2160", "1440", "1080", "720"]);
  const small = createFormatList({ info: INFO, selectedId: "137", compact: true });
  assert.deepEqual(shownIds(small), ["137", "136"]);
  const same = createFormatList({
    info: { formats: { video_simple: eight.slice(0, 4), video_all: eight.slice(0, 4), audio: [], audio_tracks: [] } },
    selectedId: "2160", compact: true,
  });
  assert.equal(toggleOf(same).hidden, true);
});

test("the expanded view shows each stream's own container and frame rate, 30 fps included", () => {
  const info = { formats: { ...INFO.formats, video_all: [{ ...h264, fps: 30, tbr: 4400 }, { ...vp9, fps: 60 }, v720] } };
  const list = createFormatList({ info, selectedId: "137", expanded: true });
  const detail = (id) => cell(rowOf(list, id), "format-detail").textContent;
  assert.equal(detail("137"), "MP4 · H.264 · 30 fps · 4.4 Mbps");
  assert.equal(detail("248"), "WEBM · VP9 · 60 fps");
  assert.equal(cell(rowOf(list, "248"), "format-file").textContent, "WEBM", "the File column is still the finished file's");
  assert.ok(byText(list.element, "Stream"), "the column is headed Stream");
});
