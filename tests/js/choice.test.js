import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import {
  MODE, FORMAT_KIND, simpleRows, allRows, visibleRows, defaultRowId, findRow, takesAudioTrack, audioTrackLabel,
  audioTrackOptions, rowSize, buildJobRequest, cleanExtras, trimErrors, trimNote, validateExtras, MAX_TRIM_LENGTH,
  sponsorCategoriesFor, withSponsorMode, subtitleLanguages, subtitleNote, chaptersNote, defaultFolder, isLive,
  lookupNotes, metaLine, extrasAvailability, queuedJobFor, SPONSORBLOCK_CATEGORIES, SPONSORBLOCK_REMOVABLE,
  resultExt,
} from "../../ui/choice.js";

const MB = 1024 ** 2;

// Rows shaped like the server's FormatRow JSON (snag/info/formats.py).
const v2160 = {
  format_id: "401", label: "2160p", ext: "mp4", vcodec: "av01.0.12M.08", height: 2160, fps: 25, size: 900 * MB,
  size_is_estimate: false, audio_id: "140", has_audio: false, language: "en", is_original: true, tbr: 18000,
  dynamic_range: "SDR", kind: "video", audio_ext: "m4a", audio_codec: "mp4a.40.2", result_ext: "mp4",
};
const v1080 = {
  ...v2160, format_id: "248", label: "1080p", ext: "webm", vcodec: "vp9", height: 1080, size: 300 * MB,
  size_is_estimate: true, audio_id: "251", audio_ext: "webm", audio_codec: "opus", result_ext: "webm", tbr: 4200,
};
const v1080h264 = { ...v1080, format_id: "137", ext: "mp4", vcodec: "avc1.640028", audio_id: "140",
  audio_ext: "m4a", audio_codec: "mp4a.40.2", result_ext: "mp4", size: 280 * MB, size_is_estimate: false };
const combined18 = {
  format_id: "18", label: "360p", ext: "mp4", vcodec: "avc1.42001E", acodec: "mp4a.40.2", height: 360, fps: 25,
  size: 20 * MB, has_audio: true, language: "en", is_original: true, kind: "video", result_ext: "mp4",
};
const a140 = {
  format_id: "140", label: "m4a 129k", ext: "m4a", acodec: "mp4a.40.2", abr: 129.5, size: 5 * MB,
  size_is_estimate: false, has_audio: true, language: "en", is_original: true, kind: "audio", result_ext: "m4a",
};
const a251 = { ...a140, format_id: "251", label: "opus 135k", ext: "webm", acodec: "opus", abr: 135, size: 6 * MB,
  size_is_estimate: true, result_ext: "opus" };
const a251de = { ...a251, format_id: "251-1", language: "de", is_original: false, size: 6 * MB };
const mp3 = { format_id: "mp3", label: "MP3", ext: "mp3", abr: 192, size: 7 * MB, size_is_estimate: true,
  has_audio: true, kind: "mp3", result_ext: "mp3" };

const formats = {
  video_simple: [v2160, v1080, combined18],
  video_all: [v2160, v1080, v1080h264, combined18],
  audio: [mp3, a251, a140, a251de],
  audio_tracks: [a251, a140, a251de],
};

const info = {
  type: "video", site: "youtube", video_id: "abc", url: "https://www.youtube.com/watch?v=abc", title: "A video",
  channel: "Some channel", duration: 225, thumbnail: null, formats, subtitle_langs: ["en", "fr"],
  auto_subtitle_langs: ["en", "de"], has_chapters: true, playlist: null, live_status: "not_live",
  js_runtime_missing: false, duplicate: null,
};

const extras = {
  subtitles: true, subtitle_lang: "en", subtitle_mode: "embed", thumbnail_tags: true, chapters: true,
  sponsorblock: false, sponsorblock_mode: "remove", sponsorblock_categories: ["sponsor", "selfpromo", "interaction"],
  trim_start: null, trim_end: null,
};

