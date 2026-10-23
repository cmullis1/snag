import test from "node:test";
import assert from "node:assert/strict";

import {
  formatSize, formatDuration, formatEta, formatSpeed, formatPercent, formatDate, formatDay, formatClock,
  formatAgo, codecName, containerName, languageName, rowLabel, extrasSummary, trimSeconds,
  alreadyDownloadedText, sponsorCategoryName, toDate,
} from "../../ui/format.js";

// A fixed "now": Wednesday 7 October 2026, 15:30 local time.
const NOW = new Date(2026, 9, 7, 15, 30, 0);
const secondsAt = (...parts) => new Date(...parts).getTime() / 1000;

test("sizes use binary units and a ~ for estimates", () => {
  assert.equal(formatSize(0), "0 bytes");
  assert.equal(formatSize(512), "512 bytes");
  assert.equal(formatSize(2048), "2 KB");
  assert.equal(formatSize(4.25 * 1024 ** 2), "4.3 MB");
  assert.equal(formatSize(218 * 1024 ** 2 + 5000), "218 MB");
  assert.equal(formatSize(1.4 * 1024 ** 3, { estimate: true }), "~1.4 GB");
  assert.equal(formatSize(150 * 1024 ** 3), "150 GB");
  assert.equal(formatSize(2.5 * 1024 ** 4), "2.5 TB");
});

test("unknown sizes are empty", () => {
  assert.equal(formatSize(null), "");
  assert.equal(formatSize(undefined), "");
  assert.equal(formatSize(Number.NaN), "");
  assert.equal(formatSize(-1), "");
});

test("a size just under the next unit doesn't read 1024", () => {
  assert.equal(formatSize(1024 ** 2 - 1), "1 MB");
  assert.equal(formatSize(1024 - 1), "1023 bytes");
  assert.equal(formatSize(1023.6), "1 KB", "rounded before the unit is chosen");
  assert.equal(formatSize(1023.4), "1023 bytes");
  assert.equal(formatSize(1024 ** 3 - 1), "1 GB");
});

test("one byte is singular", () => {
  assert.equal(formatSize(1), "1 byte");
  assert.equal(formatSize(1.2), "1 byte");
  assert.equal(formatSize(0.4), "0 bytes");
  assert.equal(formatSize(1, { estimate: true }), "~1 byte");
  assert.equal(formatSpeed(1), "1 byte/s");
});

test("durations are m:ss or h:mm:ss", () => {
  assert.equal(formatDuration(0), "0:00");
  assert.equal(formatDuration(59.9), "0:59");
  assert.equal(formatDuration(61), "1:01");
  assert.equal(formatDuration(3599), "59:59");
  assert.equal(formatDuration(3600), "1:00:00");
  assert.equal(formatDuration(36000 + 62), "10:01:02");
  assert.equal(formatDuration(null), "");
  assert.equal(formatDuration(-5), "");
});

test("time left reads in plain words", () => {
  assert.equal(formatEta(0), "Almost done");
  assert.equal(formatEta(0.4), "Almost done");
  assert.equal(formatEta(1), "1 sec left");
  assert.equal(formatEta(45), "45 sec left");
  assert.equal(formatEta(60), "1 min left");
  assert.equal(formatEta(61), "2 min left");
  assert.equal(formatEta(3600), "1 h left");
  assert.equal(formatEta(3600 + 5 * 60), "1 h 5 min left");
  assert.equal(formatEta(2 * 3600 + 59 * 60 + 30), "3 h left");
  assert.equal(formatEta(null), "");
  assert.equal(formatEta(-1), "");
});

test("time left picks its unit after rounding, so it never reads 60 sec or 60 min", () => {
  assert.equal(formatEta(58.2), "59 sec left");
  assert.equal(formatEta(59), "59 sec left");
  assert.equal(formatEta(59.2), "1 min left");
  assert.equal(formatEta(3540), "59 min left");
  assert.equal(formatEta(3540.5), "1 h left");
  assert.equal(formatEta(3599), "1 h left");
  assert.equal(formatEta(3600 + 59 * 60 + 1), "2 h left");
});

