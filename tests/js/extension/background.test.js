import test from "node:test";
import assert from "node:assert/strict";

import { stubChrome } from "./chrome-stub.js";

test("loading background.js registers every listener at once, and checks the snapshot alarm", async () => {
  const chrome = stubChrome();
  globalThis.chrome = chrome;
  try {
    await import("../../../extension/background.js");
  } finally {
    delete globalThis.chrome;
  }
  for (const event of [
    chrome.runtime.onInstalled, chrome.runtime.onStartup, chrome.runtime.onMessage,
    chrome.contextMenus.onClicked, chrome.alarms.onAlarm,
  ]) {
    assert.equal(event.listeners.length, 1);
  }
  // Every start of the worker makes sure the 30-minute alarm exists (browsers can drop alarms).
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.deepEqual(chrome.record.alarmsCreated.map((alarm) => alarm.name), ["snag-youtube-cookies"]);
});
