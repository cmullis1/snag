// The popup page's entry point (popup.html loads it as a module; the MV3 page policy allows no inline
// script): the API bound to Snag's address, the live store fed by Snag's hub, toasts, and the popup
// (app.js) drawn into the page. The hub is closed when the popup goes away.

import { SNAG_BASE, createSnagApi } from "../lib/server.js";
import { createHub, wsUrlFor } from "../ui/hub.js";
import { bindHub, createStore } from "../ui/store.js";
import { createToaster } from "../ui/components/toast.js";
import { createPopup } from "./app.js";

const store = createStore();
const hub = createHub(wsUrlFor(SNAG_BASE));
const toast = createToaster();
const popup = createPopup({
  chrome: globalThis.chrome, api: createSnagApi(), store, toast, closeWindow: () => globalThis.close(),
});

document.body.append(popup.element, toast.element);
bindHub(store, hub);
hub.connect();
globalThis.addEventListener("pagehide", () => {
  hub.close();
  popup.destroy();
});
popup.start();