const ids = (rows) => rows.map((row) => row.format_id);

// --- rows ---

test("the simple video view is the server's simple list", () => {
  assert.deepEqual(ids(simpleRows(formats, MODE.VIDEO)), ["401", "248", "18"]);
});

test("the simple audio view is MP3 first, then original-language audio only", () => {
  assert.deepEqual(ids(simpleRows(formats, MODE.AUDIO)), ["mp3", "251", "140"]);
});

test("the simple audio view falls back to every track when none is marked original", () => {
  const dubbed = { ...formats, audio: [mp3, a251de] };
  assert.deepEqual(ids(simpleRows(dubbed, MODE.AUDIO)), ["mp3", "251-1"]);
});

test("show all lists every format of the mode", () => {
  assert.deepEqual(ids(allRows(formats, MODE.VIDEO)), ["401", "248", "137", "18"]);
  assert.deepEqual(ids(allRows(formats, MODE.AUDIO)), ["mp3", "251", "140", "251-1"]);
});

test("the simple view keeps a row picked in the expanded view visible", () => {
  assert.deepEqual(ids(visibleRows(formats, MODE.VIDEO, { expanded: false, selectedId: "137" })), ["401", "248", "18", "137"]);
  assert.deepEqual(ids(visibleRows(formats, MODE.VIDEO, { expanded: false, selectedId: "248" })), ["401", "248", "18"]);
  assert.deepEqual(ids(visibleRows(formats, MODE.VIDEO, { expanded: true, selectedId: "137" })), ["401", "248", "137", "18"]);
});

test("the default row is the first (best) one of the simple view", () => {
  assert.equal(defaultRowId(formats, MODE.VIDEO), "401");
  assert.equal(defaultRowId(formats, MODE.AUDIO), "mp3");
  assert.equal(defaultRowId({ video_simple: [], video_all: [], audio: [], audio_tracks: [] }, MODE.VIDEO), null);
});

test("rows are found by id across every list", () => {
  assert.equal(findRow(formats, "137"), v1080h264);
  assert.equal(findRow(formats, "mp3"), mp3);
  assert.equal(findRow(formats, "nope"), null);
  assert.equal(FORMAT_KIND.MP3, "mp3");
});

// --- audio track ---

test("only a video format without its own audio takes another audio track", () => {
  assert.equal(takesAudioTrack(v1080), true);
  assert.equal(takesAudioTrack(combined18), false);
  assert.equal(takesAudioTrack(a140), false);
  assert.equal(takesAudioTrack(mp3), false);
  assert.equal(takesAudioTrack(null), false);
});

test("audio tracks are labelled by language, original first, with container and bitrate", () => {
  assert.equal(audioTrackLabel(a140), "English (original) · m4a 129k");
  assert.equal(audioTrackLabel(a251de), "German · opus 135k");
  assert.equal(audioTrackLabel({ ...a140, language: null, is_original: false }), "Unknown language · m4a 129k");
  const options = audioTrackOptions(formats);
  assert.deepEqual(options[0], { id: null, label: "Auto (original language)" });
  assert.deepEqual(options.slice(1).map((option) => option.id), ["251", "140", "251-1"]);
});

// --- sizes ---

test("a row's size is the server's, with its estimate flag", () => {
  assert.deepEqual(rowSize(v2160, { formats }), { bytes: 900 * MB, estimate: false });
  assert.deepEqual(rowSize(mp3, { formats }), { bytes: 7 * MB, estimate: true });
  assert.deepEqual(rowSize({ ...v2160, size: null }, { formats }), { bytes: null, estimate: false });
});

