// The pure helpers the DOM components export (their rendering is checked in a real browser).
import test from "node:test";
import assert from "node:assert/strict";

import { folderLabel } from "../../ui/components/folder-line.js";
import { formatDetail, fileLabel, showAllLabel } from "../../ui/components/format-list.js";

test("folders show their last two parts, with the full path kept for the tooltip", () => {
  assert.equal(folderLabel("C:\\Users\\me\\Videos\\Snag"), "Videos\\Snag");
  assert.equal(folderLabel("D:\\Clips\\"), "D:\\Clips");
  assert.equal(folderLabel("D:\\"), "D:\\");
  assert.equal(folderLabel("\\\\server\\share\\Music"), "share\\Music");
  assert.equal(folderLabel(""), "");
  assert.equal(folderLabel(null), "");
});

test("the expanded view's stream column: the stream's own container, codec, frame rate, bitrate, then flags", () => {
  const video = { kind: "video", ext: "webm", vcodec: "vp09.00.51.08", fps: 60, tbr: 4180.3, dynamic_range: "HDR10" };
  assert.equal(formatDetail(video), "WEBM · VP9 · 60 fps · 4.2 Mbps · HDR10");
  // 30 fps and under is shown too (the quality label leaves it out).
  assert.equal(formatDetail({ kind: "video", ext: "mp4", vcodec: "avc1.4d401f", fps: 30, tbr: 950, dynamic_range: "SDR" }),
    "MP4 · H.264 · 30 fps · 950 kbps");
  assert.equal(formatDetail({ kind: "video", ext: "mp4", vcodec: "avc1", fps: 23.976 }), "MP4 · H.264 · 24 fps");
  assert.equal(formatDetail({ kind: "video", ext: "mp4", vcodec: "avc1.42001E", fps: 30, tbr: 650, has_audio: true }),
    "MP4 · H.264 · 30 fps · 650 kbps · includes audio");
  assert.equal(formatDetail({ kind: "video", ext: "mp4", vcodec: "avc1", upscaled: true }), "MP4 · H.264 · AI-upscaled");
  assert.equal(formatDetail({ kind: "video", ext: "mp4", vcodec: 7, fps: null }), "MP4", "a codec that isn't text is left out");
  assert.equal(formatDetail({ kind: "audio", ext: "webm", acodec: "opus", abr: 135.2, language: "de", is_original: false }),
    "WEBM · Opus · 135 kbps · German");
  assert.equal(formatDetail({ kind: "audio", ext: "m4a", acodec: "mp4a.40.2", abr: 129, language: "en", is_original: true }),
    "M4A · AAC · 129 kbps · English (original)");
  assert.equal(formatDetail({ kind: "mp3" }), "Converted, 192 kbps");
});

test("the file column is the finished file's container", () => {
  assert.equal(fileLabel({ ext: "webm", result_ext: "mp4" }), "MP4");
  assert.equal(fileLabel({ ext: "webm", result_ext: null }), "");
});

test("the show-all link names how many formats there are", () => {
  assert.equal(showAllLabel(12, false), "Show all 12 formats");
  assert.equal(showAllLabel(1, false), "Show all formats");
  assert.equal(showAllLabel(12, true), "Show fewer formats");
});
