import test from "node:test";
import assert from "node:assert/strict";

import { TOOL_LABELS, settingsAttention, setupScreen, toolProgress } from "../../ui/components/setup-model.js";

const MB = 1024 * 1024;
const present = (on = true) => ({ present: on, version: on ? "1" : null, message: on ? null : "missing" });
const TOOLS = { ytdlp: present(false), ffmpeg: present(false), ffprobe: present(false), deno: present(false), ready: false };

function status(setup, { tools = TOOLS, update = {} } = {}) {
  return { tools, updater: { setup: { state: "idle", message: null, can_retry: false, tools: {}, ...setup }, update } };
}

test("the setup screen shows only while setup runs or after it failed", () => {
  assert.equal(setupScreen(status({ state: "idle" }), {}), null);
  assert.equal(setupScreen(status({ state: "done" }), {}), null);
  assert.equal(setupScreen({}, {}), null, "no status yet (before the snapshot)");
  assert.equal(setupScreen(null, null), null);
  assert.equal(setupScreen(status({ state: "running" }), {}).state, "running");
  assert.equal(setupScreen(status({ state: "error", message: "No internet", can_retry: true }), {}).state, "error");
});

test("while it runs: a title, what's happening, and every tool in install order", () => {
  const screen = setupScreen(status({ state: "running" }), {
    "yt-dlp": { phase: "downloading", done: 6 * MB, total: 12 * MB, message: null },
  });
  assert.equal(screen.title, "Setting up Snag…");
  assert.match(screen.message, /once/);
  assert.equal(screen.canRetry, false);
  assert.deepEqual(screen.tools.map((tool) => tool.label), ["yt-dlp", "ffmpeg", "Deno"]);
  assert.deepEqual(screen.tools[0], {
    name: "yt-dlp", label: "yt-dlp", text: "Downloading · 6 MB of 12 MB", value: 50, indeterminate: false, tone: null,
  });
  assert.equal(screen.tools[1].text, "Waiting");
});

test("after a failure: the server's message and Retry", () => {
  const screen = setupScreen(status({ state: "error", message: "Snag couldn't finish setting up. No internet.", can_retry: true }), {
    "yt-dlp": { phase: "done", done: 0, total: null, message: null },
    ffmpeg: { phase: "error", done: 0, total: null, message: "Couldn't reach GitHub" },
  });
  assert.equal(screen.title, "Snag couldn't finish setting up");
  assert.equal(screen.message, "Snag couldn't finish setting up. No internet.");
  assert.equal(screen.canRetry, true);
  assert.equal(screen.tools[0].text, "Ready");
  assert.deepEqual(screen.tools[1], {
    name: "ffmpeg", label: "ffmpeg", text: "Couldn't reach GitHub", value: null, indeterminate: false, tone: "danger",
  });
});

test("a tool that was already installed reads Ready, not Waiting", () => {
  const tools = { ...TOOLS, ytdlp: present(), ffmpeg: present(), ffprobe: present(false) };
  const screen = setupScreen(status({ state: "running" }, { tools }), {});
  assert.equal(screen.tools[0].text, "Ready");
  assert.equal(screen.tools[1].text, "Waiting", "ffmpeg counts only with ffprobe");
});

test("progress text for each phase", () => {
  assert.deepEqual(toolProgress({ phase: "downloading", done: 3 * MB, total: null }),
    { text: "Downloading · 3 MB", value: null, indeterminate: true, tone: null });
  assert.deepEqual(toolProgress({ phase: "downloading", done: 0, total: 0 }),
    { text: "Downloading", value: null, indeterminate: true, tone: null });
  assert.equal(toolProgress({ phase: "downloading", done: 5, total: 4 }).value, 100);
  assert.deepEqual(toolProgress({ phase: "verifying" }), { text: "Checking the download", value: null, indeterminate: true, tone: null });
  assert.deepEqual(toolProgress({ phase: "installing" }), { text: "Installing", value: null, indeterminate: true, tone: null });
  assert.deepEqual(toolProgress({ phase: "waiting" }),
    { text: "Waiting for downloads to finish", value: null, indeterminate: true, tone: null });
  assert.deepEqual(toolProgress({ phase: "done" }), { text: "Ready", value: 100, indeterminate: false, tone: "done" });
  assert.deepEqual(toolProgress({ phase: "error", message: null }),
    { text: "Couldn't install it", value: null, indeterminate: false, tone: "danger" });
  assert.deepEqual(toolProgress(null), { text: "Waiting", value: null, indeterminate: false, tone: null });
  assert.deepEqual(toolProgress({ phase: null }, { present: true }), { text: "Ready", value: 100, indeterminate: false, tone: "done" });
  assert.equal(TOOL_LABELS.deno, "Deno");
});

test("the Settings dot: a missing tool, then Deno, then an update", () => {
  const ok = { ytdlp: present(), ffmpeg: present(), ffprobe: present(), deno: present() };
  assert.equal(settingsAttention({ tools: ok, updater: { update: { available: null } } }), null);
  assert.equal(settingsAttention({}), null);
  assert.equal(settingsAttention(null), null);
  assert.deepEqual(settingsAttention({ tools: ok, updater: { update: { available: "2026.09.01" } } }),
    { tone: "accent", label: "yt-dlp 2026.09.01 is available" });
  assert.deepEqual(settingsAttention({ tools: { ...ok, deno: present(false) }, updater: { update: { available: "2026.09.01" } } }),
    { tone: "warning", label: "Deno is missing" });
  assert.deepEqual(settingsAttention({ tools: { ...ok, ffprobe: present(false), deno: present(false) } }),
    { tone: "danger", label: "ffmpeg is missing" });
  assert.deepEqual(settingsAttention({ tools: { ...ok, ytdlp: present(false) } }),
    { tone: "danger", label: "yt-dlp is missing" });
});