test("another audio track swaps the paired track's size for its own", () => {
  // 2160p's paired audio is 140 (5 MB); German opus is 6 MB and estimated.
  assert.deepEqual(rowSize(v2160, { formats, audioTrack: a251de }), { bytes: 901 * MB, estimate: true });
  assert.deepEqual(rowSize(v2160, { formats, audioTrack: a140 }), { bytes: 900 * MB, estimate: false });
  // Unknown paired size: the total can't be known.
  const unknown = { ...formats, audio_tracks: [{ ...a140, size: null }, a251de] };
  assert.deepEqual(rowSize(v2160, { formats: unknown, audioTrack: a251de }), { bytes: null, estimate: false });
  // A combined format ignores an audio track.
  assert.deepEqual(rowSize(combined18, { formats, audioTrack: a251de }), { bytes: 20 * MB, estimate: false });
});

// --- request bodies ---

const URL = "https://www.youtube.com/watch?v=abc";

test("a simple video row: url, format id and extras; no folder means the server's default", () => {
  assert.deepEqual(buildJobRequest({ url: URL, row: v2160, extras }), { url: URL, format_id: "401", extras });
});

test("an expanded row with another audio track sends audio_id", () => {
  assert.deepEqual(
    buildJobRequest({ url: URL, row: v1080h264, audioTrack: a251de, extras, destDir: "D:\\Clips" }),
    { url: URL, format_id: "137", audio_id: "251-1", extras, dest_dir: "D:\\Clips" },
  );
});

test("the paired track itself, or a track on a combined format, is not sent", () => {
  assert.deepEqual(buildJobRequest({ url: URL, row: v2160, audioTrack: a140, extras }), { url: URL, format_id: "401", extras });
  assert.deepEqual(buildJobRequest({ url: URL, row: combined18, audioTrack: a251de, extras }), { url: URL, format_id: "18", extras });
});

test("an audio-only row and MP3", () => {
  assert.deepEqual(buildJobRequest({ url: URL, row: a251, extras }), { url: URL, format_id: "251", extras });
  assert.deepEqual(buildJobRequest({ url: URL, row: mp3, extras, destDir: "C:\\Music" }),
    { url: URL, format_id: "mp3", extras, dest_dir: "C:\\Music" });
});

test("request extras are cleaned: blank trims become null, categories are copied", () => {
  const body = buildJobRequest({ url: URL, row: v2160, extras: { ...extras, trim_start: "  1:30 ", trim_end: "" } });
  assert.equal(body.extras.trim_start, "1:30");
  assert.equal(body.extras.trim_end, null);
  assert.notEqual(body.extras.sponsorblock_categories, extras.sponsorblock_categories);
  assert.deepEqual(cleanExtras(null), null);
});

test("a request without a row is refused", () => {
  assert.throws(() => buildJobRequest({ url: URL, row: null, extras }), /format/);
});

// --- trims ---

test("trim times are checked like the server", () => {
  assert.deepEqual(trimErrors("", ""), { start: null, end: null });
  assert.deepEqual(trimErrors("1:30", "2:00"), { start: null, end: null });
  assert.deepEqual(trimErrors("90", "1:02:03.5"), { start: null, end: null });
  assert.equal(trimErrors("1:75", "").start, "Use a time like 90, 1:30 or 1:02:03.5");
  assert.equal(trimErrors("", "abc").end, "Use a time like 90, 1:30 or 1:02:03.5");
  assert.equal(trimErrors("2:00", "1:30").end, "The end must come after the start");
  assert.equal(trimErrors("2:00", "2:00").end, "The end must come after the start");
  assert.equal(trimErrors(null, undefined).start, null);
});

test("trim notes name what a trim makes unavailable", () => {
  assert.equal(trimNote(extras), "");
  assert.equal(trimNote({ ...extras, trim_start: "0:30" }),
    "Chapters, SponsorBlock and subtitles aren't available when trimming from a start time.");
  assert.equal(trimNote({ ...extras, trim_end: "5:00" }), "Chapters and SponsorBlock aren't available when trimming.");
  assert.equal(trimNote({ ...extras, trim_start: "0", trim_end: "5:00" }), "Chapters and SponsorBlock aren't available when trimming.");
  // Audio has no subtitles to lose.
  assert.equal(trimNote({ ...extras, trim_start: "0:30" }, { audioOnly: true }), "Chapters and SponsorBlock aren't available when trimming.");
  // An invalid start isn't a start yet.
  assert.equal(trimNote({ ...extras, trim_start: "x" }), "Chapters and SponsorBlock aren't available when trimming.");
});

