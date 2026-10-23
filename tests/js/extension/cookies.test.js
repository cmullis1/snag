import test from "node:test";
import assert from "node:assert/strict";

import {
  MAX_COOKIES,
  SIGN_IN,
  YOUTUBE_URL,
  askWithCookiesIfNeeded,
  collectCookies,
  collectYouTubeCookies,
  cookieUrl,
  isWebUrl,
  serializeCookies,
} from "../../../extension/lib/cookies.js";

/** A chrome.cookies.Cookie as Chrome returns it, fields Snag doesn't use included. */
function chromeCookie(fields = {}) {
  return {
    domain: ".youtube.com",
    expirationDate: 1790000000.5,
    hostOnly: false,
    httpOnly: true,
    name: "SID",
    path: "/",
    sameSite: "no_restriction",
    secure: true,
    session: false,
    storeId: "0",
    value: "secret",
    ...fields,
  };
}

/** chrome.cookies stand-in: getAll records its filter and answers with `answer(filter)`. */
function stubCookies(answer = () => []) {
  const calls = [];
  return {
    calls,
    getAll: async (filter) => {
      calls.push(filter);
      return answer(filter);
    },
  };
}

test("web URLs are http and https addresses with a host", () => {
  assert.equal(isWebUrl("https://www.youtube.com/watch?v=x"), true);
  assert.equal(isWebUrl("http://example.com/a"), true);
  for (const url of ["chrome://extensions", "file:///C:/a.mp4", "about:blank", "", "not a url", "https://", null, undefined, 5]) {
    assert.equal(isWebUrl(url), false, String(url));
  }
});

test("cookies are read for the URL itself, or YouTube's for its short and embed hosts", () => {
  assert.equal(cookieUrl("https://vimeo.com/123"), "https://vimeo.com/123");
  assert.equal(cookieUrl("https://music.youtube.com/watch?v=x"), "https://music.youtube.com/watch?v=x");
  // Snag files these hosts' cookies under www.youtube.com (snag/cookies.py), so send that site's.
  assert.equal(cookieUrl("https://youtu.be/abc"), YOUTUBE_URL);
  assert.equal(cookieUrl("https://www.youtube-nocookie.com/embed/abc"), YOUTUBE_URL);
  assert.equal(cookieUrl("https://youtube-nocookie.com/embed/abc"), YOUTUBE_URL);
  assert.equal(cookieUrl("chrome://newtab"), null);
});

test("serialising keeps just the fields the server reads, in the chrome.cookies shape", () => {
  const [cookie] = serializeCookies([chromeCookie()]);
  assert.deepEqual(cookie, {
    domain: ".youtube.com",
    name: "SID",
    value: "secret",
    path: "/",
    secure: true,
    httpOnly: true,
    hostOnly: false,
    session: false,
    expirationDate: 1790000000.5,
  });
});

test("partitioned cookies (set for a site embedded in another) are left out", () => {
  const plain = chromeCookie({ name: "plain" });
  const cookies = serializeCookies([
    chromeCookie({ name: "embedded", partitionKey: { topLevelSite: "https://a.example" } }),
    chromeCookie({ name: "embedded-empty", partitionKey: {} }),
    plain,
    chromeCookie({ name: "null-key", partitionKey: null }),
  ]);
  assert.deepEqual(cookies.map((cookie) => cookie.name), ["plain", "null-key"]);
});

test("partitioned cookies don't count towards MAX_COOKIES", () => {
  const partitioned = Array.from({ length: MAX_COOKIES }, (_, i) => chromeCookie({ name: `p${i}`, partitionKey: { topLevelSite: "https://a.example" } }));
  const cookies = serializeCookies([...partitioned, chromeCookie({ name: "kept" })]);
  assert.deepEqual(cookies.map((cookie) => cookie.name), ["kept"]);
});

test("a session cookie has no expiry, and missing optional fields stay missing", () => {
  const [cookie] = serializeCookies([{ domain: "example.com", name: "a", value: "", session: true }]);
  assert.deepEqual(cookie, { domain: "example.com", name: "a", value: "", session: true });
});

test("cookies the server would refuse are left out instead of failing the whole request", () => {
  const good = chromeCookie();
  const cookies = serializeCookies([
    good,
    chromeCookie({ value: "has\ttab" }),
    chromeCookie({ name: "line\nbreak" }),
    chromeCookie({ path: "/\r" }),
    chromeCookie({ domain: "." }),
    chromeCookie({ domain: "" }),
    chromeCookie({ name: 5 }),
    null,
    "SID=1",
  ]);
  assert.deepEqual(cookies, serializeCookies([good]));
});

test("bad flag and expiry values are dropped, the cookie kept", () => {
  const [cookie] = serializeCookies([chromeCookie({ secure: "yes", expirationDate: Number.NaN })]);
  assert.equal("secure" in cookie, false);
  assert.equal("expirationDate" in cookie, false);
  assert.equal(cookie.name, "SID");
});

test("at most MAX_COOKIES are sent", () => {
  const many = Array.from({ length: MAX_COOKIES + 5 }, (_, i) => chromeCookie({ name: `c${i}` }));
  const cookies = serializeCookies(many);
  assert.equal(cookies.length, MAX_COOKIES);
  assert.equal(cookies[0].name, "c0");
});