test("speeds are per second", () => {
  assert.equal(formatSpeed(3.4 * 1024 ** 2), "3.4 MB/s");
  assert.equal(formatSpeed(800), "800 bytes/s");
  assert.equal(formatSpeed(null), "");
  assert.equal(formatSpeed(0), "");
});

test("percent is a whole number, never 100 before it's done", () => {
  assert.equal(formatPercent(42.4), "42%");
  assert.equal(formatPercent(99.7), "99%");
  assert.equal(formatPercent(100), "100%");
  assert.equal(formatPercent(null), "");
});

test("dates are relative to now", () => {
  assert.equal(formatDate(secondsAt(2026, 9, 7, 14, 2), { now: NOW }), "Today 14:02");
  assert.equal(formatDate(secondsAt(2026, 9, 6, 9, 5), { now: NOW }), "Yesterday 09:05");
  assert.equal(formatDate(secondsAt(2026, 2, 3, 9, 5), { now: NOW }), "Mar 3 09:05");
  assert.equal(formatDate(secondsAt(2025, 11, 31, 23, 59), { now: NOW }), "Dec 31, 2025");
  assert.equal(formatDate(null, { now: NOW }), "");
});

test("yesterday works across a month boundary", () => {
  const now = new Date(2026, 9, 1, 8, 0);
  assert.equal(formatDate(secondsAt(2026, 8, 30, 22, 15), { now }), "Yesterday 22:15");
});

test("days without times, for notes", () => {
  assert.equal(formatDay(secondsAt(2026, 9, 7, 1, 0), { now: NOW }), "today");
  assert.equal(formatDay(secondsAt(2026, 9, 6, 23, 0), { now: NOW }), "yesterday");
  assert.equal(formatDay(secondsAt(2026, 2, 3, 12, 0), { now: NOW }), "Mar 3");
  assert.equal(formatDay(secondsAt(2024, 2, 3, 12, 0), { now: NOW }), "Mar 3, 2024");
});

test("dates accept seconds, ISO text and Date objects", () => {
  const date = new Date(2026, 9, 7, 14, 2);
  assert.equal(toDate(date.getTime() / 1000).getTime(), date.getTime());
  assert.equal(toDate(date.toISOString()).getTime(), date.getTime());
  assert.equal(toDate(date).getTime(), date.getTime());
  assert.equal(toDate("not a date"), null);
  assert.equal(toDate(null), null);
});

test("clock times are 24-hour", () => {
  assert.equal(formatClock(secondsAt(2026, 9, 7, 14, 32)), "14:32");
  assert.equal(formatClock(secondsAt(2026, 9, 7, 7, 5)), "07:05");
});

test("how long ago", () => {
  const ago = (seconds) => formatAgo(NOW.getTime() / 1000 - seconds, { now: NOW });
  assert.equal(ago(20), "just now");
  assert.equal(ago(90), "1 min ago");
  assert.equal(ago(12 * 60), "12 min ago");
  assert.equal(ago(3 * 3600), "3 h ago");
  assert.equal(ago(5 * 86400), "5 days ago");
  assert.equal(formatAgo(NOW.getTime() / 1000 + 30, { now: NOW }), "just now");
  assert.equal(formatAgo(null, { now: NOW }), "");
});

test("how long ago counts calendar days, like formatDay", () => {
  // NOW is 15:30 on Oct 7.
  assert.equal(formatAgo(secondsAt(2026, 9, 6, 23, 0), { now: NOW }), "yesterday", "16.5 hours ago, but yesterday");
  assert.equal(formatDay(secondsAt(2026, 9, 6, 23, 0), { now: NOW }), "yesterday");
  assert.equal(formatAgo(secondsAt(2026, 9, 5, 13, 30), { now: NOW }), "2 days ago", "26 hours ago, two days back");
  assert.equal(formatAgo(secondsAt(2026, 9, 7, 0, 30), { now: NOW }), "15 h ago", "earlier today");
  const justAfterMidnight = new Date(2026, 9, 7, 0, 5);
  assert.equal(formatAgo(secondsAt(2026, 9, 6, 23, 50), { now: justAfterMidnight }), "15 min ago");
  assert.equal(formatAgo(secondsAt(2026, 9, 6, 21, 0), { now: justAfterMidnight }), "yesterday");
});