test("availability: what a trim or audio-only takes away", () => {
  assert.deepEqual(extrasAvailability(extras), { subtitles: true, chapters: true, sponsorblock: true });
  assert.deepEqual(extrasAvailability({ ...extras, trim_end: "5:00" }), { subtitles: true, chapters: false, sponsorblock: false });
  assert.deepEqual(extrasAvailability({ ...extras, trim_start: "0:10" }), { subtitles: false, chapters: false, sponsorblock: false });
  assert.deepEqual(extrasAvailability(extras, { audioOnly: true }), { subtitles: false, chapters: true, sponsorblock: true });
});

// --- validation ---

test("valid extras have no errors", () => {
  assert.deepEqual(validateExtras(extras), { valid: true, errors: {} });
});

test("trim and SponsorBlock errors are reported per field", () => {
  const result = validateExtras({ ...extras, trim_start: "3:00", trim_end: "1:00", sponsorblock: true, sponsorblock_categories: [] });
  assert.equal(result.valid, false);
  assert.equal(result.errors.trim_end, "The end must come after the start");
  assert.equal(result.errors.sponsorblock, "Pick at least one SponsorBlock category");
});

test("removing can't use the highlight or chapter categories", () => {
  const result = validateExtras({ ...extras, sponsorblock: true, sponsorblock_categories: ["sponsor", "poi_highlight"] });
  assert.equal(result.errors.sponsorblock, "SponsorBlock can't remove Highlight");
  assert.equal(validateExtras({ ...extras, sponsorblock: true, sponsorblock_mode: "mark",
    sponsorblock_categories: ["poi_highlight"] }).valid, true);
});

test("SponsorBlock categories per mode, and switching mode drops what the new mode can't use", () => {
  assert.deepEqual(sponsorCategoriesFor("mark"), SPONSORBLOCK_CATEGORIES);
  assert.deepEqual(sponsorCategoriesFor("remove"), SPONSORBLOCK_REMOVABLE);
  assert.ok(!SPONSORBLOCK_REMOVABLE.includes("chapter"));
  const marked = { ...extras, sponsorblock_mode: "mark", sponsorblock_categories: ["sponsor", "chapter"] };
  assert.deepEqual(withSponsorMode(marked, "remove").sponsorblock_categories, ["sponsor"]);
  assert.equal(withSponsorMode(marked, "remove").sponsorblock_mode, "remove");
});

// --- subtitles and chapters ---

test("subtitle languages: made by people first, then auto-generated, then the current choice", () => {
  assert.deepEqual(subtitleLanguages(info, "en"), [
    { code: "en", label: "English" }, { code: "fr", label: "French" }, { code: "de", label: "German (auto-generated)" },
  ]);
  assert.deepEqual(subtitleLanguages(info, "ja").at(-1), { code: "ja", label: "Japanese (not offered)" });
  // Without a video (defaults in Settings): common languages, with the current choice kept.
  const common = subtitleLanguages(null, "en");
  assert.deepEqual(common[0], { code: "en", label: "English" });
  assert.ok(common.some((option) => option.code === "es" && option.label === "Spanish"));
  assert.deepEqual(subtitleLanguages(null, "fi").at(-1), { code: "fi", label: "Finnish" });
});

test("subtitle and chapter notes say when the video lacks them", () => {
  assert.equal(subtitleNote(info, extras), "");
  assert.equal(subtitleNote(info, { ...extras, subtitle_lang: "ja" }), "This video has no Japanese subtitles.");
  assert.equal(subtitleNote(info, { ...extras, subtitles: false, subtitle_lang: "ja" }), "");
  assert.equal(subtitleNote(info, { ...extras, subtitle_lang: "ja" }, { audioOnly: true }), "");
  assert.equal(chaptersNote(info, extras), "");
  assert.equal(chaptersNote({ ...info, has_chapters: false }, extras), "This video has no chapters.");
  assert.equal(chaptersNote({ ...info, has_chapters: false }, { ...extras, chapters: false }), "");
});

