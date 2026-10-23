// The toolbar popup (spec 4.10), made testable: popup.js gives it the real browser and server, tests
// give it stand-ins.
//
//   const popup = createPopup({chrome, api, store, toast, closeWindow});
//   document.body.append(popup.element);  await popup.start();
//
// On start it reads the active tab's address, asks Snag for its status and settings, and looks the page
// up: without cookies, and once more with the site's cookies only when Snag answers that sign-in is
// required (spec 3.5; those cookies then go with the download too). Snag's own tab is not looked up:
// the link it shows is, if any. Then it shows one view (model.js selectView): the compact download
// screen for a video (with "Choose videos" when it is part of a playlist, whose name is looked up in
// the background), a playlist's name, count and "Choose videos", "Nothing to download on this page"
// with a paste box, "Snag isn't running…", or a setting-up notice. A notice above the view tells of a
// yt-dlp update or a missing tool. The slim queue line at the bottom follows the hub (store) live.
//
// chrome: tabs, windows and cookies (no "tabs" permission: see tabs.js); api: ui/api.js bound to Snag;
// store: ui/store.js fed by the hub; toast: createToaster() (optional); closeWindow: closes the popup
// after it opened the full tab.

import { h, replace, setText } from "../ui/components/el.js";
import { uniqueId } from "../ui/components/ids.js";
import { createDownloadPanel } from "../ui/components/download-panel.js";
import { progressBar, setProgress } from "../ui/components/job-row.js";
import { EVENT, OFFLINE, CONNECTION } from "../ui/fields.js";
import { askWithCookiesIfNeeded } from "../lib/cookies.js";
import { VIEW, appLink, noticeFor, pageTarget, pastedTarget, queueLine, selectView } from "./model.js";
import { activeTabUrl, openApp } from "./tabs.js";

export const OPEN_FAILED_TEXT = "Snag couldn't open its tab. Try again.";

