import test from "node:test";
import assert from "node:assert/strict";

import {
  HEIGHT_OPTIONS, MAX_CONCURRENT, MIN_CONCURRENT, changedFields, clampConcurrent, heightFromOption, loginText, optionOfHeight,
  saveErrorText, savedText, toolRow, ytdlpRow,
} from "../../ui/components/settings-model.js";

const NOW = new Date(Date.UTC(2026, 9, 7, 14, 30));
const MB = 1024 * 1024;

function status({ tools = {}, update = {} } = {}) {
  const ok = (version) => ({ present: true, version, message: null });
  return {
    tools: { ytdlp: ok("2026.08.19"), ffmpeg: ok("7.1"), ffprobe: ok("7.1"), deno: ok("2.4.1"), ...tools },
    updater: {
      update: {
        current: "2026.08.19", latest: "2026.08.19", available: null, last_check: null, state: "idle", message: null,
        error: null, ...update,
      },
    },
  };
}

test("max quality: Best available first, then caps down to 360p", () => {
  assert.deepEqual(HEIGHT_OPTIONS.map((option) => option.label),
    ["Best available", "Up to 2160p", "Up to 1440p", "Up to 1080p", "Up to 720p", "Up to 480p", "Up to 360p"]);
  assert.equal(optionOfHeight(null), "");
  assert.equal(optionOfHeight(1080), "1080");
  assert.equal(heightFromOption(""), null);
  assert.equal(heightFromOption("1080"), 1080);
  assert.equal(heightFromOption("999"), null, "a height the server refuses is the best available");
});

test("downloads at once stays from 1 to 10", () => {
  assert.equal(MIN_CONCURRENT, 1);
  assert.equal(MAX_CONCURRENT, 10);
  assert.equal(clampConcurrent(0), 1);
  assert.equal(clampConcurrent(11), 10);
  assert.equal(clampConcurrent(3.6), 4);
  assert.equal(clampConcurrent("7"), 7);
  assert.equal(clampConcurrent("lots"), 5, "nonsense is the default");
});

test("only fields that changed are saved", () => {
  const saved = {
    video_dir: "C:\\V", max_concurrent: 5, quick_max_height: null,
    quick_extras: { subtitles: true, sponsorblock_categories: ["sponsor"], trim_start: null },
  };
  assert.deepEqual(changedFields(saved, { max_concurrent: 5, video_dir: "C:\\V" }), {});
  assert.deepEqual(changedFields(saved, { max_concurrent: 3 }), { max_concurrent: 3 });
  assert.deepEqual(changedFields(saved, { quick_max_height: 1080 }), { quick_max_height: 1080 });
  assert.deepEqual(changedFields(saved, {
    quick_extras: { trim_start: null, sponsorblock_categories: ["sponsor"], subtitles: true },
  }), {}, "objects compare by value, whatever the key order");
  assert.deepEqual(changedFields(saved, {
    quick_extras: { subtitles: true, sponsorblock_categories: ["sponsor", "intro"], trim_start: null },
  }), { quick_extras: { subtitles: true, sponsorblock_categories: ["sponsor", "intro"], trim_start: null } });
  assert.deepEqual(changedFields(null, { max_concurrent: 2 }), { max_concurrent: 2 });
});

test("a refused save shows the server's message", () => {
  assert.equal(saveErrorText({ message: "That setting isn't valid: max_concurrent: must be 1-10" }),
    "That setting isn't valid: max_concurrent: must be 1-10");
  assert.equal(saveErrorText(null), "Couldn't save that setting");
});

test("a save says Saved when the server kept what was sent, and otherwise who changed it", () => {
  assert.equal(savedText("start_with_windows", true), "Saved");
  assert.equal(savedText("start_with_windows", false), "Windows didn't keep this");
  assert.equal(savedText("video_dir", false), "Snag adjusted this");
  assert.equal(savedText("max_concurrent", true), "Saved");
});

test("the YouTube login line", () => {
  assert.deepEqual(loginText("2026-10-07T14:18:00Z", { now: NOW }), { available: true, text: "From your browser · 12 min ago" });
  assert.deepEqual(loginText(null, { now: NOW }),
    { available: false, text: "Not available: sign in to YouTube in Chrome or Edge" });
  assert.deepEqual(loginText("garbage", { now: NOW }).available, false);
});

