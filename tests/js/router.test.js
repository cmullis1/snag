import test from "node:test";
import assert from "node:assert/strict";

import { ROUTE, buildHash, parseHash, tabOf } from "../../web/router.js";

test("each tab has its own hash; anything else is the Download tab", () => {
  assert.deepEqual(parseHash("#download"), { route: ROUTE.DOWNLOAD, params: {} });
  assert.deepEqual(parseHash("#history"), { route: ROUTE.HISTORY, params: {} });
  assert.deepEqual(parseHash("#settings"), { route: ROUTE.SETTINGS, params: {} });
  for (const hash of ["", "#", "#nowhere", "#/history", undefined, null]) {
    assert.deepEqual(parseHash(hash), { route: ROUTE.DOWNLOAD, params: {} }, String(hash));
  }
});

test("hashes ignore case and a trailing slash", () => {
  assert.equal(parseHash("#History").route, ROUTE.HISTORY);
  assert.equal(parseHash("#settings/").route, ROUTE.SETTINGS);
});

test("the playlist picker and Download tab take a url parameter", () => {
  const url = "https://www.youtube.com/playlist?list=PL1&index=2";
  assert.deepEqual(parseHash(`#playlist?url=${encodeURIComponent(url)}`), { route: ROUTE.PLAYLIST, params: { url } });
  assert.deepEqual(parseHash(`#download?url=${encodeURIComponent(url)}`), { route: ROUTE.DOWNLOAD, params: { url } });
  assert.deepEqual(parseHash("#download?url="), { route: ROUTE.DOWNLOAD, params: {} });
  assert.deepEqual(parseHash("#history?url=x"), { route: ROUTE.HISTORY, params: {} }, "other tabs take none");
});

test("the picker without a link is the Download tab", () => {
  assert.deepEqual(parseHash("#playlist"), { route: ROUTE.DOWNLOAD, params: {} });
  assert.deepEqual(parseHash("#playlist?url=%20"), { route: ROUTE.DOWNLOAD, params: {} });
});

test("a malformed escape doesn't throw: the text is kept as it came", () => {
  assert.deepEqual(parseHash("#playlist?url=%E0%A4%A"), { route: ROUTE.PLAYLIST, params: { url: "%E0%A4%A" } });
  assert.deepEqual(parseHash("#download?url=https://x.test/a%ZZb"), { route: ROUTE.DOWNLOAD, params: { url: "https://x.test/a%ZZb" } });
  assert.deepEqual(parseHash("#download?url=%%20"), { route: ROUTE.DOWNLOAD, params: { url: "%%20" } });
});

test("hashes are built back the same way", () => {
  assert.equal(buildHash(ROUTE.HISTORY), "#history");
  assert.equal(buildHash(ROUTE.DOWNLOAD, { url: "" }), "#download");
  const url = "https://www.youtube.com/playlist?list=PL1&x=#y";
  const hash = buildHash(ROUTE.PLAYLIST, { url });
  assert.equal(hash, `#playlist?url=${encodeURIComponent(url)}`);
  assert.deepEqual(parseHash(hash), { route: ROUTE.PLAYLIST, params: { url } });
});

test("the playlist picker lights up the Download tab", () => {
  assert.equal(tabOf(ROUTE.PLAYLIST), ROUTE.DOWNLOAD);
  assert.equal(tabOf(ROUTE.HISTORY), ROUTE.HISTORY);
});

test("a link keeps its own ?, & and + whether it was encoded or pasted as it is", () => {
  const url = "https://www.youtube.com/watch?v=abc&list=PLx&t=1+2";
  assert.deepEqual(parseHash(buildHash(ROUTE.DOWNLOAD, { url })), { route: ROUTE.DOWNLOAD, params: { url } });
  assert.deepEqual(parseHash(`#download?url=${url}`), { route: ROUTE.DOWNLOAD, params: { url } }, "not encoded");
  assert.deepEqual(parseHash(`#playlist?url=${url}`), { route: ROUTE.PLAYLIST, params: { url } });
  const spaced = "https://example.test/a%20b?q=x+y";
  assert.deepEqual(parseHash(buildHash(ROUTE.DOWNLOAD, { url: spaced })).params, { url: spaced }, "%20 survives a round trip");
  assert.deepEqual(parseHash("#download?url=https%3A%2F%2Fx.test%2F%3Fa%3D1%26b%3D2").params, { url: "https://x.test/?a=1&b=2" });
});
