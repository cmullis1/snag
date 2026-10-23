import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

import { FakeClock, FakeDocument, FakeLocation, FakeWindow, fakeObserverClass } from "./youtube-dom.js";

const SOURCE = readFileSync(new URL("../../../extension/content/youtube.js", import.meta.url), "utf8");
const WATCH = "https://www.youtube.com/watch?v=abc123";

/** Runs the content script as a classic script in a fresh global (as Chrome does) and returns that
 * global, where it leaves its functions under SnagYouTube. */
function load(globals = {}) {
  const context = vm.createContext({ console, URL, URLSearchParams, Promise, ...globals });
  vm.runInContext(SOURCE, context, { filename: "content/youtube.js" });
  return context;
}

const { SnagYouTube: yt } = load();

/** A watch page laid out as YouTube's is: the watch element holds the video's metadata, which holds
 * the title and the actions block; in that, #menu holds a ytd-menu-renderer whose row holds YouTube's
 * own Like and Share buttons. */
function page({ href = WATCH, row = true, popover = false } = {}) {
  const doc = new FakeDocument({ popover });
  const flexy = doc.createElement("ytd-watch-flexy");
  doc.body.appendChild(flexy);
  doc.answer("ytd-watch-flexy", flexy);
  const metadata = doc.createElement("ytd-watch-metadata");
  const title = doc.createElement("h1");
  title.appendChild(doc.createElement("span"));
  const actions = doc.createElement("div");
  actions.id = "actions";
  metadata.append(title, actions);
  flexy.appendChild(metadata);
  const parts = { doc, flexy, metadata, title, actions, menu: null, renderer: null, row: null, like: null, share: null };
  addMenu(parts);
  if (row) addRow(parts);
  return { ...parts, location: new FakeLocation(href) };
}

/** A new #menu holding a new, empty ytd-menu-renderer, in place of the actions block's old one. */
function addMenu(parts) {
  const menu = parts.doc.createElement("div");
  menu.id = "menu";
  const renderer = parts.doc.createElement("ytd-menu-renderer");
  menu.appendChild(renderer);
  if (parts.menu) parts.menu.remove();
  parts.actions.appendChild(menu);
  Object.assign(parts, { menu, renderer });
  return menu;
}

/** A new, empty ytd-menu-renderer in place of the old one in #menu. */
function replaceRenderer(parts) {
  const renderer = parts.doc.createElement("ytd-menu-renderer");
  parts.renderer.remove();
  parts.menu.appendChild(renderer);
  parts.renderer = renderer;
  return renderer;
}

function addRow(parts) {
  const row = parts.doc.createElement("div");
  row.id = "top-level-buttons-computed";
  const like = parts.doc.createElement("like-button");
  const share = parts.doc.createElement("share-button");
  row.append(like, share);
  parts.renderer.appendChild(row);
  parts.doc.answer(yt.CANDIDATES[0].selector, row);
  Object.assign(parts, { row, like, share });
  return row;
}

/** YouTube puts the next video's title in: a change outside the actions block. */
function changeTitle(parts) {
  parts.title.childNodes[0].remove();
  parts.title.appendChild(parts.doc.createElement("span"));
}

function hosts(doc) {
  return [...doc.documentElement.descendants()].filter((node) => node.id === yt.HOST_ID);
}

function shadowText(node) {
  return [...node.deepDescendants()].map((n) => n.text).join("");
}

function find(node, predicate) {
  return [...node.deepDescendants()].find(predicate);
}

/** Checks what an observer watches; a failure names tags rather than dumping the whole fake page. */
function assertWatching(observer, expected) {
  const target = observer.observing && observer.observing.target;
  assert.ok(target === expected, `watching ${target ? target.tagName : "nothing"}, expected ${expected.tagName}`);
}