test("anything that isn't a list serialises to no cookies", () => {
  assert.deepEqual(serializeCookies(undefined), []);
  assert.deepEqual(serializeCookies({}), []);
});

test("collectCookies asks chrome.cookies for the URL's cookies", async () => {
  const api = stubCookies(() => [chromeCookie()]);
  const cookies = await collectCookies("https://www.youtube.com/watch?v=x", api);
  assert.deepEqual(api.calls, [{ url: "https://www.youtube.com/watch?v=x" }]);
  assert.equal(cookies.length, 1);
  assert.equal(cookies[0].name, "SID");
});

test("collectCookies reads YouTube's cookies for a youtu.be link", async () => {
  const api = stubCookies(() => []);
  await collectCookies("https://youtu.be/x", api);
  assert.deepEqual(api.calls, [{ url: YOUTUBE_URL }]);
});

test("collectCookies has none for pages that aren't on the web, without asking", async () => {
  const api = stubCookies(() => [chromeCookie()]);
  assert.deepEqual(await collectCookies("chrome://extensions", api), []);
  assert.deepEqual(api.calls, []);
});

test("collectCookies has none when chrome.cookies fails or is missing", async () => {
  const failing = { getAll: async () => { throw new Error("No host permissions for cookies"); } };
  assert.deepEqual(await collectCookies("https://example.com/", failing), []);
  assert.deepEqual(await collectCookies("https://example.com/", undefined), []);
});

test("the YouTube snapshot is every youtube.com cookie", async () => {
  const api = stubCookies(() => [chromeCookie(), chromeCookie({ domain: "music.youtube.com", hostOnly: true })]);
  const cookies = await collectYouTubeCookies(api);
  assert.deepEqual(api.calls, [{ domain: "youtube.com" }]);
  assert.equal(cookies.length, 2);
});

test("a failed YouTube snapshot rejects, so an empty list never replaces the server's copy", async () => {
  const failing = { getAll: async () => { throw new Error("browser shutting down"); } };
  await assert.rejects(collectYouTubeCookies(failing), /browser shutting down/);
});

// ----- cookies only when the site needs them -----

const URL_ = "https://example.com/members/1";
const signInError = () => Object.assign(new Error("Sign in to example.com in Chrome or Edge, then retry"), { kind: SIGN_IN });

/** ask stand-in: answers[i] for the i-th call (an Error is thrown), recording the cookies passed. */
function stubAsk(...answers) {
  const calls = [];
  const ask = async (cookies) => {
    calls.push(cookies);
    const answer = answers[calls.length - 1];
    if (answer instanceof Error) throw answer;
    return answer;
  };
  return { ask, calls };
}

test("a request that works without cookies sends none and reads none", async () => {
  const cookies = stubCookies(() => [chromeCookie()]);
  const { ask, calls } = stubAsk("info");
  assert.deepEqual(await askWithCookiesIfNeeded(URL_, ask, cookies), { answer: "info", cookies: null });
  assert.deepEqual(calls, [null]);
  assert.deepEqual(cookies.calls, []);
});

test("a sign-in error asks once more with the site's cookies, and says which were sent", async () => {
  const cookies = stubCookies(() => [chromeCookie({ domain: "example.com" })]);
  const { ask, calls } = stubAsk(signInError(), "info");
  const outcome = await askWithCookiesIfNeeded(URL_, ask, cookies);
  assert.equal(outcome.answer, "info");
  assert.equal(outcome.cookies.length, 1);
  assert.equal(outcome.cookies[0].domain, "example.com");
  assert.deepEqual(calls, [null, outcome.cookies]);
  assert.deepEqual(cookies.calls, [{ url: URL_ }]);
});

test("a sign-in error with no cookies for the site stands, without asking again", async () => {
  const first = signInError();
  const { ask, calls } = stubAsk(first);
  await assert.rejects(askWithCookiesIfNeeded(URL_, ask, stubCookies(() => [])), (error) => error === first);
  assert.equal(calls.length, 1);
});

test("other errors are not retried and read no cookies", async () => {
  const cookies = stubCookies(() => [chromeCookie()]);
  for (const kind of ["unsupported", "unavailable", "offline", "bad_request"]) {
    const { ask, calls } = stubAsk(Object.assign(new Error(kind), { kind }));
    await assert.rejects(askWithCookiesIfNeeded(URL_, ask, cookies), new RegExp(kind));
    assert.equal(calls.length, 1, kind);
  }
  assert.deepEqual(cookies.calls, []);
});

test("the retry's own failure is the one reported", async () => {
  const second = Object.assign(new Error("Video unavailable"), { kind: "unavailable" });
  const { ask } = stubAsk(signInError(), second);
  await assert.rejects(askWithCookiesIfNeeded(URL_, ask, stubCookies(() => [chromeCookie()])), (error) => error === second);
});

test("a request no longer wanted isn't retried with cookies", async () => {
  const cookies = stubCookies(() => [chromeCookie()]);
  const { ask, calls } = stubAsk(signInError(), "info");
  await assert.rejects(askWithCookiesIfNeeded(URL_, ask, cookies, { stillWanted: () => false }), { kind: SIGN_IN });
  assert.equal(calls.length, 1);
  assert.deepEqual(cookies.calls, []);
});
