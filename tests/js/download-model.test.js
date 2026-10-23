import test from "node:test";
import assert from "node:assert/strict";

import { LINK_LOST, PRESS_LOOK_UP, isLink, isYouTubeLink, lookupTarget, playlistSummary, readAlike } from "../../web/download-model.js";

test("a pasted link is looked up as it is", () => {
  assert.deepEqual(lookupTarget("https://www.youtube.com/watch?v=abc"), { url: "https://www.youtube.com/watch?v=abc" });
  assert.deepEqual(lookupTarget("  https://youtu.be/abc \n"), { url: "https://youtu.be/abc" });
  assert.deepEqual(lookupTarget("http://example.test/v"), { url: "http://example.test/v" });
});

test("the first link in pasted text is used", () => {
  assert.deepEqual(lookupTarget("Watch this: https://youtu.be/abc it's great https://youtu.be/def"),
    { url: "https://youtu.be/abc" });
});

test("a link without its scheme gets https", () => {
  assert.deepEqual(lookupTarget("youtube.com/watch?v=abc"), { url: "https://youtube.com/watch?v=abc" });
  assert.deepEqual(lookupTarget("www.youtube.com/playlist?list=PL1"), { url: "https://www.youtube.com/playlist?list=PL1" });
  assert.deepEqual(lookupTarget("youtu.be/abc"), { url: "https://youtu.be/abc" });
});

test("other schemes yt-dlp knows are passed on", () => {
  assert.deepEqual(lookupTarget("fake://video"), { url: "fake://video" });
});

test("empty text is nothing to look up; other text is an error", () => {
  assert.deepEqual(lookupTarget("   "), { url: null, error: null });
  assert.deepEqual(lookupTarget(null), { url: null, error: null });
  const error = "That isn't a link. Paste the address of a video or playlist.";
  assert.deepEqual(lookupTarget("cat videos"), { url: null, error });
  assert.deepEqual(lookupTarget("-o x"), { url: null, error });
  assert.deepEqual(lookupTarget("javascript:alert(1)"), { url: null, error });
});

test("a playlist's summary names it and counts its videos", () => {
  const entries = [{ duplicate: null }, { duplicate: { id: 1 } }, { duplicate: null }];
  assert.deepEqual(playlistSummary({ title: "Mix", entries }), { title: "Mix", text: "3 videos · 1 downloaded before" });
  assert.deepEqual(playlistSummary({ title: "", entries: [{}] }), { title: "Playlist", text: "1 video" });
  assert.deepEqual(playlistSummary({ title: "Empty", entries: [] }), { title: "Empty", text: "No videos" });
});

test("isLink: text lookupTarget would find a link in", () => {
  assert.equal(isLink("https://www.youtube.com/watch?v=abc"), true);
  assert.equal(isLink("https://x.test/a%ZZb"), true);
  assert.equal(isLink("%E0%A4%A"), false);
  assert.equal(isLink(""), false);
  assert.equal(LINK_LOST, "That link didn't come through. Paste it again.");
});

test("YouTube links: youtube.com and its subdomains, youtu.be, youtube-nocookie.com", () => {
  for (const url of [
    "https://www.youtube.com/watch?v=abc", "https://youtube.com/playlist?list=PL1", "https://m.youtube.com/watch?v=abc",
    "https://music.youtube.com/watch?v=abc", "https://youtu.be/abc", "http://www.youtube.com/watch?v=abc",
    "https://www.youtube-nocookie.com/embed/abc", "https://youtube-nocookie.com/embed/abc", "https://WWW.YouTube.com/watch?v=abc",
  ]) assert.equal(isYouTubeLink(url), true, url);
  for (const url of [
    "https://vimeo.com/1", "https://www.youtube.com.evil.test/watch?v=abc", "https://evilyoutube.com/x",
    "https://youtu.be.evil.test/abc", "http://127.0.0.1:8080/x", "file:///C:/x", "javascript:alert(1)", "not a link", null, "",
  ]) assert.equal(isYouTubeLink(url), false, String(url));
  assert.equal(PRESS_LOOK_UP, "Press Look up to look up this link.");
});

test("readAlike: addresses the server's lookup treats the same way (snag/info/lookup.py classify_url)", () => {
  const same = [
    ["https://www.youtube.com/watch?v=abc", "https://youtu.be/abc"],
    ["https://www.youtube.com/watch?v=abc&list=PL1", "https://m.youtube.com/watch?v=xyz&list=PL1&index=2"],
    ["https://www.youtube.com/playlist?list=PL1", "https://music.youtube.com/playlist?list=PL1&x=1"],
    ["https://vimeo.com/1", "https://example.org/other"],
    ["https://www.youtube.com/watch?v=abc&list=", "https://www.youtube.com/watch?v=abc"],
    ["https://www.youtube.com/watch?v=abc&list=&list=PL1", "https://www.youtube.com/watch?v=abc&list=PL1"],
    ["https://www.youtube.com/playlist", "https://www.youtube.com/watch?v=abc"], // no list: just a video, as on the server
  ];
  for (const [a, b] of same) assert.equal(readAlike(a, b), true, `${a} ${b}`);
  const different = [
    ["https://www.youtube.com/watch?v=abc&list=PL1", "https://www.youtube.com/watch?v=abc"],
    ["https://www.youtube.com/watch?v=abc&list=PL1", "https://www.youtube.com/watch?v=abc&list=PL2"],
    ["https://www.youtube.com/playlist?list=PL1", "https://www.youtube.com/watch?v=abc&list=PL1"],
    ["https://www.youtube.com/playlist", "https://vimeo.com/1"],
    ["https://www.youtube.com/watch?v=abc", "https://vimeo.com/1"],
  ];
  for (const [a, b] of different) assert.equal(readAlike(a, b), false, `${a} ${b}`);
  assert.equal(readAlike("not a link", "also not"), true, "neither is YouTube's: both are left to yt-dlp");
});
