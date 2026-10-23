// The Download tab: a paste box that looks a link up on paste or Enter, then the download panel for a
// video, or for a playlist its name, count and "Choose videos" (the playlist picker).
//
//   const tab = createDownloadTab({api, store, toast, getSettings, navigate, remember});
//   tab.show({url, lookUp})   // the route's ?url=…: looked up unless it's what's shown already; with
//                             // lookUp false (a link from outside the page that isn't YouTube's) it
//                             // waits in the box for Look up
//   tab.forget(url)   // the next show({url}) looks it up again (History's Download again)
//
// getSettings() → the settings (or null); navigate(route, params) changes the route; remember(url, info)
// keeps a lookup for the playlist picker; recall(url) → a kept lookup (or null), shown at once when the
// route brings a link back (Back from the picker) instead of looking it up again. A newer lookup cancels
// the one under way, and an answer for a link no longer shown is dropped. A route's link that holds no
// link (a malformed one) isn't looked up: the tab says it didn't come through.

import { isInert, setInert } from "../ui/components/busy.js";
import { h, replace } from "../ui/components/el.js";
import { uniqueId } from "../ui/components/ids.js";
import { createDownloadPanel } from "../ui/components/download-panel.js";
import { LINK_LOST, PRESS_LOOK_UP, isLink, lookupTarget, playlistSummary } from "./download-model.js";
import { ROUTE } from "./router.js";

export function createDownloadTab({
  api, store, toast, getSettings, navigate, remember = () => {}, recall = () => null, replaceUrl = () => {},
}) {
  let controller = null; // the lookup under way
  let shownUrl = null; // the link whose result is shown (or being looked up)
  let panel = null;

  const inputId = uniqueId("paste");
  const hintId = uniqueId("paste-hint");
  const input = h("input", {
    id: inputId, class: "input input-large paste-input", type: "text", inputmode: "url", autocomplete: "off",
    spellcheck: "false", placeholder: "Paste a link to a video or playlist", "aria-describedby": hintId,
    onPaste: () => setTimeout(() => submit(), 0),
  });
  const button = h("button", { type: "submit", class: "btn btn-primary paste-button" }, "Look up");
  const form = h("form", {
    class: "paste", role: "search", "aria-label": "Look up a link",
    onSubmit: (event) => {
      event.preventDefault();
      if (!isInert(button)) submit({ force: true });
    },
  },
  h("label", { class: "visually-hidden", for: inputId }, "Link to a video or playlist"),
  input, button);
  const status = h("p", { class: "lookup-status small muted", role: "status", "aria-live": "polite" });
  const error = h("p", { class: "field-error lookup-error", role: "alert" });
  const result = h("div", { class: "lookup-result" });
  const hint = h("p", { id: hintId, class: "lookup-hint muted" },
    "Paste a link from YouTube or another video site. Snag looks it up as soon as you paste.");
  const element = h("div", { class: "download-tab" }, form, status, error, hint, result);

  function setBusy(busy) {
    replace(status, busy ? [h("span", { class: "spinner", "aria-hidden": "true" }), h("span", null, "Looking up…")] : []);
    status.classList.toggle("is-busy", busy);
    setInert(button, busy);
  }

  function clearResult() {
    panel?.destroy();
    panel = null;
    replace(result);
  }

  function showPlaylist(url, info) {
    const summary = playlistSummary(info);
    replace(result, h("section", { class: "card playlist-card", "aria-label": summary.title },
      h("div", { class: "spread" },
        h("div", { class: "stack", style: { "--gap": "2px" } },
          h("h2", { class: "playlist-card-title", dir: "auto" }, summary.title),
          h("p", { class: "small muted" }, `Playlist · ${summary.text}`)),
        h("button", {
          type: "button", class: "btn btn-primary", onClick: () => navigate(ROUTE.PLAYLIST, { url }),
        }, "Choose videos"))));
  }

  function showVideo(url, info) {
    panel = createDownloadPanel({
      api, store, url, info, settings: getSettings(), toast,
      onChoosePlaylist: (playlist) => navigate(ROUTE.PLAYLIST, { url: playlist.url }),
    });
    replace(result, panel.element);
  }

  /** url: the link that was looked up (downloads ask for it, so Snag reuses that lookup). */
  function showInfo(url, info) {
    if (info?.type === "playlist") showPlaylist(url, info);
    else showVideo(url, info);
  }

  /** Stop any lookup under way and clear what's shown, for url. */
  function startOver(url) {
    controller?.abort();
    controller = null;
    setBusy(false);
    shownUrl = url;
    clearResult();
    replace(error);
    hint.hidden = true;
  }

  /** Show a kept lookup, cancelling any lookup under way. */
  function showKept(url, info) {
    startOver(url);
    showInfo(url, info);
  }

  async function lookUp(url) {
    controller?.abort();
    const mine = new AbortController();
    controller = mine;
    shownUrl = url;
    clearResult();
    replace(error);
    hint.hidden = true;
    setBusy(true);
    try {
      const info = await api.lookup(url, { signal: mine.signal });
      if (controller !== mine) return;
      remember(url, info);
      showInfo(url, info);
    } catch (failure) {
      if (controller !== mine || mine.signal.aborted) return;
      shownUrl = null;
      replace(error, failure?.message || "Snag couldn't look that up");
    } finally {
      if (controller === mine) {
        controller = null;
        setBusy(false);
      }
    }
  }

  /** Look the box's link up (force: even when it's the one shown). */
  function submit({ force = false } = {}) {
    const target = lookupTarget(input.value);
    if (!target.url) {
      replace(error, target.error ?? "");
      return;
    }
    if (!force && target.url === shownUrl) return;
    replaceUrl(target.url);
    lookUp(target.url);
  }

  return {
    element,
    /** The route's parameters: a url is looked up unless it's the one shown, or (lookUp false) put in
     * the box to wait for Look up; a kept lookup of it is shown either way. */
    show({ url = null, lookUp: mayLookUp = true } = {}) {
      if (url && url !== shownUrl) {
        input.value = url;
        if (!isLink(url)) {
          startOver(url);
          replace(error, LINK_LOST);
          return;
        }
        const kept = recall(url);
        if (kept) showKept(url, kept);
        else if (mayLookUp) lookUp(url);
        else {
          startOver(url);
          replace(status, PRESS_LOOK_UP);
        }
      }
    },
    focus() {
      input.focus();
    },
    /** url's result may be stale: show({url}) looks it up again even when it's the one shown. */
    forget(url) {
      if (url === shownUrl) shownUrl = null;
    },
  };
}
