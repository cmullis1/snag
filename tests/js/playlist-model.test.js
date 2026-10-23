import test from "node:test";
import assert from "node:assert/strict";

import {
  QUALITY_CHIPS, DEFAULT_QUALITY, buildBatchRequest, countText, defaultSelection, downloadLabel, entryLabel, entryMeta,
  isAudioQuality, joinFolder, playlistFolder, playlistFolderName, qualityFor, queuedVideos, selectedIndexes,
} from "../../ui/components/playlist-model.js";

const NOW = new Date(2026, 9, 7, 15, 0);
const DUPLICATE = { finished_at: new Date(2026, 2, 3, 9, 0).getTime() / 1000, format_summary: "1080p MP4" };
const ENTRIES = [
  { index: 1, video_id: "a", title: "One", duration: 61, duplicate: null },
  { index: 2, video_id: "b", title: "Two", duration: 3725, duplicate: DUPLICATE },
  { index: 3, video_id: "c", title: "Three", duration: null, duplicate: null },
];

test("the quality chips: Best available first, then caps, MP3 and the original audio", () => {
  assert.deepEqual(QUALITY_CHIPS.map((chip) => chip.label),
    ["Best available", "≤2160p", "≤1440p", "≤1080p", "≤720p", "≤480p", "MP3", "Original audio"]);
  assert.equal(DEFAULT_QUALITY, "best");
  assert.deepEqual(qualityFor("best"), { kind: "quick", height: null });
  assert.deepEqual(qualityFor("1080"), { kind: "quick", height: 1080 });
  assert.deepEqual(qualityFor("mp3"), { kind: "mp3" });
  assert.deepEqual(qualityFor("best_audio"), { kind: "best_audio" });
  assert.deepEqual(qualityFor("nonsense"), { kind: "quick", height: null }, "unknown ids are the default");
  assert.equal(isAudioQuality("mp3"), true);
  assert.equal(isAudioQuality("best_audio"), true);
  assert.equal(isAudioQuality("720"), false);
});

test("qualityFor hands out copies", () => {
  qualityFor("best").height = 5;
  assert.deepEqual(qualityFor("best"), { kind: "quick", height: null });
});

test("videos downloaded before start unticked", () => {
  assert.deepEqual([...defaultSelection(ENTRIES)], [1, 3]);
  assert.deepEqual([...defaultSelection(null)], []);
});

const queuedJob = (state, video_id, site = "youtube") => ({ id: `${state}-${video_id}`, state, spec: { site, video_id } });

test("videos in the queue this session: waiting, running, interrupted or done, not failed or cancelled", () => {
  const jobs = [
    queuedJob("waiting", "a"), queuedJob("downloading", "b"), queuedJob("interrupted", "c"), queuedJob("done", "d"),
    queuedJob("failed", "e"), queuedJob("cancelled", "f"), queuedJob("waiting", "g", "vimeo"),
  ];
  const queued = queuedVideos(jobs, "youtube");
  assert.deepEqual([...queued].sort(), ["a", "b", "c", "d"]);
  assert.deepEqual([...queuedVideos(null, "youtube")], []);
  assert.deepEqual([...queuedVideos([{ state: "waiting" }, null], "youtube")], []);
});

test("videos already in the queue start unticked, like those downloaded before", () => {
  assert.deepEqual([...defaultSelection(ENTRIES, new Set(["c"]))], [1]);
  assert.deepEqual([...defaultSelection(ENTRIES, new Set())], [1, 3]);
});

test("the counts line and the button say how many", () => {
  assert.equal(countText(18, 5), "18 videos · 5 selected");
  assert.equal(countText(1, 1), "1 video · 1 selected");
  assert.equal(downloadLabel(5), "Download 5 videos");
  assert.equal(downloadLabel(1), "Download 1 video");
  assert.equal(downloadLabel(0), "Download");
});

test("entries are numbered like the files: at least two digits, as wide as the count", () => {
  assert.equal(entryLabel({ index: 1, title: "One" }, 18), "01 · One");
  assert.equal(entryLabel({ index: 7, title: "Seven" }, 120), "007 · Seven");
  assert.equal(entryLabel({ index: 12, title: "" }, 0), "12 · Untitled");
});