/** start() on a page, with a fake clock, observer, console, window and chrome.runtime. */
function run(p, { sendMessage = async () => ({ ok: true, summary: "1080p MP4 + subtitles", title: "A video" }), window = new FakeWindow() } = {}) {
  const clock = new FakeClock();
  const { FakeMutationObserver, instances } = fakeObserverClass();
  const logs = [];
  const sent = [];
  const chrome = {
    runtime: {
      id: "ext",
      sendMessage: (message) => {
        sent.push(message);
        return sendMessage(message);
      },
    },
  };
  p.doc.takeChanges(); // building the page happened before the script ran
  const controller = yt.start({
    document: p.doc,
    location: p.location,
    chrome,
    console: { log: (...args) => logs.push(args.join(" ")) },
    setTimeout: clock.setTimeout,
    clearTimeout: clock.clearTimeout,
    MutationObserver: FakeMutationObserver,
    window,
  });
  p.doc.takeChanges(); // ... and so did placing the button, before the script started observing
  /** Tells the observers about every change to the page since the last call (each hears only about
   * the ones under what it observes), then lets the coalesced check run. */
  const mutate = () => {
    for (const node of p.doc.takeChanges()) for (const observer of instances) observer.trigger(node);
    clock.advance(yt.CHECK_DELAY_MS);
  };
  return { controller, clock, logs, sent, mutate, observers: instances, chrome, window };
}

