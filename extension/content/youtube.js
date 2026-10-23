// Snag's Download button on YouTube watch pages (/watch?v=… and live streams' /live/<id>; spec 4.10).
// A classic content script (content scripts can't be modules): everything is defined on one object,
// SnagYouTube, which the tests load in Node with a fake page, and the script starts itself only when
// it runs as a content script.
//
// The button sits in the actions row next to Like and Share, styled like YouTube's own pill buttons
// with YouTube's theme variables (fallback colours follow YouTube's own dark theme, html[dark]), with
// logical properties so right-to-left pages mirror it, inside its own shadow root so neither side's
// styles leak. The label under it is a top-layer popover placed from the button's position, so
// YouTube's overflow can't clip it (a plain label where the Popover API is missing); it is cleared
// when the page moves to another video, and an answer for a click made on another video is ignored.
// Screen readers hear the label's text from a visually hidden status region that is always rendered
// (a hidden popover or an empty, undisplayed label would announce nothing); the visible label is
// hidden from them so it isn't read twice.
// YouTube navigates without reloading, so the script re-checks on yt-navigate-finish and
// yt-page-data-updated and, as a fallback, on page changes (coalesced) under YouTube's watch element,
// then, once the button is in place, under the video's metadata block its row is in (so a rebuilt
// menu or the next video's title is seen too); it keeps exactly one button, moving it if YouTube
// rebuilds the row, and leaves alone a button another copy of the script already added. A copy
// whose extension was reloaded or removed takes its button away and stops. If no row turns up within
// 10 seconds of a navigation it adds nothing, logs one console line and stops looking until the next
// video. It touches nothing on the page but its own button. No HTML strings: YouTube enforces
// Trusted Types.
(function (root) {
  "use strict";

  const HOST_ID = "snag-download";
  const GIVE_UP_MS = 10000;
  const CHECK_DELAY_MS = 200;
  const LABEL_MS = 6000;
  const ERROR_LABEL_MS = 12000;
  const LABEL_GAP_PX = 6;
  const WATCH_ROOT = "ytd-watch-flexy";
  const METADATA_ROOT = "ytd-watch-metadata";
  const LIVE_PATH = /^\/live\/[A-Za-z0-9_-]+\/?$/;
  const OBSERVE = Object.freeze({ childList: true, subtree: true });
  const SVG_NS = "http://www.w3.org/2000/svg";
  // The extension was reloaded or updated under the page: this copy of the script can't reach it.
  const RELOAD_MESSAGE = "Snag's extension was reloaded. Reload this page and try again.";
  // The background worker stopped before answering (the browser ended it mid-request): the request may
  // well have reached Snag.
  const MAYBE_ADDED_MESSAGE = "Snag may still be adding it. Check the queue in the Snag app.";
  const NO_ANSWER_MESSAGE = "Snag's extension didn't answer. Try again.";
  const BUTTON_TEXT = "Download";
  const PENDING_TEXT = "Adding to Snag…";
  const FALLBACK_MESSAGE = "Snag couldn't add that download.";
  const GIVE_UP_LOG = "Snag: couldn't find YouTube's like/share row within 10 seconds, so the Download button wasn't added to this page.";

  // Where the button may go, most specific first. "append": at the end of a row of buttons; "before":
  // just before YouTube's "..." menu, in the menu's own row.
  const CANDIDATES = Object.freeze([
    { selector: "ytd-watch-metadata #actions #top-level-buttons-computed", place: "append" },
    { selector: "ytd-watch-metadata #top-level-buttons-computed", place: "append" },
    { selector: "ytd-watch-flexy #top-level-buttons-computed", place: "append" },
    { selector: "ytd-menu-renderer #top-level-buttons-computed", place: "append" },
    { selector: "#top-level-buttons-computed", place: "append" },
    { selector: "ytd-watch-metadata #actions #menu", place: "before" },
    { selector: "#actions #menu", place: "before" },
  ].map(Object.freeze));

  const STYLE = `
:host {
  display: inline-flex;
  position: relative;
  flex: none;
  margin-inline-start: 8px;
}
button {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 36px;
  padding-block: 0;
  padding-inline: 12px 16px;
  border: 0;
  border-radius: 18px;
  background: var(--yt-spec-badge-chip-background, rgba(0, 0, 0, 0.05));
  color: var(--yt-spec-text-primary, #0f0f0f);
  font-family: "Roboto", "Arial", sans-serif;
  font-size: 14px;
  font-weight: 500;
  line-height: 36px;
  white-space: nowrap;
  cursor: pointer;
}
button:hover {
  background: var(--yt-spec-button-chip-background-hover, rgba(0, 0, 0, 0.1));
}
button:focus-visible {
  outline: 2px solid var(--yt-spec-call-to-action, #065fd4);
  outline-offset: 2px;
}
button[aria-disabled="true"] {
  cursor: progress;
  opacity: 0.7;
}
svg {
  width: 24px;
  height: 24px;
  fill: currentColor;
}
.label {
  box-sizing: border-box;
  width: max-content;
  max-width: min(320px, calc(100vw - 16px));
  padding: 8px 12px;
  border: 0;
  border-radius: 8px;
  background: var(--yt-spec-base-background, #fff);
  color: var(--yt-spec-text-primary, #0f0f0f);
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.25);
  font-family: "Roboto", "Arial", sans-serif;
  font-size: 13px;
  line-height: 18px;
  white-space: normal;
}
.label[popover] {
  position: fixed;
  inset: auto;
  margin: 0;
  overflow: visible;
}
.label:not([popover]) {
  position: absolute;
  top: calc(100% + 6px);
  inset-inline-end: 0;
  z-index: 2100;
}
.label:not([popover]):empty {
  display: none;
}
.label[data-state="error"] {
  border-inline-start: 3px solid #cc0000;
}
.announce {
  position: absolute;
  width: 1px;
  height: 1px;
  margin: -1px;
  padding: 0;
  border: 0;
  overflow: hidden;
  clip-path: inset(50%);
  white-space: nowrap;
}
:host-context(html[dark]) button {
  background: var(--yt-spec-badge-chip-background, rgba(255, 255, 255, 0.1));
  color: var(--yt-spec-text-primary, #f1f1f1);
}
:host-context(html[dark]) button:hover {
  background: var(--yt-spec-button-chip-background-hover, rgba(255, 255, 255, 0.2));
}
:host-context(html[dark]) .label {
  background: var(--yt-spec-base-background, #212121);
  color: var(--yt-spec-text-primary, #f1f1f1);
}`;

  // A download arrow over a tray line, Snag's mark, at YouTube's 24-pixel icon size.
  const ICON_PATH = "M11 4h2v8.17l3.09-3.08L17.5 10.5 12 16l-5.5-5.5 1.41-1.41L11 12.17V4zM5 18h14v2H5z";

  /** True on youtube.com/watch?v=… pages and live streams' /live/<id> pages. */
  function isWatchPage(location) {
    if (LIVE_PATH.test(location.pathname)) return true;
    return location.pathname === "/watch" && new URLSearchParams(location.search).has("v");
  }

  /** The first candidate in the page: {element, place}, or null. */
  function findActionsRow(document, candidates = CANDIDATES) {
    for (const candidate of candidates) {
      const element = document.querySelector(candidate.selector);
      if (element && element.isConnected !== false) return { element, place: candidate.place };
    }
    return null;
  }

  /** Where the button goes for a found row: parent.insertBefore(host, before). */
  function placementOf(found) {
    return found.place === "before"
      ? { parent: found.element.parentNode, before: found.element }
      : { parent: found.element, before: null };
  }

  /** What to do with the button: "insert" a new one, "move" it into a rebuilt row, "keep" it,
   * "remove" it (not a watch page), "wait" for the row, "defer" to another copy's button, or "none".
   * host: the button if it was ever placed; foreign: a Download button another copy of the script
   * added (its extension reloaded under the page, say). */
  function planButton({ onWatch, host, found, foreign }) {
    if (!onWatch) return host && host.parentNode ? "remove" : "none";
    if (foreign && !(host && host.isConnected)) return "defer";
    if (!found) return host && host.isConnected ? "keep" : "wait";
    if (!host) return "insert";
    return host.parentNode === placementOf(found).parent ? "keep" : "move";
  }

  /** The text under the button after a click: {text, ok}. response: the background worker's answer to
   * {type: "quick"}; error: why asking it failed. */
  function labelFor(response, error) {
    if (error) {
      const message = String((error && error.message) || error);
      if (/Extension context invalidated/i.test(message)) return { text: RELOAD_MESSAGE, ok: false };
      if (/message (channel|port) closed/i.test(message)) return { text: MAYBE_ADDED_MESSAGE, ok: false };
      return { text: NO_ANSWER_MESSAGE, ok: false };
    }
    if (!response || typeof response !== "object") return { text: NO_ANSWER_MESSAGE, ok: false };
    if (response.ok) return { text: `Queued: ${response.summary || response.title || "your download"}`, ok: true };
    return { text: typeof response.message === "string" && response.message ? response.message : FALLBACK_MESSAGE, ok: false };
  }

  /** The button's elements: host (goes in the page), and inside its closed shadow root the button (its
   * caption: the text in it), the label under it, a manual popover when the browser has the Popover
   * API (popover: true), and status, the visually hidden live region screen readers hear the label's
   * text from. */
  function buildButton(document) {
    const host = document.createElement("div");
    host.id = HOST_ID;
    const shadow = host.attachShadow({ mode: "closed" });

    const style = document.createElement("style");
    style.textContent = STYLE;

    const button = document.createElement("button");
    button.setAttribute("type", "button");
    button.setAttribute("title", "Download with Snag");
    const icon = document.createElementNS(SVG_NS, "svg");
    icon.setAttribute("viewBox", "0 0 24 24");
    icon.setAttribute("aria-hidden", "true");
    icon.setAttribute("focusable", "false");
    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", ICON_PATH);
    icon.appendChild(path);
    const caption = document.createElement("span");
    caption.textContent = BUTTON_TEXT;
    button.appendChild(icon);
    button.appendChild(caption);

    const label = document.createElement("div");
    label.className = "label";
    label.setAttribute("aria-hidden", "true");
    const popover = typeof label.showPopover === "function" && typeof label.hidePopover === "function";
    if (popover) label.setAttribute("popover", "manual");

    const status = document.createElement("div");
    status.className = "announce";
    status.setAttribute("role", "status");
    status.setAttribute("aria-live", "polite");

    shadow.appendChild(style);
    shadow.appendChild(button);
    shadow.appendChild(label);
    shadow.appendChild(status);
    return { host, button, caption, label, status, popover };
  }

  /** Runs the button on a page. env: {document, location, chrome, console, setTimeout, clearTimeout,
   * MutationObserver, window (optional: scroll/resize and the text direction)}. Returns
   * {parts, check(), stop()}. */
  function start(env) {
    const document = env.document;
    const location = env.location;
    const runtime = env.chrome && env.chrome.runtime;
    const log = env.console;
    const later = env.setTimeout;
    const cancel = env.clearTimeout;
    const win = env.window || null;

    const controller = { parts: null, check, stop };
    let lastHref = location.href;
    let gaveUp = false;
    let stopped = false;
    let giveUpTimer = null;
    let checkTimer = null;
    let labelTimer = null;
    let pending = null; // the click being answered: {href}
    let observed = null;
    let following = false; // the popover label is following the button on scroll and resize

    // ----- the label -----

    function isRtl(host) {
      try {
        if (win && typeof win.getComputedStyle === "function") return win.getComputedStyle(host).direction === "rtl";
      } catch {
        // Fall through to the page's own direction.
      }
      const html = document.documentElement;
      return Boolean(html && html.getAttribute && html.getAttribute("dir") === "rtl");
    }

    /** Puts the popover label just under the button, lined up with the button's end edge. */
    function placeLabel() {
      const { host, button, label } = controller.parts;
      const rect = button.getBoundingClientRect();
      const width = (document.documentElement && document.documentElement.clientWidth) || 0;
      const end = isRtl(host) ? rect.left : width - rect.right;
      label.style.top = `${Math.round(rect.bottom + LABEL_GAP_PX)}px`;
      label.style.insetInlineEnd = `${Math.max(0, Math.round(end))}px`;
    }

    function follow(on) {
      if (!win || on === following) return;
      following = on;
      const method = on ? "addEventListener" : "removeEventListener";
      win[method]("scroll", placeLabel, { capture: true, passive: true });
      win[method]("resize", placeLabel, { passive: true });
    }

    function popoverOpen(label) {
      try {
        return label.matches(":popover-open");
      } catch {
        return false;
      }
    }

    function hideLabel() {
      if (!controller.parts) return;
      const { label, status, popover } = controller.parts;
      if (labelTimer !== null) cancel(labelTimer);
      labelTimer = null;
      label.textContent = "";
      status.textContent = "";
      if (popover) {
        if (popoverOpen(label)) {
          try {
            label.hidePopover();
          } catch {
            // Already hidden by the browser.
          }
        }
        follow(false);
      }
    }

    function showLabel(text, state, ms) {
      const { label, status, popover } = controller.parts;
      hideLabel();
      label.textContent = text;
      label.setAttribute("data-state", state);
      status.textContent = text;
      if (popover) {
        placeLabel();
        try {
          label.showPopover();
          follow(true);
        } catch {
          // The button isn't in the page right now: the text is there for when it is.
        }
      }
      labelTimer = later(() => {
        labelTimer = null;
        hideLabel();
      }, ms);
    }

    // ----- clicking -----

    function setReady(ready) {
      const { button, caption } = controller.parts;
      if (ready) button.removeAttribute("aria-disabled");
      else button.setAttribute("aria-disabled", "true");
      caption.textContent = ready ? BUTTON_TEXT : PENDING_TEXT;
    }

    async function onClick() {
      if (pending) return;
      const request = { href: location.href };
      pending = request;
      hideLabel();
      setReady(false);
      let response;
      let error = null;
      try {
        response = await runtime.sendMessage({ type: "quick", url: request.href });
      } catch (caught) {
        error = caught || new Error("no answer");
      }
      if (pending !== request) return; // the page moved to another video meanwhile
      pending = null;
      setReady(true);
      if (location.href !== request.href) return; // ... and the script hasn't noticed yet
      const { text, ok } = labelFor(response, error);
      showLabel(text, ok ? "ok" : "error", ok ? LABEL_MS : ERROR_LABEL_MS);
    }

    /** Forgets the click being answered (its answer will be ignored) and clears the label. */
    function forgetClick() {
      if (pending) {
        pending = null;
        setReady(true);
      }
      hideLabel();
    }

    function parts() {
      if (!controller.parts) {
        controller.parts = buildButton(document);
        controller.parts.button.addEventListener("click", onClick);
      }
      return controller.parts;
    }

    function clearGiveUp() {
      if (giveUpTimer !== null) cancel(giveUpTimer);
      giveUpTimer = null;
    }

    function giveUp() {
      giveUpTimer = null;
      const host = controller.parts && controller.parts.host;
      if (host && host.isConnected) return;
      gaveUp = true;
      log.log(GIVE_UP_LOG);
    }

    function navigated() {
      lastHref = location.href;
      gaveUp = false;
      clearGiveUp();
      forgetClick();
      if (isWatchPage(location)) giveUpTimer = later(giveUp, GIVE_UP_MS);
    }

    // ----- watching the page -----

    /** Where page changes are watched. Once the button is in place: the video's metadata block its row
     * is in (a rebuilt row or menu, a removed button and the next video's title all show there), else
     * the watch element the row is in, else the whole page. Before that: YouTube's watch element, else
     * the whole page. */
    function observeTarget() {
      const host = controller.parts && controller.parts.host;
      const row = host && host.isConnected ? host.parentNode : null;
      if (row) {
        const near = typeof row.closest === "function" ? row.closest(METADATA_ROOT) || row.closest(WATCH_ROOT) : null;
        return near || document.documentElement || document;
      }
      const watch = document.querySelector(WATCH_ROOT);
      if (watch && watch.isConnected !== false) return watch;
      return document.documentElement || document;
    }

    function retarget() {
      const target = observeTarget();
      if (target === observed) return;
      observer.disconnect();
      observer.observe(target, OBSERVE);
      observed = target;
    }

    /** A Download button in the page that isn't this copy's, or null. */
    function foreignButton() {
      const existing = document.getElementById(HOST_ID);
      const ours = controller.parts && controller.parts.host;
      return existing && existing !== ours ? existing : null;
    }

    /** The extension this copy belongs to was reloaded or removed (Chrome clears chrome.runtime.id):
     * take the button away and stop, leaving the page to a fresh copy. */
    function retire() {
      stop();
      if (controller.parts) controller.parts.host.remove();
    }

    function check() {
      if (stopped) return;
      if (runtime && !runtime.id) {
        retire();
        return;
      }
      if (location.href !== lastHref) navigated();
      const onWatch = isWatchPage(location);
      if (onWatch && gaveUp) return;
      const placed = controller.parts && controller.parts.host.parentNode ? controller.parts.host : null;
      const foreign = onWatch ? foreignButton() : null;
      const found = onWatch && !foreign ? findActionsRow(document) : null;
      const plan = planButton({ onWatch, host: placed, found, foreign });
      if (plan === "remove") {
        placed.remove();
      } else if (plan === "insert" || plan === "move") {
        const { parent, before } = placementOf(found);
        parent.insertBefore(parts().host, before);
        clearGiveUp();
      } else if (plan === "keep" || plan === "defer") {
        clearGiveUp();
      }
      retarget();
    }

    function scheduleCheck() {
      if (checkTimer !== null) return;
      checkTimer = later(() => {
        checkTimer = null;
        check();
      }, CHECK_DELAY_MS);
    }

    function onNavigateFinish() {
      navigated();
      check();
    }

    function onPageDataUpdated() {
      check();
    }

    const observer = new env.MutationObserver(scheduleCheck);
    document.addEventListener("yt-navigate-finish", onNavigateFinish);
    document.addEventListener("yt-page-data-updated", onPageDataUpdated);

    function stop() {
      stopped = true;
      observer.disconnect();
      observed = null;
      document.removeEventListener("yt-navigate-finish", onNavigateFinish);
      document.removeEventListener("yt-page-data-updated", onPageDataUpdated);
      forgetClick();
      for (const timer of [giveUpTimer, checkTimer, labelTimer]) if (timer !== null) cancel(timer);
      giveUpTimer = checkTimer = labelTimer = null;
    }

    navigated();
    check();
    return controller;
  }

  root.SnagYouTube = Object.freeze({
    HOST_ID, GIVE_UP_MS, CHECK_DELAY_MS, LABEL_MS, ERROR_LABEL_MS, RELOAD_MESSAGE, MAYBE_ADDED_MESSAGE,
    NO_ANSWER_MESSAGE, CANDIDATES,
    isWatchPage, findActionsRow, placementOf, planButton, labelFor, buildButton, start,
  });

  if (typeof chrome !== "undefined" && chrome && chrome.runtime && chrome.runtime.id && typeof document !== "undefined") {
    start({
      document,
      location,
      chrome,
      console,
      setTimeout: (callback, ms) => setTimeout(callback, ms),
      clearTimeout: (id) => clearTimeout(id),
      MutationObserver,
      window: typeof window === "undefined" ? null : window,
    });
  }
})(globalThis);
