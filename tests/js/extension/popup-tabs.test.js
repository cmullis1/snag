// The popup's tab handling (extension/popup/tabs.js): the active tab's address, and "Open app" /
// "Choose videos" opening the full tab or bringing an open one forward.
import test from "node:test";
import assert from "node:assert/strict";

import { activeTabUrl, openApp } from "../../../extension/popup/tabs.js";
import { APP_TAB_PATTERNS, appLink } from "../../../extension/popup/model.js";

/** chrome.tabs / chrome.windows as the popup uses them; open: the tabs a {url} query finds. */
function stubTabs({ active = [{ id: 1, windowId: 1, url: "https://www.youtube.com/watch?v=abc" }], open = [], failQuery = false, failUpdate = false } = {}) {
  const calls = [];
  const chrome = {
    calls,
    tabs: {
      query: async (filter) => {
        calls.push(["query", filter]);
        if (failQuery) throw new Error("query failed");
        return filter.active ? active : open;
      },
      update: async (id, props) => {
        calls.push(["update", id, props]);
        if (failUpdate) throw new Error("No tab with id");
        return { id, ...props };
      },
      create: async (props) => {
        calls.push(["create", props]);
        return { id: 99, ...props };
      },
    },
    windows: {
      update: async (id, props) => {
        calls.push(["windows.update", id, props]);
        return { id };
      },
    },
  };
  return chrome;
}

test("the active tab's address comes from tabs.query in the current window", async () => {
  const chrome = stubTabs();
  assert.equal(await activeTabUrl(chrome), "https://www.youtube.com/watch?v=abc");
  assert.deepEqual(chrome.calls, [["query", { active: true, currentWindow: true }]]);
});

test("a tab whose address the popup can't see (chrome://, no host permission) is no address", async () => {
  assert.equal(await activeTabUrl(stubTabs({ active: [{ id: 1, windowId: 1 }] })), null);
  assert.equal(await activeTabUrl(stubTabs({ active: [] })), null);
  assert.equal(await activeTabUrl(stubTabs({ failQuery: true })), null);
});

test("Open app opens the full tab when none is open", async () => {
  const chrome = stubTabs();
  await openApp(chrome);
  assert.deepEqual(chrome.calls, [["query", { url: [...APP_TAB_PATTERNS] }], ["create", { url: appLink() }]]);
});

test("Open app brings an open full tab forward without changing its page", async () => {
  const chrome = stubTabs({ open: [{ id: 7, windowId: 3, url: "http://127.0.0.1:47823/#history" }] });
  await openApp(chrome);
  assert.deepEqual(chrome.calls.slice(1), [["update", 7, { active: true }], ["windows.update", 3, { focused: true }]]);
});

test("Choose videos takes an open full tab to the playlist picker", async () => {
  const link = appLink("playlist", "https://www.youtube.com/playlist?list=PL1");
  const chrome = stubTabs({ open: [{ id: 7, windowId: 3, url: "http://127.0.0.1:47823/" }] });
  await openApp(chrome, link);
  assert.deepEqual(chrome.calls.slice(1), [["update", 7, { active: true, url: link }], ["windows.update", 3, { focused: true }]]);
});

test("with several full tabs open, the one used last comes forward", async () => {
  const chrome = stubTabs({
    open: [
      { id: 4, windowId: 1, url: "http://127.0.0.1:47823/", lastAccessed: 100 },
      { id: 5, windowId: 2, url: "http://localhost:47823/", lastAccessed: 300 },
      { id: 6, windowId: 1, url: "http://127.0.0.1:47823/#settings", lastAccessed: 200 },
    ],
  });
  await openApp(chrome);
  assert.deepEqual(chrome.calls[1], ["update", 5, { active: true }]);
});

test("when the open tab can't be found or used, a new one opens", async () => {
  const failing = stubTabs({ failQuery: true });
  await openApp(failing);
  assert.deepEqual(failing.calls.at(-1), ["create", { url: appLink() }]);
  const closed = stubTabs({ open: [{ id: 7, windowId: 3, url: "http://127.0.0.1:47823/" }], failUpdate: true });
  await openApp(closed, appLink("history"));
  assert.deepEqual(closed.calls.at(-1), ["create", { url: appLink("history") }]);
});
