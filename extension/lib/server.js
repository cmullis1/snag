// Snag's local server as the extension reaches it: the shared API client (ui/api.js, synced into
// extension/ui/) bound to the fixed address. Requests from the extension carry
// Origin: chrome-extension://<EXTENSION_ID>, which the server's guard allows (snag/server/guard.py).

import { createApi } from "../ui/api.js";

export const SNAG_BASE = "http://127.0.0.1:47823";
export const SNAG_APP_URL = `${SNAG_BASE}/`;

/** The API client for Snag's server; options.fetch is injectable for tests. */
export function createSnagApi(options) {
  return createApi(SNAG_BASE, options);
}
