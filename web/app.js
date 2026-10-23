// The full app tab's entry point: the API, the live store fed by the hub, toasts, and the frame
// (web/shell.js) drawn into this page.

import { createApi } from "../ui/api.js";
import { createHub, wsUrlFor } from "../ui/hub.js";
import { bindHub, createStore } from "../ui/store.js";
import { createToaster } from "../ui/components/toast.js";
import { createShell } from "./shell.js";

const api = createApi("");
const store = createStore();
const hub = createHub(wsUrlFor(""));
const shell = createShell({ api, store, toast: createToaster(), win: window, doc: document });

shell.start();
bindHub(store, hub);
hub.connect();
