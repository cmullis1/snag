// The first-run setup screen on a fake DOM: what it shows from setupScreen(), and Retry.
import "./fake-dom.js";
import test from "node:test";
import assert from "node:assert/strict";

import { byClass, byText } from "./fake-dom.js";
import { createSetupScreen } from "../../ui/components/setup-screen.js";
import { setupScreen } from "../../ui/components/setup-model.js";

const settle = () => new Promise((resolve) => setImmediate(resolve));
const status = (state, extra = {}) => ({ updater: { setup: { state, message: null, can_retry: state === "error", tools: {}, ...extra } } });

test("the screen shows the title, the message and one row per tool, updated in place", () => {
  const screen = createSetupScreen({ api: {} });
  screen.update(setupScreen(status("running"), { "yt-dlp": { phase: "downloading", done: 50, total: 100 } }));
  const title = byClass(screen.element, "setup-title")[0];
  assert.equal(title.textContent, "Setting up Snag…");
  const rows = byClass(screen.element, "setup-tool");
  assert.ok(rows.length >= 1);
  assert.equal(byText(screen.element, "Retry", "button").hidden, true, "no Retry while it runs");
  assert.equal(byClass(screen.element, "setup-foot")[0].hidden, true, "nor an empty foot under the tools");
  screen.update(setupScreen(status("running"), { "yt-dlp": { phase: "done", done: 100, total: 100 } }));
  assert.deepEqual(byClass(screen.element, "setup-tool"), rows, "the same rows");
  screen.focus();
  assert.equal(document.activeElement, title);
});

test("after a failure, Retry asks the server again; a refused retry says why", async () => {
  const shown = [];
  let tries = 0;
  const api = { retrySetup: async () => { tries += 1; if (tries > 1) throw new Error("Setup is already running"); return { started: true }; } };
  const screen = createSetupScreen({ api, toast: { show: (text, options) => shown.push([text, options?.kind]) } });
  screen.update(setupScreen(status("error", { message: "Couldn't download ffmpeg" }), {}));
  assert.equal(byClass(screen.element, "setup-title")[0].textContent, "Snag couldn't finish setting up");
  const retry = byText(screen.element, "Retry", "button");
  assert.equal(retry.hidden, false);
  assert.equal(byClass(screen.element, "setup-foot")[0].hidden, false);
  retry.click();
  await settle();
  assert.equal(tries, 1);
  retry.click();
  await settle();
  assert.deepEqual(shown, [["Setup is already running", "error"]]);
  assert.ok(screen.element.textContent.includes("Setup is already running"));
  assert.equal(retry.getAttribute("aria-disabled"), null);
});

test("a refused retry's error stays in view when setup starts running again", async () => {
  let fail;
  const api = { retrySetup: () => new Promise((resolve, reject) => { fail = reject; }) };
  const screen = createSetupScreen({ api });
  screen.update(setupScreen(status("error", { message: "Couldn't download ffmpeg" }), {}));
  byText(screen.element, "Retry", "button").click();
  screen.update(setupScreen(status("running"), {}));
  fail(new Error("Setup is already running"));
  await settle();
  const foot = byClass(screen.element, "setup-foot")[0];
  assert.equal(foot.hidden, false);
  assert.ok(foot.textContent.includes("Setup is already running"));
});