export function createPopup({ chrome, api, store, toast = null, closeWindow = () => {} }) {
  let reading = true; // the tab's address isn't known yet
  let snagPage = false; // the tab is Snag's own page (showing no link)
  let target = null; // the address looked up: the tab's, or one pasted
  let pasted = false; // target came from the paste box
  let lookup = null; // null while under way, {url, info, cookies} or {error}
  let lookupSerial = 0; // which lookup is current; an answer for an older one is dropped
  let offline = false;
  let status = null;
  let shownKey = null;
  let shownView = null;
  let panel = null;

  // --- the frame ---

  const openButton = h("button", { type: "button", class: "link-button", onClick: () => go(appLink()) }, "Open app");
  const header = h("header", { class: "popup-header spread" }, h("h1", { class: "app-name" }, "Snag"), openButton);
  const noticeText = h("span", { class: "grow" });
  const noticeButton = h("button", { type: "button", class: "link-button", onClick: () => go(appLink("settings")) });
  const notice = h("p", { class: "note popup-notice spread", hidden: true }, noticeText, noticeButton);
  const content = h("div", { class: "popup-view" });
  const queueText = h("span", { class: "popup-queue-text small" });
  const queueProgress = progressBar("All downloads");
  const queue = h("footer", { class: "popup-queue", hidden: true }, queueText, queueProgress);
  const element = h("main", { class: "popup" }, header, notice, content, queue);

  /** Open the full tab (or a link into it), then close the popup; a toast if the browser refuses. */
  async function go(link) {
    try {
      await openApp(chrome, link);
    } catch {
      toast?.show(OPEN_FAILED_TEXT, { kind: "error" });
      return;
    }
    closeWindow();
  }

  // --- what Snag says ---

  async function loadStatus() {
    try {
      status = await api.getStatus();
    } catch (error) {
      if (error?.kind === OFFLINE) offline = true;
    }
    render();
  }

  async function loadSettings() {
    try {
      const settings = await api.getSettings();
      if (!store.getState().settings) store.dispatch({ type: EVENT.SETTINGS, settings });
    } catch {
      // Offline (shown by the view) or refused: the panel's folder line stays empty until the hub sends them.
    }
  }

  async function lookUp(url) {
    lookupSerial += 1;
    const mine = lookupSerial;
    const current = () => mine === lookupSerial;
    lookup = null;
    render();
    let answer;
    try {
      // Cookies only when the page needs sign-in; none is sent as none (an empty list would replace
      // what Snag keeps for the site).
      const { answer: info, cookies } = await askWithCookiesIfNeeded(
        url, (cookies) => api.lookup(url, cookies ? { cookies } : {}), chrome.cookies, { stillWanted: current });
      answer = { url, info, cookies };
    } catch (error) {
      if (!current()) return;
      if (error?.kind === OFFLINE) {
        offline = true;
        render();
        return;
      }
      answer = { error };
    }
    if (!current()) return;
    lookup = answer;
    render();
  }

  /** Look url up (null: there is nothing to look up); fromPaste: it came from the paste box. */
  function setTarget(url, { fromPaste = false } = {}) {
    target = url || null;
    pasted = Boolean(target) && fromPaste;
    if (target) {
      lookUp(target);
      return;
    }
    lookupSerial += 1;
    lookup = null;
    render();
  }

  /** The same address again, as it came. */
  const askAgain = () => setTarget(target, { fromPaste: pasted });

  /** Ask everything again (Try again after Snag wasn't running). */
  function retry() {
    offline = false;
    loadStatus();
    loadSettings();
    askAgain();
  }

  // --- the views ---

  // One paste box for the popup's life, so its text and its field error survive the views around it.
  const paste = (() => {
    const inputId = uniqueId("popup-paste");
    const errorId = uniqueId("popup-paste-error");
    const input = h("input", {
      id: inputId, class: "input", type: "text", inputmode: "url", autocomplete: "off", spellcheck: "false",
      placeholder: "Paste a link to a video or playlist", "aria-describedby": errorId,
    });
    const error = h("p", { id: errorId, class: "field-error", role: "alert" });
    const setError = (text) => {
      replace(error, text ?? "");
      if (text) input.setAttribute("aria-invalid", "true");
      else input.removeAttribute("aria-invalid");
    };
    const form = h("form", {
      class: "popup-paste", role: "search", "aria-label": "Look up a link",
      onSubmit: (event) => {
        event.preventDefault();
        const found = pastedTarget(input.value);
        setError(found.error);
        if (found.url) setTarget(found.url, { fromPaste: true });
      },
    },
    h("label", { class: "visually-hidden", for: inputId }, "Link to a video or playlist"),
    h("div", { class: "popup-paste-row" }, input, h("button", { type: "submit", class: "btn" }, "Look up")),
    error);
    return { element: form, setError, focus: () => input.focus() };
  })();

  /** Names the playlist on the panel's "part of a playlist" line once a (flat, list-only) lookup of it
   * answers; a failure, or an answer after the panel has gone, leaves things as they are. */
  async function namePlaylist(shown, url) {
    let info;
    try {
      info = await api.lookup(url);
    } catch {
      return;
    }
    if (panel !== shown || info?.type !== "playlist" || !info.title) return;
    shown.setPlaylistName(info.title);
  }

  function videoView(view) {
    panel = createDownloadPanel({
      api, store, url: lookup.url, info: view.info, settings: store.getState().settings, compact: true, toast,
      cookies: lookup.cookies ?? null,
      onChoosePlaylist: view.playlistLink ? () => go(view.playlistLink) : null,
    });
    if (view.playlistLink) namePlaylist(panel, view.info.playlist.url);
    return panel.element;
  }

  function playlistView(view) {
    return h("section", { class: "card popup-playlist stack", "aria-label": view.summary.title },
      h("div", { class: "stack", style: { "--gap": "2px" } },
        h("h2", { class: "popup-title", dir: "auto" }, view.summary.title),
        h("p", { class: "small muted" }, `Playlist · ${view.summary.text}`)),
      h("div", { class: "cluster" },
        h("button", { type: "button", class: "btn btn-primary", onClick: () => go(view.chooseLink) }, "Choose videos"),
        view.singleVideo
          ? h("button", { type: "button", class: "btn", onClick: () => setTarget(view.singleVideo) }, "Just this video")
          : null));
  }

  function setupView(view) {
    const error = h("p", { class: "field-error small", role: "alert" });
    let action;
    if (view.failed && view.canRetry) {
      action = h("button", {
        type: "button", class: "btn btn-sm",
        onClick: async () => {
          action.disabled = true;
          replace(error);
          try {
            await api.retrySetup();
          } catch (failure) {
            replace(error, failure?.message || "Snag couldn't start setting up again");
          } finally {
            action.disabled = false;
          }
        },
      }, "Retry setup");
    } else {
      action = h("button", { type: "button", class: "btn btn-sm", onClick: () => go(appLink()) }, "See progress in Snag");
    }
    return h("div", { class: ["note", "popup-setup", "stack", view.failed ? "note-danger" : "note-accent"] },
      h("div", { class: "stack", role: "status" },
        h("strong", null, view.title),
        h("p", { class: "small" }, view.message)),
      h("div", { class: "cluster" }, action),
      error);
  }

  function viewElement(view) {
    switch (view.view) {
      case VIEW.LOADING:
        return h("p", { class: "lookup-status small muted popup-loading" },
          h("span", { class: "spinner", "aria-hidden": "true" }), h("span", { role: "status" }, "Looking up this page…"));
      case VIEW.VIDEO:
        return videoView(view);
      case VIEW.PLAYLIST:
        return playlistView(view);
      case VIEW.NOTHING:
        paste.setError(view.pasteError);
        return h("div", { class: "popup-empty stack" },
          h("h2", { class: "popup-title" }, view.text),
          h("p", { class: "small muted" }, "Paste a link to a video or playlist to download it."),
          paste.element);
      case VIEW.ERROR:
        paste.setError(null);
        return h("div", { class: "popup-empty stack" },
          h("p", { class: "note note-danger", role: "alert" }, view.text),
          view.canRetry
            ? h("div", { class: "cluster" }, h("button", { type: "button", class: "btn", onClick: askAgain }, "Try again"))
            : null,
          paste.element);
      case VIEW.OFFLINE:
        return h("div", { class: "popup-empty stack" },
          h("p", { class: "note note-warning", role: "alert" }, view.text),
          h("div", { class: "cluster" }, h("button", { type: "button", class: "btn", onClick: retry }, "Try again")));
      case VIEW.SETUP:
        return setupView(view);
      default:
        return null;
    }
  }

  /** A key that changes when the view must be drawn again (not on every status or progress event). */
  function keyOf(view) {
    return [view.view, lookupSerial, target ?? "", view.title ?? "", view.message ?? "", view.text ?? "", view.pasteError ?? "",
      view.canRetry ?? ""].join("\u0000");
  }

  function render() {
    const view = selectView({ reading, target, pasted, snag: snagPage, offline, status, lookup });
    const shownNotice = view.view === VIEW.SETUP || view.view === VIEW.OFFLINE ? null : noticeFor(status);
    notice.hidden = !shownNotice;
    notice.classList.toggle("note-accent", shownNotice?.tone === "accent");
    notice.classList.toggle("note-warning", shownNotice?.tone === "warning");
    notice.classList.toggle("note-danger", shownNotice?.tone === "danger");
    setText(noticeText, shownNotice?.text ?? "");
    setText(noticeButton, shownNotice?.action ?? "");
    openButton.hidden = view.view === VIEW.OFFLINE; // nothing to open while Snag isn't running

    const key = keyOf(view);
    if (key === shownKey) return;
    const leftSetup = shownView === VIEW.SETUP;
    shownKey = key;
    shownView = view.view;
    panel?.destroy();
    panel = null;
    replace(content, viewElement(view));
    // The paste box is where to type when there is nothing else, and stays so after a pasted link fails.
    if (view.view === VIEW.NOTHING || (view.view === VIEW.ERROR && pasted)) paste.focus();
    // A lookup that failed while Snag was still setting up (no yt-dlp yet) is asked again once it's ready.
    if (leftSetup && view.view === VIEW.ERROR) askAgain();
  }

  // --- the hub: live status and the queue line ---

  function renderQueue(state) {
    const line = queueLine(state);
    queue.hidden = line.hidden;
    setText(queueText, line.text);
    setProgress(queueProgress, line.progress === null ? null : { value: line.progress, tone: line.paused ? "paused" : null, indeterminate: false });
  }

  let wasConnected = false;
  const unsubscribe = store.subscribe((state) => {
    renderQueue(state);
    if (state.ready && state.status !== status) {
      status = state.status;
      render();
    }
    // Snag started while the popup was open: the hub got through, so ask again.
    const connected = state.connection === CONNECTION.CONNECTED;
    if (connected && !wasConnected && offline) retry();
    wasConnected = connected;
  });

  return {
    element,
    async start() {
      renderQueue(store.getState());
      render();
      const page = pageTarget(await activeTabUrl(chrome));
      reading = false;
      snagPage = page.snag;
      loadStatus();
      loadSettings();
      setTarget(page.target);
    },
    destroy() {
      unsubscribe();
      panel?.destroy();
    },
  };
}
