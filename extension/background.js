// Snag's background service worker (an ES module): the right-click menu, notifications, the YouTube
// cookie snapshot and the messages from the YouTube button and the popup. The logic lives in
// lib/worker.js; this file only connects it to the browser. Listeners are registered at the top level,
// synchronously, so the events that wake the worker reach them; then, on every start of the worker,
// the 30-minute alarm is checked (an existing one keeps its schedule).

import { createSnagApi } from "./lib/server.js";
import { createWorker, registerListeners } from "./lib/worker.js";

const worker = createWorker({ chrome, api: createSnagApi() });
registerListeners(chrome, worker);
void worker.onWorkerStart();
