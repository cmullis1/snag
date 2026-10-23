// The History tab: search (debounced), All / Video / Audio, the list newest first ("Title · when" over
// "summary · size", or "Failed · <message>"), each row's Open folder, Download again and "…" menu
// (Remove from history), "Showing N of M · Load more", and Clear history with a confirm step.
// Deleting History never deletes files.
//
//   const history = createHistoryList({api, toast, onDownloadAgain(url), now});
//   container.append(history.element);  history.refresh();  ...  history.destroy();
//
// refresh() reloads what's shown (call it when the tab is shown or a download finishes); focus() puts
// focus in the search box. Each row's "…" is a disclosure button showing its one action. An answer to a
// load that a removal overtook is dropped (and the load made again), so a removed entry never comes back.

import { busy, isInert, setInert } from "./busy.js";
import { h, replace } from "./el.js";
import { uniqueId } from "./ids.js";
import { icon } from "./icons.js";
import {
  KIND_FILTERS, applyPage, canLoadMore, emptyText, historyRequest, initialHistory, pagingText, refreshRequest, removeEntry,
  rowText, withQuery,
} from "./history-model.js";

const SEARCH_DELAY = 250;

export function createHistoryList({
  api, toast = null, onDownloadAgain = null, now = () => new Date(), debounce = SEARCH_DELAY,
  setTimer = (fn, ms) => setTimeout(fn, ms), clearTimer = (id) => clearTimeout(id),
}) {
  let state = initialHistory();
  let request = 0; // the latest request's number: older answers are dropped
  let loading = false;
  let searchTimer = null;
  let openMenu = null; // {button, menu}
  let confirming = false; // Clear history's confirm step is showing
  const rows = new Map(); // entry id -> {element, key, focusTarget}

  // --- tools: search, kind, clear ---

  const searchId = uniqueId("history-search");
  const search = h("input", {
    id: searchId, class: "input history-search", type: "search", placeholder: "Search by title", autocomplete: "off",
    spellcheck: "false",
    onInput: () => {
      clearTimer(searchTimer);
      searchTimer = setTimer(() => setQuery({ search: search.value }), debounce);
    },
    onKeydown: (event) => {
      if (event.key !== "Enter") return;
      clearTimer(searchTimer);
      setQuery({ search: search.value });
    },
  });
  const kindButtons = new Map();
  const kinds = h("div", { class: "segmented history-kinds", role: "radiogroup", "aria-label": "Show" },
    KIND_FILTERS.map((filter, position) => {
      const button = h("button", {
        type: "button", role: "radio",
        onClick: () => setQuery({ kind: filter.value }),
        onKeydown: (event) => {
          const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[event.key];
          if (!step) return;
          event.preventDefault();
          const next = KIND_FILTERS[(position + step + KIND_FILTERS.length) % KIND_FILTERS.length];
          setQuery({ kind: next.value });
          kindButtons.get(next.value).focus();
        },
      }, filter.label);
      kindButtons.set(filter.value, button);
      return button;
    }));

  const clearButton = h("button", { type: "button", class: "btn btn-ghost btn-sm btn-danger", onClick: () => setConfirming(true) },
    "Clear history");
  const confirmYes = h("button", { type: "button", class: "btn btn-sm btn-primary btn-danger", onClick: clearAll }, "Clear history");
  const confirmNo = h("button", { type: "button", class: "btn btn-sm", onClick: () => setConfirming(false) }, "Cancel");
  const confirm = h("div", { class: "note note-danger history-confirm", role: "group", "aria-label": "Clear history", hidden: true },
    h("span", { class: "grow" }, "Clear all of History? Your downloaded files stay where they are."),
    h("span", { class: "cluster" }, confirmYes, confirmNo));

  // --- list and foot ---

  const error = h("p", { class: "field-error", role: "alert" });
  const list = h("ul", { class: "rows history-list", role: "list", "aria-label": "Downloads" });
  const empty = h("p", { class: "history-empty muted", hidden: true });
  const loadingNote = h("p", { class: "history-loading muted small", hidden: true },
    h("span", { class: "spinner", "aria-hidden": "true" }), h("span", null, "Loading…"));
  const paging = h("span", { class: "small muted", "aria-live": "polite" });
  const more = h("button", { type: "button", class: "link-button small", onClick: () => { if (!isInert(more)) load({ more: true }); },
  }, "Load more");
  const element = h("section", { class: "history", "aria-label": "History" },
    h("div", { class: "history-tools" },
      h("label", { class: "visually-hidden", for: searchId }, "Search History"),
      search, kinds),
    error,
    confirm,
    list,
    empty,
    loadingNote,
    h("div", { class: "history-foot spread" },
      h("span", { class: "cluster" }, paging, more),
      clearButton));

  // --- menu ---

  function closeMenu({ focus = false } = {}) {
    if (!openMenu) return;
    const { button, menu } = openMenu;
    openMenu = null;
    menu.hidden = true;
    button.setAttribute("aria-expanded", "false");
    if (focus) button.focus();
  }

  function onDocumentClick(event) {
    if (openMenu && !openMenu.button.contains(event.target) && !openMenu.menu.contains(event.target)) closeMenu();
  }
  globalThis.document?.addEventListener?.("click", onDocumentClick);

  function moreMenu(entry, title) {
    const menuId = uniqueId("history-menu");
    const remove = h("button", {
      type: "button", class: "menu-item",
      onClick: () => {
        closeMenu();
        removeOne(entry);
      },
      onKeydown: (event) => {
        if (event.key === "Escape" || event.key === "Tab") closeMenu({ focus: event.key === "Escape" });
      },
    }, "Remove from history");
    const menu = h("div", { id: menuId, class: "menu", hidden: true }, remove);
    const button = h("button", {
      type: "button", class: "btn btn-ghost btn-sm btn-icon", "aria-label": `More: ${title}`,
      "aria-expanded": "false", "aria-controls": menuId,
      onClick: () => {
        const opening = openMenu?.menu !== menu;
        closeMenu();
        if (!opening) return;
        openMenu = { button, menu };
        menu.hidden = false;
        button.setAttribute("aria-expanded", "true");
        remove.focus();
      },
      onKeydown: (event) => {
        if (event.key === "Escape") closeMenu({ focus: true });
      },
    }, icon("more"));
    return h("span", { class: "menu-wrap" }, button, menu);
  }

  // --- rows ---

  function iconButton(name, label, run) {
    const button = h("button", {
      type: "button", class: "btn btn-ghost btn-sm btn-icon", "aria-label": label, title: label.split(":")[0],
      onClick: async () => {
        try {
          await busy(button, run);
        } catch (failure) {
          toast?.show(failure?.message || "Something went wrong", { kind: "error" });
        }
      },
    }, icon(name));
    return button;
  }

  function buildRow(entry) {
    const text = rowText(entry, { now: now() });
    const open = text.canOpen ? iconButton("folder", `Open folder: ${text.title}`, () => api.openHistoryFolder(entry.id)) : null;
    const again = entry.url && onDownloadAgain
      ? iconButton("download", `Download again: ${text.title}`, () => onDownloadAgain(entry.url)) : null;
    const menu = moreMenu(entry, text.title);
    const element = h("li", { class: ["row", "history-row", text.failed && "is-failed"], dataset: { id: entry.id } },
      h("div", { class: "row-main" },
        h("div", { class: "history-line" },
          h("span", { class: "row-title truncate", title: text.title, dir: "auto" }, text.title),
          text.when ? h("span", { class: "history-when muted small" }, `· ${text.when}`) : null),
        h("div", { class: ["row-meta", text.failed && "danger-text"] }, text.detail)),
      // A failed download has no folder to open: a gap keeps the other buttons lined up with the rows above.
      h("div", { class: "history-actions" }, open ?? h("span", { class: "history-action-gap", "aria-hidden": "true" }), again, menu));
    return { element, focusTarget: open ?? again ?? menu.firstChild };
  }

  function render() {
    for (const [value, button] of kindButtons) {
      button.setAttribute("aria-checked", String(value === state.kind));
      button.setAttribute("tabindex", value === state.kind ? "0" : "-1");
    }
    const seen = new Set();
    state.entries.forEach((entry, index) => {
      const key = JSON.stringify(entry);
      let row = rows.get(entry.id);
      if (!row || row.key !== key) {
        const built = buildRow(entry);
        row?.element.remove();
        row = { ...built, key };
        rows.set(entry.id, row);
      }
      seen.add(entry.id);
      if (list.children[index] !== row.element) list.insertBefore(row.element, list.children[index] ?? null);
    });
    for (const [id, row] of rows) {
      if (!seen.has(id)) {
        row.element.remove();
        rows.delete(id);
      }
    }
    list.hidden = state.entries.length === 0;
    empty.hidden = !state.loaded || state.entries.length > 0;
    replace(empty, emptyText(state));
    loadingNote.hidden = !loading || state.entries.length > 0;
    replace(paging, pagingText(state));
    more.hidden = !canLoadMore(state);
    setInert(more, loading);
    replace(more, loading && state.entries.length ? "Loading…" : "Load more");
    confirm.hidden = !confirming;
    clearButton.hidden = confirming || !state.loaded || (state.total === 0 && !state.search && !state.kind);
  }

  async function load({ more: next = false, refresh = false } = {}) {
    const number = ++request;
    const query = refresh ? refreshRequest(state) : historyRequest(state, { more: next });
    loading = true;
    render();
    try {
      const page = await api.listHistory(query);
      if (number !== request) return;
      state = applyPage(state, page, { more: next });
      replace(error);
    } catch (failure) {
      if (number !== request) return;
      replace(error, failure?.message || "Couldn't load History");
    } finally {
      if (number === request) {
        loading = false;
        render();
      }
    }
  }

  function setQuery(query) {
    const next = withQuery(state, query);
    if (next === state) return;
    state = next;
    closeMenu();
    load();
  }

  async function removeOne(entry) {
    const position = state.entries.findIndex((item) => item.id === entry.id);
    try {
      await api.removeHistoryEntry(entry.id);
    } catch (failure) {
      toast?.show(failure?.message || "Couldn't remove that from History", { kind: "error" });
      return;
    }
    // A load under way may answer with the entry still in it: drop that answer and load again.
    const overtaken = loading;
    if (overtaken) {
      request += 1;
      loading = false;
    }
    state = removeEntry(state, entry.id);
    render();
    if (overtaken) load({ refresh: true });
    toast?.show("Removed from History");
    const neighbour = state.entries[Math.min(position, state.entries.length - 1)];
    (neighbour ? rows.get(neighbour.id)?.focusTarget : search)?.focus();
  }

  function setConfirming(on) {
    confirming = on;
    render();
    if (on) confirmNo.focus();
    else clearButton.focus();
  }

  async function clearAll() {
    try {
      await busy(confirmYes, async () => {
        await api.clearHistory();
        confirming = false;
        state = withQuery(initialHistory(), { search: state.search, kind: state.kind });
        toast?.show("History cleared");
        await load();
        search.focus();
      });
    } catch (failure) {
      toast?.show(failure?.message || "Couldn't clear History", { kind: "error" });
    }
  }

  render();

  return {
    element,
    /** Reload what's shown (the first page the first time). */
    refresh() {
      return load({ refresh: state.loaded });
    },
    focus() {
      search.focus();
    },
    destroy() {
      clearTimer(searchTimer);
      globalThis.document?.removeEventListener?.("click", onDocumentClick);
    },
  };
}