test("yt-dlp: its version, and an update when one is available", () => {
  assert.deepEqual(ytdlpRow(status(), {}, { now: NOW }),
    { text: "yt-dlp 2026.08.19", missing: false, updateLabel: null, busy: false, note: "", noteTone: null });
  const available = ytdlpRow(status({ update: { available: "2026.09.30" } }), {}, { now: NOW });
  assert.equal(available.updateLabel, "Update to 2026.09.30");
});

test("yt-dlp while updating, checking, and after", () => {
  const installing = status({ update: { available: "2026.09.30", state: "installing" } });
  const row = ytdlpRow(installing, { "yt-dlp": { phase: "downloading", done: 2 * MB, total: 4 * MB } }, { now: NOW });
  assert.equal(row.busy, true);
  assert.equal(row.updateLabel, null);
  assert.equal(row.note, "Downloading · 2 MB of 4 MB");
  assert.equal(ytdlpRow(status({ update: { state: "checking" } }), {}, { now: NOW }).note, "Checking for an update…");
  assert.deepEqual(
    (({ note, noteTone }) => ({ note, noteTone }))(ytdlpRow(status({ update: { message: "Updated to 2026.09.30" } }), {}, { now: NOW })),
    { note: "Updated to 2026.09.30", noteTone: "success" });
  assert.equal(ytdlpRow(status({ update: { message: "Update didn't work: still on version 2026.08.19" } }), {}, { now: NOW }).noteTone,
    "danger");
  assert.equal(ytdlpRow(status({ update: { error: "GitHub didn't answer" } }), {}, { now: NOW }).note,
    "Couldn't check for an update: GitHub didn't answer");
  assert.equal(ytdlpRow(status({ update: { last_check: "2026-10-07T14:18:00Z" } }), {}, { now: NOW }).note, "Checked 12 min ago");
  assert.equal(ytdlpRow(status({ update: { message: "Reinstalled ffmpeg" } }), {}, { now: NOW }).note, "",
    "a reinstall's message belongs to that tool");
});

test("yt-dlp missing", () => {
  const row = ytdlpRow(status({ tools: { ytdlp: { present: false, version: null, message: "yt-dlp is missing" } } }), {}, { now: NOW });
  assert.equal(row.missing, true);
  assert.equal(row.text, "yt-dlp is missing");
});

test("ffmpeg and Deno: version or what's wrong, Reinstall progress, and its result", () => {
  assert.deepEqual(toolRow(status(), {}, "ffmpeg"),
    { label: "ffmpeg", text: "7.1", missing: false, busy: false, note: "", noteTone: null });
  const missing = toolRow(status({ tools: { ffprobe: { present: false, version: null, message: "ffprobe is missing" } } }), {}, "ffmpeg");
  assert.equal(missing.missing, true);
  assert.equal(missing.text, "ffprobe is missing");
  const deno = toolRow(status({ tools: { deno: { present: false, version: null, message: null } } }), {}, "deno");
  assert.deepEqual([deno.label, deno.text, deno.missing], ["Deno", "Missing, so some YouTube formats won't show", true]);
  const busy = toolRow(status({ update: { state: "installing" } }), { deno: { phase: "waiting" } }, "deno");
  assert.equal(busy.busy, true);
  assert.equal(busy.note, "Waiting for downloads to finish");
  assert.equal(toolRow(status(), { deno: { phase: "done" } }, "deno").busy, false, "a finished reinstall isn't busy");
  assert.deepEqual(
    (({ note, noteTone }) => ({ note, noteTone }))(toolRow(status({ update: { message: "Reinstalled Deno" } }), {}, "deno")),
    { note: "Reinstalled Deno", noteTone: "success" });
  assert.equal(toolRow(status({ update: { message: "Couldn't reinstall ffmpeg: offline" } }), {}, "ffmpeg").noteTone, "danger");
  assert.equal(toolRow(status({ update: { message: "Couldn't reinstall ffmpeg: offline" } }), {}, "deno").note, "");
});