test("an entry's meta: its length and whether it's downloaded already", () => {
  assert.deepEqual(entryMeta(ENTRIES[0], { now: NOW }), { duration: "1:01", haveIt: false, haveItTitle: "", inQueue: false });
  assert.deepEqual(entryMeta(ENTRIES[1], { now: NOW }),
    { duration: "1:02:05", haveIt: true, haveItTitle: "Already downloaded Mar 3 (1080p MP4)", inQueue: false });
  assert.equal(entryMeta(ENTRIES[2], { now: NOW }).duration, "");
});

test("an entry already in the queue says so", () => {
  assert.equal(entryMeta(ENTRIES[0], { now: NOW }).inQueue, false);
  assert.equal(entryMeta(ENTRIES[0], { now: NOW, queued: new Set(["a"]) }).inQueue, true);
});

test("the playlist's folder name follows the server's safe_name", () => {
  assert.equal(playlistFolderName("Fake: Playlist?"), "Fake_ Playlist_");
  assert.equal(playlistFolderName("  Mix...  "), "Mix");
  assert.equal(playlistFolderName("a/b\\c"), "a_b_c");
  assert.equal(playlistFolderName(""), "Playlist");
  assert.equal(playlistFolderName("..."), "Playlist");
  assert.equal(playlistFolderName(null), "Playlist");
  assert.equal(playlistFolderName("CON"), "_CON");
  assert.equal(playlistFolderName("com1.txt"), "_com1.txt");
  assert.equal(playlistFolderName("Console"), "Console");
  const note = "\u{1F3B5}";
  assert.equal(Array.from(playlistFolderName(note.repeat(160))).length, 150, "150 characters as Python counts them");
  assert.equal(playlistFolderName(`${"a".repeat(149)}${note}x`), `${"a".repeat(149)}${note}`, "never half an emoji");
  assert.equal(playlistFolderName("x".repeat(200)).length, 150);
  assert.equal(playlistFolderName(`${"x".repeat(149)}. y`), "x".repeat(149));
});

test("videos go to a folder named after the playlist inside the picked or default folder", () => {
  const settings = { video_dir: "C:\\Users\\me\\Videos\\Snag", audio_dir: "C:\\Users\\me\\Music\\Snag" };
  assert.equal(playlistFolder({ settings, qualityId: "best", title: "Mix" }), "C:\\Users\\me\\Videos\\Snag\\Mix");
  assert.equal(playlistFolder({ settings, qualityId: "mp3", title: "Mix" }), "C:\\Users\\me\\Music\\Snag\\Mix");
  assert.equal(playlistFolder({ settings, qualityId: "mp3", title: "Mix", pickedBase: "D:\\" }), "D:\\Mix");
  assert.equal(playlistFolder({ settings: null, qualityId: "best", title: "Mix" }), "Mix");
  assert.equal(joinFolder("D:\\Clips", "A"), "D:\\Clips\\A");
  assert.equal(joinFolder("D:/Clips/", "A"), "D:/Clips/A");
});

test("the batch body lists the ticked videos in playlist order, with the quality, extras and folder", () => {
  const selection = new Set([3, 1]);
  assert.deepEqual(selectedIndexes(ENTRIES, selection), [1, 3]);
  const extras = { subtitles: true, trim_start: " ", trim_end: "", sponsorblock_categories: ["sponsor"] };
  assert.deepEqual(buildBatchRequest({ url: "https://y.test/list", entries: ENTRIES, selection, qualityId: "720", extras }), {
    url: "https://y.test/list", entries: [1, 3], quality: { kind: "quick", height: 720 },
    extras: { subtitles: true, trim_start: null, trim_end: null, sponsorblock_categories: ["sponsor"] },
  });
  assert.deepEqual(buildBatchRequest({
    url: "u", entries: ENTRIES, selection: new Set([2]), qualityId: "best_audio", pickedBase: "D:\\Music",
  }), { url: "u", entries: [2], quality: { kind: "best_audio" }, dest_dir: "D:\\Music" });
  assert.throws(() => buildBatchRequest({ url: "u", entries: ENTRIES, selection: new Set(), qualityId: "best" }),
    /at least one/);
  assert.throws(() => buildBatchRequest({ url: "u", entries: ENTRIES, selection: new Set([9]), qualityId: "best" }),
    /at least one/, "indexes not in the playlist don't count");
});