test("codec names", () => {
  assert.equal(codecName("av01.0.08M.08"), "AV1");
  assert.equal(codecName("vp09.00.40.08"), "VP9");
  assert.equal(codecName("vp9"), "VP9");
  assert.equal(codecName("avc1.64002A"), "H.264");
  assert.equal(codecName("hev1.1.6.L120.90"), "HEVC");
  assert.equal(codecName("hvc1"), "HEVC");
  assert.equal(codecName("mp4a.40.2"), "AAC");
  assert.equal(codecName("opus"), "Opus");
  assert.equal(codecName("vorbis"), "Vorbis");
  assert.equal(codecName("mp3"), "MP3");
  assert.equal(codecName("flac"), "FLAC");
  assert.equal(codecName("ac-3"), "AC-3");
  assert.equal(codecName("ec-3"), "E-AC-3");
  assert.equal(codecName("theora"), "theora");
  assert.equal(codecName("none"), "");
  assert.equal(codecName(null), "");
});

test("codec names tolerate values that aren't text", () => {
  assert.equal(codecName(42), "");
  assert.equal(codecName({ codec: "avc1" }), "");
  assert.equal(codecName(["avc1"]), "");
  assert.equal(codecName(true), "");
});

test("container names are upper case", () => {
  assert.equal(containerName("mp4"), "MP4");
  assert.equal(containerName("webm"), "WEBM");
  assert.equal(containerName(null), "");
});

test("language names", () => {
  assert.equal(languageName("en"), "English");
  assert.equal(languageName("de"), "German");
  assert.equal(languageName("pt-BR"), "Brazilian Portuguese");
  assert.equal(languageName("zz-not-a-language-code"), "zz-not-a-language-code");
  assert.equal(languageName(null), "");
  assert.equal(languageName(""), "");
});

test("an undetermined language reads as unknown", () => {
  assert.equal(languageName("und"), "Unknown language");
  assert.equal(languageName("UND"), "Unknown language");
  assert.equal(languageName(5), "");
});

test("row labels fall back when the server sent none", () => {
  assert.equal(rowLabel({ label: "1080p60", kind: "video", height: 1080, fps: 60 }), "1080p60");
  assert.equal(rowLabel({ label: "", kind: "video", height: 720, fps: 30 }), "720p");
  assert.equal(rowLabel({ label: "", kind: "video", height: 1080, fps: 59.94 }), "1080p60");
  assert.equal(rowLabel({ label: "", kind: "audio", acodec: "opus", abr: 129.5 }), "Opus 130k");
  assert.equal(rowLabel({ label: "", kind: "mp3" }), "MP3");
});

test("trim times", () => {
  assert.equal(trimSeconds("90"), 90);
  assert.equal(trimSeconds("1:30"), 90);
  assert.equal(trimSeconds("1:02:03.5"), 3723.5);
  assert.equal(trimSeconds("125:00"), 7500);
  assert.equal(trimSeconds("1:60"), null);
  assert.equal(trimSeconds("1:2:3:4"), null);
  assert.equal(trimSeconds("abc"), null);
  assert.equal(trimSeconds(""), null);
  assert.equal(trimSeconds(null), null);
});

const EXTRAS = {
  subtitles: true, subtitle_lang: "en", subtitle_mode: "embed", thumbnail_tags: true, chapters: true,
  sponsorblock: false, sponsorblock_mode: "remove", sponsorblock_categories: ["sponsor", "selfpromo", "interaction"],
  trim_start: null, trim_end: null,
};

test("extras summary: the defaults", () => {
  assert.equal(extrasSummary(EXTRAS), "English subtitles, thumbnail and tags, chapters");
});