/** A sendMessage whose answer waits for release(answer). */
function held() {
  let release;
  const sendMessage = () => new Promise((resolve) => { release = resolve; });
  return { sendMessage, release: (answer) => release(answer) };
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

// ----- pure parts -----

test("watch pages are /watch with a video id", () => {
  assert.equal(yt.isWatchPage(new FakeLocation(WATCH)), true);
  assert.equal(yt.isWatchPage(new FakeLocation(`${WATCH}&list=PL1&index=2`)), true);
  for (const href of ["https://www.youtube.com/", "https://www.youtube.com/watch", "https://www.youtube.com/watch?list=PL1",
    "https://www.youtube.com/shorts/abc", "https://www.youtube.com/playlist?list=PL1", "https://www.youtube.com/watchlater?v=x",
    "https://www.youtube.com/live", "https://www.youtube.com/live/", "https://www.youtube.com/@channel/live",
    "https://www.youtube.com/live/abc/extra"]) {
    assert.equal(yt.isWatchPage(new FakeLocation(href)), false, href);
  }
});

test("live stream pages (/live/<id>) are watch pages too", () => {
  for (const href of ["https://www.youtube.com/live/abc_DEF-123", "https://www.youtube.com/live/abc123?si=share", "https://www.youtube.com/live/abc123/"]) {
    assert.equal(yt.isWatchPage(new FakeLocation(href)), true, href);
  }
});

test("the candidate selectors go from the watch page's own row to looser fallbacks", () => {
  const selectors = yt.CANDIDATES.map((candidate) => candidate.selector);
  assert.equal(selectors[0], "ytd-watch-metadata #actions #top-level-buttons-computed");
  assert.ok(selectors.includes("#top-level-buttons-computed"));
  assert.ok(selectors.includes("ytd-menu-renderer #top-level-buttons-computed"));
  assert.ok(selectors.includes("#actions #menu"));
  for (const candidate of yt.CANDIDATES) assert.ok(["append", "before"].includes(candidate.place));
});

test("the actions row is the first candidate in the page", () => {
  const p = page({ row: false });
  p.doc.answer("#actions #menu", p.menu);
  let found = yt.findActionsRow(p.doc);
  assert.equal(found.element, p.menu);
  assert.equal(found.place, "before");

  const row = addRow(p);
  found = yt.findActionsRow(p.doc);
  assert.equal(found.element, row);
  assert.equal(found.place, "append");
});

test("a candidate no longer in the page is skipped, and none found is null", () => {
  const p = page();
  p.row.remove();
  assert.equal(yt.findActionsRow(p.doc), null);
  assert.equal(yt.findActionsRow(new FakeDocument()), null);
});

test("the button goes at the end of a buttons row, or just before the menu", () => {
  const p = page();
  let where = yt.placementOf({ element: p.row, place: "append" });
  assert.equal(where.parent, p.row);
  assert.equal(where.before, null);
  where = yt.placementOf({ element: p.menu, place: "before" });
  assert.equal(where.parent, p.actions);
  assert.equal(where.before, p.menu);
});

test("planning: one button, in the row, only on watch pages", () => {
  const p = page();
  const found = { element: p.row, place: "append" };
  const host = p.doc.createElement("div");
  assert.equal(yt.planButton({ onWatch: true, host: null, found }), "insert");
  assert.equal(yt.planButton({ onWatch: true, host: null, found: null }), "wait");
  p.row.appendChild(host);
  assert.equal(yt.planButton({ onWatch: true, host, found }), "keep");
  assert.equal(yt.planButton({ onWatch: true, host, found: null }), "keep", "a placed button stays while the row can't be seen");
  assert.equal(yt.planButton({ onWatch: false, host, found }), "remove");
  assert.equal(yt.planButton({ onWatch: false, host: null, found: null }), "none");
  // Another copy of the script already has a button here: leave it, unless this copy's is in place.
  const foreign = p.doc.createElement("div");
  assert.equal(yt.planButton({ onWatch: true, host: null, found, foreign }), "defer");
  assert.equal(yt.planButton({ onWatch: true, host, found, foreign }), "keep");
  // YouTube rebuilt the row: the button moves to the new one.
  const newRow = p.doc.createElement("div");
  p.actions.appendChild(newRow);
  assert.equal(yt.planButton({ onWatch: true, host, found: { element: newRow, place: "append" } }), "move");
  // The row (with the button in it) was taken out of the page and nothing replaced it yet.
  p.row.remove();
  assert.equal(yt.planButton({ onWatch: true, host, found: null }), "wait");
});

test("labels: Queued with the summary, the server's message, or how to recover", () => {
  let label = yt.labelFor({ ok: true, summary: "best quality MP4 + subtitles", title: "A video" });
  assert.equal(label.text, "Queued: best quality MP4 + subtitles");
  assert.equal(label.ok, true);
  assert.equal(yt.labelFor({ ok: true, summary: "", title: "A video" }).text, "Queued: A video");
  label = yt.labelFor({ ok: false, message: "Snag isn't running. It starts with Windows, and you can also start it from the Start menu." });
  assert.equal(label.ok, false);
  assert.match(label.text, /^Snag isn't running/);
});

test("labels when the extension didn't answer: reload only when it was reloaded under the page", () => {
  assert.equal(yt.labelFor(undefined, new Error("Extension context invalidated.")).text, yt.RELOAD_MESSAGE);
  assert.match(yt.RELOAD_MESSAGE, /Reload this page/);
  // The worker stopped before answering: the download may well have been added.
  for (const message of [
    "A listener indicated an asynchronous response by returning true, but the message channel closed before a response was received",
    "The message port closed before a response was received.",
  ]) {
    const label = yt.labelFor(undefined, new Error(message));
    assert.equal(label.text, yt.MAYBE_ADDED_MESSAGE, message);
    assert.equal(label.ok, false);
  }
  assert.equal(yt.MAYBE_ADDED_MESSAGE, "Snag may still be adding it. Check the queue in the Snag app.");
  assert.equal(yt.labelFor(undefined, new Error("Could not establish connection. Receiving end does not exist.")).text, yt.NO_ANSWER_MESSAGE);
  assert.equal(yt.labelFor(undefined).text, yt.NO_ANSWER_MESSAGE);
  assert.equal(yt.labelFor(undefined, "a string").text, yt.NO_ANSWER_MESSAGE);
  for (const text of [yt.NO_ANSWER_MESSAGE, yt.MAYBE_ADDED_MESSAGE]) assert.doesNotMatch(text, /Reload/);
});

test("the button is built in its own shadow root, with a label under it", () => {
  const doc = new FakeDocument();
  const parts = yt.buildButton(doc);
  assert.equal(parts.host.id, yt.HOST_ID);
  assert.equal(parts.host.shadow.mode, "closed");
  assert.equal(parts.button.tagName, "BUTTON");
  assert.equal(parts.button.getAttribute("type"), "button");
  assert.equal(shadowText(parts.button), "Download");
  assert.equal(parts.button.getAttribute("title"), "Download with Snag");
  const style = find(parts.host, (n) => n.tagName === "STYLE");
  assert.match(style.text, /--yt-spec-badge-chip-background/);
  assert.match(style.text, /--yt-spec-text-primary/);
  assert.equal(find(parts.host, (n) => n.tagName === "SVG").getAttribute("aria-hidden"), "true");
});

test("screen readers hear the label from an always-rendered, visually hidden status region, not the label itself", () => {
  for (const popover of [false, true]) {
    const parts = yt.buildButton(new FakeDocument({ popover }));
    const { status, label } = parts;
    assert.ok(status.parentNode === parts.host.shadow, "the status region is in the shadow root");
    assert.equal(status.getAttribute("role"), "status");
    assert.equal(status.getAttribute("aria-live"), "polite");
    assert.equal(status.getAttribute("popover"), null, "a hidden popover isn't rendered, so it would announce nothing");
    assert.equal(status.className, "announce");
    // The visible label repeats what the region says, so it's hidden from screen readers.
    assert.equal(label.getAttribute("aria-hidden"), "true");
    assert.equal(label.getAttribute("role"), null);
    assert.equal(label.getAttribute("aria-live"), null);
  }
  const style = find(yt.buildButton(new FakeDocument()).host, (n) => n.tagName === "STYLE").text;
  const rule = style.match(/\.announce \{([^}]*)\}/);
  assert.ok(rule, "the status region has its own rule");
  assert.match(rule[1], /position: absolute;/);
  assert.match(rule[1], /width: 1px;/);
  assert.match(rule[1], /height: 1px;/);
  assert.match(rule[1], /overflow: hidden;/);
  assert.match(rule[1], /clip-path: inset\(50%\);/);
  assert.doesNotMatch(rule[1], /display: none|visibility: hidden/);
  assert.doesNotMatch(style, /\.announce[^{]*:empty/, "the region stays rendered while it's empty");
});

test("the button's styles follow YouTube's own theme and the text direction", () => {
  const style = find(yt.buildButton(new FakeDocument()).host, (n) => n.tagName === "STYLE").text;
  // The fallback colours follow YouTube's dark theme (html[dark]), not the system's.
  assert.match(style, /:host-context\(html\[dark\]\)/);
  assert.doesNotMatch(style, /prefers-color-scheme/);
  // Logical properties, so a right-to-left page mirrors the button and its label.
  assert.match(style, /margin-inline-start:/);
  assert.match(style, /inset-inline-end:/);
  assert.match(style, /border-inline-start:/);
  assert.doesNotMatch(style, /margin-(left|right)|border-(left|right)|(^|[\s;{-])(left|right):/m);
});

test("the label is a manual popover where the browser has the Popover API, else a plain label", () => {
  const withPopover = yt.buildButton(new FakeDocument({ popover: true }));
  assert.equal(withPopover.label.getAttribute("popover"), "manual");
  assert.equal(withPopover.popover, true);
  const without = yt.buildButton(new FakeDocument());
  assert.equal(without.label.getAttribute("popover"), null);
  assert.equal(without.popover, false);
});

test("the script never assigns HTML (YouTube enforces Trusted Types)", () => {
  assert.doesNotMatch(SOURCE, /innerHTML|outerHTML|insertAdjacentHTML|document\.write/);
});

// ----- the script on a page -----

test("loaded without the extension API it only defines its functions", () => {
  const p = page();
  const context = load({ document: p.doc, location: p.location });
  assert.equal(typeof context.SnagYouTube.start, "function");
  assert.equal(hosts(p.doc).length, 0);
});

test("loaded as a content script it adds the button by itself", () => {
  const p = page();
  const clock = new FakeClock();
  const { FakeMutationObserver } = fakeObserverClass();
  load({
    document: p.doc, location: p.location, chrome: { runtime: { id: "ext", sendMessage: async () => ({}) } },
    setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout, MutationObserver: FakeMutationObserver,
  });
  assert.equal(hosts(p.doc).length, 1);
});

test("on a watch page the button goes at the end of the row, and nothing else changes", () => {
  const p = page();
  const { controller } = run(p);
  assert.deepEqual(p.row.childNodes.map((n) => n.tagName), ["LIKE-BUTTON", "SHARE-BUTTON", "DIV"]);
  assert.equal(p.row.childNodes[2], controller.parts.host);
  assert.equal(p.like.attributes.size, 0);
  assert.equal(p.share.attributes.size, 0);
  assert.deepEqual(p.doc.body.childNodes.map((n) => n.tagName), ["YTD-WATCH-FLEXY"]);
});

test("the button waits for a row that appears later", () => {
  const p = page({ row: false });
  const { mutate, logs } = run(p);
  assert.equal(hosts(p.doc).length, 0);
  mutate();
  assert.equal(hosts(p.doc).length, 0);
  addRow(p);
  mutate();
  assert.equal(hosts(p.doc).length, 1);
  assert.equal(p.row.childNodes.at(-1).id, yt.HOST_ID);
  assert.deepEqual(logs, []);
});

test("checks are coalesced: a burst of changes is one look at the page", () => {
  const p = page({ row: false });
  const { observers, clock } = run(p);
  const candidates = new Set(yt.CANDIDATES.map((candidate) => candidate.selector));
  const looks = () => p.doc.queries.filter((selector) => candidates.has(selector)).length;
  const before = looks();
  for (let i = 0; i < 50; i += 1) observers[0].trigger(p.renderer);
  clock.advance(yt.CHECK_DELAY_MS);
  assert.equal(looks() - before, yt.CANDIDATES.length);
});

test("page changes are watched on the watch page's element until the row turns up, then on the video's metadata the row is in", () => {
  const p = page({ row: false });
  const { observers, mutate } = run(p);
  assert.equal(observers.length, 1);
  assertWatching(observers[0], p.flexy);
  assert.deepEqual({ ...observers[0].observing.options }, { childList: true, subtree: true });
  addRow(p);
  mutate();
  assertWatching(observers[0], p.metadata);
  // Leaving the watch page: back to the watch page's element (YouTube keeps it, hidden).
  p.location.href = "https://www.youtube.com/";
  p.doc.dispatch("yt-navigate-finish");
  assertWatching(observers[0], p.flexy);
});

test("without YouTube's watch element yet, the whole page is watched", () => {
  const p = page({ row: false });
  p.doc.selectors.delete("ytd-watch-flexy");
  const { observers, mutate } = run(p);
  assertWatching(observers[0], p.doc.documentElement);
  p.doc.answer("ytd-watch-flexy", p.flexy);
  changeTitle(p);
  mutate();
  assertWatching(observers[0], p.flexy);
});

test("a button in a row outside the video's metadata has the watch element watched, else the whole page", () => {
  const p = page({ row: false });
  const row = p.doc.createElement("div");
  p.flexy.appendChild(row);
  p.doc.answer(yt.CANDIDATES[0].selector, row);
  const { observers, mutate } = run(p);
  assert.equal(row.childNodes.length, 1);
  assertWatching(observers[0], p.flexy);
  const loose = p.doc.createElement("div");
  p.doc.body.appendChild(loose);
  row.remove();
  p.doc.answer(yt.CANDIDATES[0].selector, loose);
  mutate();
  assert.equal(loose.childNodes.length, 1, "the button moved to the row outside the watch element");
  assertWatching(observers[0], p.doc.documentElement);
});

test("a replaced ytd-menu-renderer gets the button back", () => {
  const p = page();
  const { controller, mutate } = run(p);
  replaceRenderer(p);
  addRow(p);
  mutate();
  assert.ok(p.row.childNodes.at(-1) === controller.parts.host, "the button is at the end of the new row");
  assert.equal(hosts(p.doc).length, 1);
  // And again, when YouTube rebuilds the whole #menu around a new one.
  addMenu(p);
  addRow(p);
  mutate();
  assert.ok(p.row.childNodes.at(-1) === controller.parts.host, "the button is at the end of the new row");
  assert.equal(hosts(p.doc).length, 1);
});

test("an address change seen through a mutation outside the row is noticed", async () => {
  const p = page({ popover: true });
  const { controller, mutate } = run(p);
  controller.parts.button.dispatch("click");
  await settle();
  assert.equal(controller.parts.label.popoverOpen, true);
  p.location.href = "https://www.youtube.com/watch?v=next";
  changeTitle(p);
  mutate();
  assert.equal(controller.parts.label.text, "", "the first video's label is cleared");
  assert.equal(controller.parts.label.popoverOpen, false);
});

test("YouTube's yt-page-data-updated event runs a check", () => {
  const p = page();
  run(p);
  p.location.href = "https://www.youtube.com/";
  p.doc.dispatch("yt-page-data-updated");
  assert.equal(hosts(p.doc).length, 0);
  p.location.href = WATCH;
  p.doc.dispatch("yt-page-data-updated");
  assert.equal(hosts(p.doc).length, 1);
});

test("a Download button from another copy of the script is left alone, and no second one is added", () => {
  const p = page();
  const other = p.doc.createElement("div");
  other.id = yt.HOST_ID;
  p.row.appendChild(other);
  const { clock, logs, mutate } = run(p);
  mutate();
  assert.deepEqual(hosts(p.doc), [other]);
  clock.advance(yt.GIVE_UP_MS);
  assert.deepEqual(logs, [], "a button is there, so nothing to report");
  // The other copy went away (its extension was reloaded): this one takes over.
  other.remove();
  mutate();
  assert.equal(hosts(p.doc).length, 1);
  assert.notEqual(hosts(p.doc)[0], other);
});

test("a copy of the script whose extension was reloaded or removed takes its button away and stops", () => {
  const p = page();
  const { chrome, observers, mutate, clock } = run(p);
  assert.equal(hosts(p.doc).length, 1);
  delete chrome.runtime.id; // what Chrome does to a content script cut off from its extension
  changeTitle(p);
  mutate();
  assert.equal(hosts(p.doc).length, 0);
  assert.equal(observers[0].observing, null);
  assert.equal(clock.timers.size, 0);
});

test("in-page navigation never makes a second button", () => {
  const p = page();
  const { mutate } = run(p);
  for (let i = 0; i < 3; i += 1) {
    p.location.href = `https://www.youtube.com/watch?v=next${i}`;
    p.doc.dispatch("yt-navigate-finish");
    mutate();
  }
  assert.equal(hosts(p.doc).length, 1);
});

test("a rebuilt row gets the same button moved into it", () => {
  const p = page();
  const { controller, mutate } = run(p);
  const oldRow = p.row;
  oldRow.remove();
  addRow(p);
  mutate();
  assert.equal(hosts(p.doc).length, 1);
  assert.equal(p.row.childNodes.at(-1), controller.parts.host);
  assert.equal(oldRow.childNodes.length, 2);
});

test("a button YouTube removed from the row is put back", () => {
  const p = page();
  const { controller, mutate } = run(p);
  controller.parts.host.remove();
  mutate();
  assert.equal(p.row.childNodes.at(-1), controller.parts.host);
});

test("leaving the watch page takes the button away; coming back adds it again", () => {
  const p = page();
  const { mutate } = run(p);
  p.location.href = "https://www.youtube.com/";
  p.doc.dispatch("yt-navigate-finish");
  assert.equal(hosts(p.doc).length, 0);
  p.location.href = WATCH;
  p.doc.dispatch("yt-navigate-finish");
  mutate();
  assert.equal(hosts(p.doc).length, 1);
});

test("navigation is noticed from page changes too, without yt-navigate-finish", () => {
  const p = page();
  const { mutate } = run(p);
  p.location.href = "https://www.youtube.com/feed/subscriptions";
  p.share.remove();
  mutate();
  assert.equal(hosts(p.doc).length, 0);
});

test("no row within 10 seconds: nothing is added, one console line, and the search stops", () => {
  const p = page({ row: false });
  const { clock, logs, mutate } = run(p);
  clock.advance(yt.GIVE_UP_MS - 1);
  assert.deepEqual(logs, []);
  clock.advance(1);
  assert.equal(logs.length, 1);
  assert.match(logs[0], /Snag/);
  const queries = p.doc.queries.length;
  addRow(p);
  mutate();
  mutate();
  clock.advance(60_000);
  assert.equal(hosts(p.doc).length, 0);
  assert.equal(p.doc.queries.length, queries, "after giving up it stops looking");
  assert.equal(logs.length, 1);
});

test("after giving up, the next video looks again", () => {
  const p = page({ row: false });
  const { clock, mutate } = run(p);
  clock.advance(yt.GIVE_UP_MS);
  addRow(p);
  p.location.href = "https://www.youtube.com/watch?v=other";
  p.doc.dispatch("yt-navigate-finish");
  mutate();
  assert.equal(hosts(p.doc).length, 1);
});

test("a button in place by the deadline means no console line", () => {
  const p = page();
  const { clock, logs } = run(p);
  clock.advance(yt.GIVE_UP_MS * 2);
  assert.deepEqual(logs, []);
});

test("stop() disconnects everything", () => {
  const p = page({ row: false });
  const { controller, observers, clock, logs } = run(p);
  controller.stop();
  assert.equal(observers[0].observing, null);
  assert.equal(clock.timers.size, 0);
  for (const type of ["yt-navigate-finish", "yt-page-data-updated"]) assert.equal((p.doc.listeners.get(type) ?? []).length, 0, type);
  p.doc.dispatch("yt-navigate-finish");
  clock.advance(yt.GIVE_UP_MS);
  assert.deepEqual(logs, []);
});

// ----- clicking -----

test("clicking queues this page and shows what was queued under the button for a few seconds", async () => {
  const p = page();
  const { controller, sent, clock } = run(p);
  const { button, label } = controller.parts;
  assert.equal(shadowText(button), "Download");
  button.dispatch("click");
  assert.equal(shadowText(button), "Adding to Snag…");
  assert.equal(button.getAttribute("aria-disabled"), "true");
  assert.equal(label.text, "");
  await settle();
  assert.equal(shadowText(button), "Download");
  assert.equal(sent.length, 1);
  assert.equal(sent[0].type, "quick");
  assert.equal(sent[0].url, WATCH);
  assert.equal(label.text, "Queued: 1080p MP4 + subtitles");
  assert.equal(label.getAttribute("data-state"), "ok");
  assert.equal(button.getAttribute("aria-disabled"), null);
  clock.advance(yt.LABEL_MS);
  assert.equal(label.text, "");
});

test("what the label says is announced through the status region, which empties with it", async () => {
  for (const popover of [false, true]) {
    const p = page({ popover });
    const answer = held();
    const { controller, clock } = run(p, { sendMessage: answer.sendMessage });
    const { button, status } = controller.parts;
    assert.equal(status.text, "");
    button.dispatch("click");
    answer.release({ ok: true, summary: "1080p MP4 + subtitles", title: "A video" });
    await settle();
    assert.equal(status.text, "Queued: 1080p MP4 + subtitles", `popover: ${popover}`);
    clock.advance(yt.LABEL_MS);
    assert.equal(status.text, "");
    // A second click empties the region at once, so the same answer is announced again.
    button.dispatch("click");
    assert.equal(status.text, "");
    answer.release({ ok: false, message: "This video is private." });
    await settle();
    assert.equal(status.text, "This video is private.");
    // Moving to another video empties it too.
    p.location.href = "https://www.youtube.com/watch?v=next";
    p.doc.dispatch("yt-navigate-finish");
    assert.equal(status.text, "");
  }
});

test("with the Popover API the label shows in the top layer, just under the button's end edge", async () => {
  const p = page({ popover: true });
  const { controller, clock } = run(p);
  const { button, label } = controller.parts;
  button.rect = { top: 100, bottom: 136, left: 900, right: 1010, width: 110, height: 36 };
  assert.equal(label.popoverOpen, false);
  button.dispatch("click");
  assert.equal(label.popoverOpen, false, "nothing shows under the button while it says Adding to Snag");
  await settle();
  assert.equal(label.popoverOpen, true);
  assert.equal(label.text, "Queued: 1080p MP4 + subtitles");
  assert.equal(label.style.top, "142px");
  assert.equal(label.style.insetInlineEnd, `${1280 - 1010}px`);
  clock.advance(yt.LABEL_MS);
  assert.equal(label.popoverOpen, false);
  assert.equal(label.text, "");
});

test("on a right-to-left page the label lines up with the button's left edge", async () => {
  const p = page({ popover: true });
  const { controller } = run(p, { window: new FakeWindow({ direction: "rtl" }) });
  controller.parts.button.rect = { top: 100, bottom: 136, left: 200, right: 310 };
  controller.parts.button.dispatch("click");
  await settle();
  assert.equal(controller.parts.label.style.insetInlineEnd, "200px");
});

test("the label follows the button when the page scrolls or resizes, and stops listening once hidden", async () => {
  const p = page({ popover: true });
  const window = new FakeWindow();
  const { controller, clock } = run(p, { window });
  const { button, label } = controller.parts;
  button.rect = { top: 100, bottom: 136, left: 900, right: 1010 };
  button.dispatch("click");
  await settle();
  button.rect = { top: 40, bottom: 76, left: 900, right: 1010 };
  window.dispatch("scroll");
  assert.equal(label.style.top, "82px");
  button.rect = { top: 40, bottom: 76, left: 700, right: 810 };
  window.dispatch("resize");
  assert.equal(label.style.insetInlineEnd, `${1280 - 810}px`);
  clock.advance(yt.LABEL_MS);
  assert.equal(window.count("scroll") + window.count("resize"), 0);
});

test("a label that can't be shown as a popover still gets its text, and the next one shows", async () => {
  const p = page({ popover: true });
  const { controller, clock } = run(p);
  const { button, label } = controller.parts;
  controller.parts.host.remove(); // out of the page: showPopover refuses
  button.dispatch("click");
  await settle();
  assert.equal(label.text, "Queued: 1080p MP4 + subtitles");
  assert.equal(label.popoverOpen, false);
  clock.advance(yt.LABEL_MS);
  p.row.appendChild(controller.parts.host);
  button.dispatch("click");
  await settle();
  assert.equal(label.popoverOpen, true);
});

test("a popover the browser closed by itself (its button left the page) is shown again next time", async () => {
  const p = page({ popover: true });
  const { controller } = run(p);
  const { button, label } = controller.parts;
  button.dispatch("click");
  await settle();
  label.popoverOpen = false; // removing a showing popover from the page hides it
  button.dispatch("click");
  await settle();
  assert.equal(label.popoverOpen, true);
});

test("moving to another video clears the label", async () => {
  const p = page({ popover: true });
  const { controller, mutate } = run(p);
  controller.parts.button.dispatch("click");
  await settle();
  assert.equal(controller.parts.label.popoverOpen, true);
  p.location.href = "https://www.youtube.com/watch?v=next";
  p.doc.dispatch("yt-navigate-finish");
  mutate();
  assert.equal(controller.parts.label.text, "");
  assert.equal(controller.parts.label.popoverOpen, false);
});

test("an answer for a click on another video is ignored, and the button is ready on the new one", async () => {
  const p = page();
  const answer = held();
  const { controller, sent, mutate } = run(p, { sendMessage: answer.sendMessage });
  const { button, caption, label } = controller.parts;
  button.dispatch("click");
  p.location.href = "https://www.youtube.com/watch?v=next";
  p.doc.dispatch("yt-navigate-finish");
  mutate();
  assert.equal(caption.textContent, "Download");
  assert.equal(button.getAttribute("aria-disabled"), null);
  answer.release({ ok: true, summary: "MP3", title: "x" });
  await settle();
  assert.equal(label.text, "", "the first video's answer isn't shown on the second");
  button.dispatch("click");
  assert.equal(sent.length, 2);
  assert.equal(sent[1].url, "https://www.youtube.com/watch?v=next");
});

test("an answer that arrives after the address changed, before the script noticed, is ignored", async () => {
  const p = page();
  const answer = held();
  const { controller } = run(p, { sendMessage: answer.sendMessage });
  controller.parts.button.dispatch("click");
  p.location.href = "https://www.youtube.com/watch?v=next";
  answer.release({ ok: true, summary: "MP3", title: "x" });
  await settle();
  assert.equal(controller.parts.label.text, "");
  assert.equal(controller.parts.button.getAttribute("aria-disabled"), null);
});

test("a second click while one is being added sends nothing more", async () => {
  const p = page();
  let release;
  const { controller, sent } = run(p, { sendMessage: () => new Promise((resolve) => { release = resolve; }) });
  controller.parts.button.dispatch("click");
  controller.parts.button.dispatch("click");
  release({ ok: true, summary: "MP3", title: "x" });
  await settle();
  assert.equal(sent.length, 1);
});

test("the server's error shows under the button, for longer", async () => {
  const p = page();
  const message = "Snag isn't running. It starts with Windows, and you can also start it from the Start menu.";
  const { controller, clock } = run(p, { sendMessage: async () => ({ ok: false, message }) });
  controller.parts.button.dispatch("click");
  await settle();
  assert.equal(controller.parts.label.text, message);
  assert.equal(controller.parts.label.getAttribute("data-state"), "error");
  clock.advance(yt.LABEL_MS);
  assert.equal(controller.parts.label.text, message);
  clock.advance(yt.ERROR_LABEL_MS - yt.LABEL_MS);
  assert.equal(controller.parts.label.text, "");
});

test("an extension that was reloaded under the page says to reload it", async () => {
  const p = page();
  const { controller } = run(p, { sendMessage: () => { throw new Error("Extension context invalidated."); } });
  controller.parts.button.dispatch("click");
  await settle();
  assert.equal(controller.parts.label.text, yt.RELOAD_MESSAGE);
  assert.equal(shadowText(controller.parts.button), "Download");
});

test("a worker that stopped before answering says the download may still be added", async () => {
  const p = page();
  const closed = "A listener indicated an asynchronous response by returning true, but the message channel closed before a response was received";
  const { controller } = run(p, { sendMessage: async () => { throw new Error(closed); } });
  controller.parts.button.dispatch("click");
  await settle();
  assert.equal(controller.parts.label.text, yt.MAYBE_ADDED_MESSAGE);
  assert.equal(controller.parts.label.getAttribute("data-state"), "error");
});
