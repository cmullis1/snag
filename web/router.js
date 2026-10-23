// The full tab's hash routes: #download (optionally ?url=… to look a link up), #history, #settings,
// and #playlist?url=… (the playlist picker, shown under the Download tab). Pure: no DOM.

export const ROUTE = Object.freeze({
  DOWNLOAD: "download",
  HISTORY: "history",
  SETTINGS: "settings",
  PLAYLIST: "playlist",
});

const ROUTES = new Set(Object.values(ROUTE));
const TAKES_URL = new Set([ROUTE.DOWNLOAD, ROUTE.PLAYLIST]);

// The link is everything after "url=", decoded once: a link pasted into the address bar as it is
// ("#download?url=https://www.youtube.com/watch?v=abc&list=PLx") keeps its own "?", "&" and "+", and one
// built by buildHash (encoded) comes back exactly. Text that doesn't decode (a stray "%") is kept as it
// came, so the Download tab can say the link didn't come through. null when there is none.
const URL_PARAM = /(?:^|&)url=/;

function urlParam(query) {
  const match = URL_PARAM.exec(query);
  if (!match) return null;
  const raw = query.slice(match.index + match[0].length);
  let value;
  try {
    value = decodeURIComponent(raw);
  } catch {
    value = raw;
  }
  return value.trim() || null;
}

/** {route, params: {url?}} for a location.hash; unknown or incomplete routes are the Download tab. */
export function parseHash(hash) {
  const text = typeof hash === "string" ? hash.replace(/^#/, "") : "";
  const mark = text.indexOf("?");
  const name = (mark < 0 ? text : text.slice(0, mark)).replace(/\/+$/, "").toLowerCase();
  const query = mark < 0 ? "" : text.slice(mark + 1);
  if (!ROUTES.has(name)) return { route: ROUTE.DOWNLOAD, params: {} };
  const url = TAKES_URL.has(name) ? urlParam(query) : null;
  if (name === ROUTE.PLAYLIST && !url) return { route: ROUTE.DOWNLOAD, params: {} };
  return { route: name, params: url ? { url } : {} };
}

/** The hash for a route: "#history", "#playlist?url=…" (the link encoded). The supported way to build a
 * link into the full tab (the extension opens these); parseHash reads it back exactly. */
export function buildHash(route, { url = null } = {}) {
  return url ? `#${route}?url=${encodeURIComponent(url)}` : `#${route}`;
}

/** The header tab a route belongs to. */
export function tabOf(route) {
  return route === ROUTE.PLAYLIST ? ROUTE.DOWNLOAD : route;
}
