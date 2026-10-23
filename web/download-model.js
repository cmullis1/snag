// Pure rules behind the Download tab: what to look up from the paste box (or whether a link handed to it is
// one), and a playlist's summary.

const WITH_SCHEME = /[a-z][a-z0-9+.-]*:\/\/[^\s"'<>]+/i;
const BARE_HOST = /^(?:[a-z0-9-]+\.)+[a-z]{2,}(?:[/?#][^\s"'<>]*)?$/i;
const NOT_A_LINK = "That isn't a link. Paste the address of a video or playlist.";
/** Said when a link handed to the tab (its route's ?url=…) holds no link, e.g. a malformed one. */
export const LINK_LOST = "That link didn't come through. Paste it again.";
/** Said when a link handed to the tab from outside the page waits for Look up (see isYouTubeLink). */
export const PRESS_LOOK_UP = "Press Look up to look up this link.";

/** {url} to look up from the box's text: the first link in it (a bare "youtube.com/…" gets https://);
 * {url: null, error: null} when it's empty; {url: null, error} when it holds no link. */
export function lookupTarget(text) {
  const value = typeof text === "string" ? text.trim() : "";
  if (!value) return { url: null, error: null };
  const linked = WITH_SCHEME.exec(value);
  if (linked) return { url: linked[0] };
  const bare = value.split(/\s+/).find((word) => BARE_HOST.test(word));
  if (bare) return { url: `https://${bare}` };
  return { url: null, error: NOT_A_LINK };
}

/** Whether text holds a link lookupTarget would look up. */
export function isLink(text) {
  return lookupTarget(text).url !== null;
}

// A link into the full tab (#download?url=…, #playlist?url=…) can come from anywhere: the extension,
// but also any web page or program that opens Snag's address. Snag looks it up by itself only when it
// is YouTube's (the extension's links are, nearly always); any other link waits in the paste box for
// the user to press Look up, so nothing outside can make Snag fetch an address the user didn't choose.
const YOUTUBE_DOMAINS = ["youtube.com", "youtu.be", "youtube-nocookie.com"];

/** True for an http(s) link on youtube.com (www., m., music. …), youtu.be or youtube-nocookie.com. */
export function isYouTubeLink(url) {
  let address;
  try {
    address = new URL(url);
  } catch {
    return false;
  }
  if (address.protocol !== "https:" && address.protocol !== "http:") return false;
  const host = address.hostname.toLowerCase();
  return YOUTUBE_DOMAINS.some((domain) => host === domain || host.endsWith(`.${domain}`));
}

const YOUTUBE_HOSTS = ["youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"];

// What the server's lookup makes of a link (snag/info/lookup.py classify_url): for a YouTube address, a
// video or a playlist page and the playlist the address names; any other address is left to yt-dlp.
function linkKind(url) {
  let address;
  try {
    address = new URL(url);
  } catch {
    return "auto";
  }
  if (!YOUTUBE_HOSTS.includes(address.hostname.toLowerCase())) return "auto";
  const list = address.searchParams.getAll("list").find((id) => id !== "") ?? null;
  return `${address.pathname === "/playlist" && list ? "playlist" : "video"} ${list}`;
}

/** Whether the server's lookup reads both addresses alike. A lookup kept under one address may be kept
 * under the other too only then: watch?v=x&list=PL1 answers "x, from playlist PL1", which x's own
 * address must not get (the server's cache follows the same rule, in LookupService._store). */
export function readAlike(a, b) {
  return linkKind(a) === linkKind(b);
}

const plural = (count, one, many = `${one}s`) => `${count} ${count === 1 ? one : many}`;

/** {title, text: "18 videos · 3 downloaded before"} for the Download tab's playlist card. */
export function playlistSummary(info) {
  const entries = Array.isArray(info?.entries) ? info.entries : [];
  const before = entries.filter((entry) => entry?.duplicate).length;
  const parts = [entries.length ? plural(entries.length, "video") : "No videos"];
  if (before) parts.push(`${before} downloaded before`);
  return { title: info?.title || "Playlist", text: parts.join(" · ") };
}
