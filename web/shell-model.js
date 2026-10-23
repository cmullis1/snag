// Pure rules for the full tab's frame: the note shown when the page can't reach Snag, and the page title.

const NOT_RUNNING = "Snag isn't running. It starts with Windows, and you can also start it from the Start menu.";
const RECONNECTING = "Lost touch with Snag. Reconnecting…";

/** The note above the tabs about the live connection, or null. connection: the hub's state;
 * everConnected: it has been connected since the page opened; failedOnce: a connection attempt failed. */
export function connectionNote({ connection, everConnected, failedOnce }) {
  if (connection === "connected" || !failedOnce) return null;
  return everConnected ? RECONNECTING : NOT_RUNNING;
}

const TITLES = { history: "History", settings: "Settings", playlist: "Choose videos", setup: "Setting up" };

/** The document title for a route ("download", "history", "settings", "playlist", or "setup"). */
export function pageTitle(route) {
  return Object.hasOwn(TITLES, route) ? `${TITLES[route]} · Snag` : "Snag";
}
