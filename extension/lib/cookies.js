// The user's site cookies for Snag (spec 3.5): read with chrome.cookies and sent to the server in the
// shape chrome.cookies returns them (POST /api/lookup, /api/jobs/quick, /api/cookies), and only when a
// request needs them: askWithCookiesIfNeeded asks without cookies first and again with the site's
// only when Snag answers that sign-in is required (error kind sign_in). The server
// checks the fields it writes to a cookies.txt line (snag/server/routes/lookup.py parse_cookies) and
// refuses the whole request if one cookie is malformed, so cookies it would refuse are left out here.
// Partitioned cookies (CHIPS: set by a site while embedded in another, with a partitionKey) are left
// out too: they belong to that embedding, not to the site's own pages, and the cookies.txt Snag
// writes has no way to say so.

export const MAX_COOKIES = 2000; // the server's limit per request (MAX_COOKIES in lookup.py)
export const YOUTUBE_URL = "https://www.youtube.com/";
export const YOUTUBE_DOMAIN = "youtube.com";
export const SIGN_IN = "sign_in"; // the error kind Snag answers when a page needs the user's login (snag/errors.py)

const TEXT_FIELDS = ["domain", "name", "value", "path"];
const FLAG_FIELDS = ["secure", "httpOnly", "hostOnly", "session"];
const LINE_BREAKING = /[\t\r\n]/;
// Hosts whose cookies Snag files under www.youtube.com (snag/cookies.py _ALIASES): read YouTube's.
const YOUTUBE_ALIASES = new Set(["youtu.be", "youtube-nocookie.com", "www.youtube-nocookie.com"]);

function parsed(url) {
  if (typeof url !== "string") return null;
  try {
    return new URL(url);
  } catch {
    return null;
  }
}

/** True for an http:// or https:// address with a host: the only pages Snag downloads from. */
export function isWebUrl(url) {
  const address = parsed(url);
  return address !== null && (address.protocol === "http:" || address.protocol === "https:") && address.hostname !== "";
}

/** The address whose cookies go with a request for url, or null when it isn't a web page. */
export function cookieUrl(url) {
  if (!isWebUrl(url)) return null;
  return YOUTUBE_ALIASES.has(parsed(url).hostname.toLowerCase()) ? YOUTUBE_URL : url;
}

function serializeCookie(cookie) {
  if (cookie === null || typeof cookie !== "object") return null;
  if (cookie.partitionKey !== undefined && cookie.partitionKey !== null) return null;
  const out = {};
  for (const field of TEXT_FIELDS) {
    const text = cookie[field];
    if (text === undefined && field === "path") continue;
    if (typeof text !== "string" || LINE_BREAKING.test(text)) return null;
    out[field] = text;
  }
  if (!out.domain.replace(/^\.+|\.+$/g, "")) return null;
  for (const field of FLAG_FIELDS) {
    if (typeof cookie[field] === "boolean") out[field] = cookie[field];
  }
  if (typeof cookie.expirationDate === "number" && Number.isFinite(cookie.expirationDate)) {
    out.expirationDate = cookie.expirationDate;
  }
  return out;
}

/** chrome.cookies.Cookie objects → the list the server takes: only the fields it reads, cookies it
 * would refuse left out, at most MAX_COOKIES. */
export function serializeCookies(cookies) {
  if (!Array.isArray(cookies)) return [];
  const out = [];
  for (const cookie of cookies) {
    const serialized = serializeCookie(cookie);
    if (serialized) out.push(serialized);
    if (out.length === MAX_COOKIES) break;
  }
  return out;
}

/** The cookies the browser would send to url's site; none for pages that aren't on the web or when
 * chrome.cookies fails (a download without cookies still works for most videos). */
export async function collectCookies(url, cookiesApi = globalThis.chrome?.cookies) {
  const source = cookieUrl(url);
  if (source === null || !cookiesApi) return [];
  try {
    return serializeCookies(await cookiesApi.getAll({ url: source }));
  } catch {
    return [];
  }
}

/** Every youtube.com cookie, for the snapshot the server keeps (spec 3.5). Rejects when chrome.cookies
 * fails, so a failure is never sent as "no cookies" (which would clear the server's copy). */
export async function collectYouTubeCookies(cookiesApi = globalThis.chrome?.cookies) {
  return serializeCookies(await cookiesApi.getAll({ domain: YOUTUBE_DOMAIN }));
}

/** Asks Snag without cookies, and only when it answers that sign-in is required (an error of kind
 * sign_in) asks once more with the cookies the browser has for url's site: ask(cookies | null) makes
 * the request (null: send none). → {answer, cookies: the cookies the answer took, or null}. Any other
 * failure, a sign-in error when the site has no cookies, or one for a request no longer wanted
 * (stillWanted() false: nothing is read or sent then) is thrown as it came; so is the retry's own. */
export async function askWithCookiesIfNeeded(url, ask, cookiesApi = globalThis.chrome?.cookies, { stillWanted = () => true } = {}) {
  try {
    return { answer: await ask(null), cookies: null };
  } catch (error) {
    if (error?.kind !== SIGN_IN || !stillWanted()) throw error;
    const cookies = await collectCookies(url, cookiesApi);
    if (!cookies.length || !stillWanted()) throw error;
    return { answer: await ask(cookies), cookies };
  }
}
