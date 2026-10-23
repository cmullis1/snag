// The queue banner on a fake DOM: its action buttons.
import "./fake-dom.js";
import test from "node:test";
import assert from "node:assert/strict";

import { byText } from "./fake-dom.js";
import { createBanner } from "../../ui/components/banner.js";

test("a failed setup's banner retries setup", async () => {
  let retried = 0;
  const banner = createBanner({ onResume: async () => {}, onRetrySetup: async () => { retried += 1; } });
  banner.update([{ kind: "setup", text: "Snag couldn't finish setting up.", action: "retry_setup" }]);
  const button = byText(banner.element, "Retry setup", "button");
  assert.ok(button);
  button.click();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(retried, 1);
  assert.equal(button.getAttribute("aria-disabled"), null);
});

test("an action without a handler shows no button", () => {
  const banner = createBanner({});
  banner.update([{ kind: "setup", text: "Snag couldn't finish setting up.", action: "retry_setup" },
    { kind: "paused", text: "Downloads are paused", action: "resume" }]);
  assert.equal(byText(banner.element, "Retry setup", "button"), null);
  assert.equal(byText(banner.element, "Resume", "button"), null);
});

test("Resume still resumes", async () => {
  let resumed = 0;
  const banner = createBanner({ onResume: async () => { resumed += 1; } });
  banner.update([{ kind: "paused", text: "Downloads are paused", action: "resume" }]);
  byText(banner.element, "Resume", "button").click();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(resumed, 1);
});

test("an action that fails says why in a toast", async () => {
  const shown = [];
  const toast = { show: (message, options) => shown.push([message, options?.kind]) };
  const banner = createBanner({ onResume: async () => { throw new Error("Snag isn't running"); }, toast });
  banner.update([{ kind: "paused", text: "Downloads are paused", action: "resume" }]);
  const button = byText(banner.element, "Resume", "button");
  button.click();
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(shown, [["Snag isn't running", "error"]]);
  assert.equal(button.getAttribute("aria-disabled"), null);
  const silent = createBanner({ onResume: async () => { throw new Error(""); }, toast });
  silent.update([{ kind: "paused", text: "Downloads are paused", action: "resume" }]);
  byText(silent.element, "Resume", "button").click();
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(shown.at(-1), ["Couldn't resume", "error"]);
});