// --- folders, notes, meta ---

test("the default folder follows the mode", () => {
  const settings = { video_dir: "C:\\Users\\me\\Videos\\Snag", audio_dir: "C:\\Users\\me\\Music\\Snag" };
  assert.equal(defaultFolder(settings, MODE.VIDEO), settings.video_dir);
  assert.equal(defaultFolder(settings, MODE.AUDIO), settings.audio_dir);
  assert.equal(defaultFolder(null, MODE.AUDIO), "");
});

test("live streams can't be downloaded; Deno missing is a warning", () => {
  assert.equal(isLive(info), false);
  assert.equal(isLive({ ...info, live_status: "is_live" }), true);
  assert.equal(isLive({ ...info, live_status: "is_upcoming" }), true);
  assert.equal(isLive({ ...info, live_status: "was_live" }), false);
  assert.deepEqual(lookupNotes(info), []);
  const live = lookupNotes({ ...info, live_status: "is_live", js_runtime_missing: true });
  assert.deepEqual(live.map((note) => note.kind), ["live", "deno"]);
  assert.match(live[0].text, /live stream/);
  assert.equal(live[1].text, "Deno is missing, so some YouTube formats won't show");
  assert.match(lookupNotes({ ...info, live_status: "is_upcoming" })[0].text, /hasn't started/);
});

test("the meta line: channel, length and the already-downloaded note", () => {
  const now = new Date(2026, 9, 7, 15, 30);
  assert.deepEqual(metaLine(info, { now }), { parts: ["Some channel", "3:45"], duplicate: "" });
  const duplicate = { finished_at: new Date(2026, 2, 3, 9, 0).getTime() / 1000, format_summary: "1080p MP4" };
  assert.deepEqual(metaLine({ ...info, channel: null, duration: null, duplicate }, { now }),
    { parts: [], duplicate: "already downloaded Mar 3 (1080p MP4)" });
});

test("a video already in the queue is found by site and video id, unfinished jobs only", () => {
  const job = (state, videoId = "abc", site = "youtube") => ({ id: `${state}-${videoId}`, state, spec: { site, video_id: videoId } });
  assert.equal(queuedJobFor([job("done"), job("failed")], info), null);
  assert.equal(queuedJobFor([job("done"), job("waiting")], info).id, "waiting-abc");
  assert.equal(queuedJobFor([job("downloading", "other"), job("waiting", "abc", "vimeo")], info), null);
  assert.equal(queuedJobFor([job("interrupted")], info).id, "interrupted-abc");
  assert.equal(queuedJobFor([], null), null);
});

// --- the finished file's container with another audio track ---

const RESULT_EXTS = JSON.parse(readFileSync(new globalThis.URL("./fixtures/result-exts.json", import.meta.url), "utf8"));

test("resultExt matches the server's container for every row and audio track in the recorded samples", () => {
  // tests/js/fixtures/result-exts.json comes from the server's own rule (make_result_exts.py there).
  let checked = 0;
  const failures = [];
  for (const [name, sample] of Object.entries(RESULT_EXTS)) {
    for (const [rowId, trackId, exts] of sample.cases) {
      const row = sample.rows[rowId];
      const track = trackId === null ? null : sample.tracks[trackId];
      const expected = exts.length === 1 ? exts[0] : null; // several possible: not known in advance
      const actual = resultExt(row, track);
      if (actual !== expected) failures.push(`${name}: ${rowId} + ${trackId}: ${actual} (server: ${exts.join(" or ") || "unknown"})`);
      checked += 1;
    }
  }
  assert.deepEqual(failures, []);
  assert.ok(checked > 1000, "the fixture covers the samples");
  const merged = new Set(Object.values(RESULT_EXTS).flatMap((sample) => sample.cases
    .filter(([, trackId]) => trackId !== null).map(([, , exts]) => exts[0])));
  assert.deepEqual([...merged].sort(), ["mkv", "mp4", "webm"], "the samples cover every merge outcome");
});

test("resultExt: a row's own audio keeps its container; another track can change it", () => {
  const opus = { format_id: "251", kind: "audio", ext: "webm", acodec: "opus" };
  const aac = { format_id: "140", kind: "audio", ext: "m4a", acodec: "mp4a.40.2" };
  assert.equal(resultExt(v2160, null), "mp4");
  assert.equal(resultExt(v2160, aac), "mp4", "the paired track is the row's own");
  assert.equal(resultExt(v2160, opus), "webm", "AV1 + Opus fit WebM");
  assert.equal(resultExt({ ...v2160, vcodec: "avc1.640028" }, opus), "mkv", "H.264 + Opus fit neither");
  assert.equal(resultExt({ ...v1080, audio_id: "251", result_ext: "webm" }, aac), "mkv", "VP9 + AAC fit neither");
  assert.equal(resultExt({ ...v2160, vcodec: null, ext: "mp4" }, aac), "mp4", "no codec: the containers decide");
  assert.equal(resultExt({ ...v2160, ext: "" }, opus), null, "no container: not known");
  assert.equal(resultExt({ ...v2160, has_audio: true, result_ext: "mp4" }, opus), "mp4", "a format with its own audio takes no track");
  assert.equal(resultExt({ kind: "audio", ext: "webm", acodec: "opus", result_ext: "opus" }, aac), "opus");
  assert.equal(resultExt({ kind: "mp3", ext: "mp3", result_ext: "mp3" }, null), "mp3");
  assert.equal(resultExt(null, null), null);
});

test("a trim time longer than the server's 32 characters is an error, like the server's", () => {
  assert.equal(MAX_TRIM_LENGTH, 32);
  const long = `${"0".repeat(29)}1:30`; // 33 characters that would otherwise read as 1:30
  assert.equal(long.length, 33);
  assert.equal(trimErrors(long, "").start, "Use a time of at most 32 characters");
  assert.equal(trimErrors("", long).end, "Use a time of at most 32 characters");
  assert.deepEqual(trimErrors(long.slice(1), ""), { start: null, end: null }, "32 characters are fine");
  assert.deepEqual(trimErrors(`  ${long.slice(1)}  `, ""), { start: null, end: null }, "outer spaces aren't sent, so don't count");
  assert.equal(validateExtras({ ...extras, trim_start: long }).valid, false);
});

test("visibleRows can cap the simple view, keeping the selected row listed in its place", () => {
  const rows = ["a", "b", "c", "d", "e", "f"].map((format_id) => ({ format_id, kind: "video" }));
  const formats = { video_simple: rows, video_all: [...rows, { format_id: "x", kind: "video" }], audio: [] };
  assert.deepEqual(ids(visibleRows(formats, MODE.VIDEO, { limit: 4 })), ["a", "b", "c", "d"]);
  assert.deepEqual(ids(visibleRows(formats, MODE.VIDEO, { limit: 4, selectedId: "e" })), ["a", "b", "c", "d", "e"]);
  assert.deepEqual(ids(visibleRows(formats, MODE.VIDEO, { limit: 4, selectedId: "x" })), ["a", "b", "c", "d", "x"]);
  assert.deepEqual(ids(visibleRows(formats, MODE.VIDEO, { limit: 4, selectedId: "b" })), ["a", "b", "c", "d"]);
  assert.equal(visibleRows(formats, MODE.VIDEO, { limit: 4, expanded: true }).length, 7, "expanded ignores the cap");
  assert.equal(visibleRows(formats, MODE.VIDEO, {}).length, 6, "no cap, no change");
});