test("extras summary: nothing chosen", () => {
  const none = { ...EXTRAS, subtitles: false, thumbnail_tags: false, chapters: false };
  assert.equal(extrasSummary(none), "No extras");
  assert.equal(extrasSummary(null), "No extras");
});

test("extras summary: subtitle file, SponsorBlock", () => {
  const extras = { ...EXTRAS, subtitle_mode: "file", subtitle_lang: "de", thumbnail_tags: false, sponsorblock: true };
  assert.equal(extrasSummary(extras), "German subtitles as a file, chapters, SponsorBlock removes 3 kinds of segment");
  const mark = { ...extras, sponsorblock_mode: "mark", sponsorblock_categories: ["sponsor"] };
  assert.equal(extrasSummary(mark), "German subtitles as a file, chapters, SponsorBlock marks sponsor segments as chapters");
  const markMany = { ...extras, sponsorblock_mode: "mark" };
  assert.equal(
    extrasSummary(markMany), "German subtitles as a file, chapters, SponsorBlock marks 3 kinds of segment as chapters",
  );
  const removeOne = { ...extras, sponsorblock_categories: ["selfpromo"] };
  assert.equal(extrasSummary(removeOne), "German subtitles as a file, chapters, SponsorBlock removes self-promotion segments");
});

test("extras summary: audio only drops subtitles", () => {
  assert.equal(extrasSummary(EXTRAS, { audioOnly: true }), "Thumbnail and tags, chapters");
});

test("extras summary: trims say what isn't available", () => {
  const endOnly = { ...EXTRAS, trim_end: "2:00", sponsorblock: true };
  assert.equal(
    extrasSummary(endOnly),
    "English subtitles, thumbnail and tags, trim to 2:00 (chapters and SponsorBlock not available when trimming)",
  );
  const both = { ...EXTRAS, trim_start: "1:30", trim_end: "2:00" };
  assert.equal(
    extrasSummary(both),
    "Thumbnail and tags, trim 1:30–2:00 (chapters not available when trimming; subtitles not available when trimming from a start time)",
  );
  const startOnlySubtitles = { ...EXTRAS, chapters: false, thumbnail_tags: false, trim_start: "0:10" };
  assert.equal(
    extrasSummary(startOnlySubtitles), "Trim from 0:10 (subtitles not available when trimming from a start time)",
  );
  const fromZero = { ...EXTRAS, chapters: false, trim_start: "0", trim_end: "2:00" };
  assert.equal(extrasSummary(fromZero), "English subtitles, thumbnail and tags, trim 0–2:00");
  const startOnly = { ...EXTRAS, thumbnail_tags: false, chapters: false, subtitles: false, trim_start: "10" };
  assert.equal(extrasSummary(startOnly), "Trim from 10");
});

test("already downloaded note", () => {
  const entry = { finished_at: secondsAt(2026, 2, 3, 12, 0), format_summary: "1080p MP4" };
  assert.equal(alreadyDownloadedText(entry, { now: NOW }), "Already downloaded Mar 3 (1080p MP4)");
  const today = { finished_at: secondsAt(2026, 9, 7, 9, 0), format_summary: "" };
  assert.equal(alreadyDownloadedText(today, { now: NOW }), "Already downloaded today");
  assert.equal(alreadyDownloadedText(null, { now: NOW }), "");
});

test("already downloaded note without a date has no double space", () => {
  assert.equal(alreadyDownloadedText({ finished_at: null, format_summary: "1080p MP4" }, { now: NOW }), "Already downloaded (1080p MP4)");
  assert.equal(alreadyDownloadedText({ finished_at: "garbage", format_summary: "" }, { now: NOW }), "Already downloaded");
});

test("SponsorBlock category names", () => {
  assert.equal(sponsorCategoryName("sponsor"), "Sponsor");
  assert.equal(sponsorCategoryName("selfpromo"), "Self-promotion");
  assert.equal(sponsorCategoryName("interaction"), "Interaction reminder");
  assert.equal(sponsorCategoryName("music_offtopic"), "Non-music section");
  assert.equal(sponsorCategoryName("new_one"), "new_one");
});
