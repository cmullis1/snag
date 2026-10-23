// The full app tab's frame, made testable: header tabs (Download, History, Settings; a dot on Settings
// when a tool is missing or a yt-dlp update is available), the hash router (#download, #history,
// #settings, #playlist?url=…), the queue bar, toasts, and the first-run setup screen that stands in for
// the tabs while Snag installs its tools. app.js gives it the real page; tests give it fakes.
//
//   const shell = createShell({api, store, toast, win: window, doc: document});
//   shell.start();   // draws the page into doc.body, follows the hash, loads the settings
//   shell.destroy(); // stops following the store and the clock (tests)
//
// win: {location, history, addEventListener}; doc: {body, title}. The settings come from the store
// (GET /api/settings is dispatched into it as a settings event, then the server's settings events keep
// it current). A route changed from inside the page moves focus to the new view's heading or main
// control; the setup screen takes focus when it appears. Lookups are kept to show a link again at once,
// but never for History's Download again, and a finished job drops those of its video (and of playlists
// holding it), so "already downloaded" stays current.

import { EVENT, END_JOB_STATES, CONNECTION } from "../ui/fields.js";
import { jobList } from "../ui/store.js";
import { h, replace, setText } from "../ui/components/el.js";
import { createQueueBar } from "../ui/components/queue-bar.js";
import { createPlaylistPicker } from "../ui/components/playlist-picker.js";
import { createHistoryList } from "../ui/components/history-list.js";
import { createSettingsForm } from "../ui/components/settings-form.js";
import { createSetupScreen } from "../ui/components/setup-screen.js";
import { settingsAttention, setupScreen } from "../ui/components/setup-model.js";
import { createDownloadTab } from "./download-tab.js";
import { isLink, isYouTubeLink, readAlike } from "./download-model.js";
import { ROUTE, buildHash, parseHash, tabOf } from "./router.js";
import { connectionNote, pageTitle } from "./shell-model.js";

const LOOKUPS_KEPT = 20;
const TABS = [
  { route: ROUTE.DOWNLOAD, label: "Download" },
  { route: ROUTE.HISTORY, label: "History" },
  { route: ROUTE.SETTINGS, label: "Settings" },
];

export function createShell({
  api, store, toast, win, doc, pickFolder = null,
  setTimer = (fn, ms) => setTimeout(fn, ms), clearTimer = (id) => clearTimeout(id),
}) {
  const picker = pickFolder ?? (async (initial) => (await api.pickFolder(initial))?.path ?? null);
  const getSettings = () => store.getState().settings;

  // --- lookups: kept so the picker and the Download tab show a link again at once ---

  const lookups = new Map(); // url -> lookup JSON, newest last
  const pending = new Map(); // url -> the lookup under way

  /** Keep info under the link asked for, and under its page's own address (info.url) when the two read
   * alike, as the server's cache does (snag/info/lookup.py LookupService._store): a video looked up from
   * watch?v=x&list=PL1 is "part of a playlist PL1", which its plain address must not show. */
  function remember(url, info) {
    lookups.delete(url);
    lookups.set(url, info);
    if (info?.url && info.url !== url && readAlike(info.url, url)) {
      lookups.delete(info.url);
      lookups.set(info.url, info);
    }
    while (lookups.size > LOOKUPS_KEPT) lookups.delete(lookups.keys().next().value);
  }

  /** Forget url's kept lookup, under every link it was kept by. Returns the links forgotten. */
  function forget(url) {
    const info = lookups.get(url);
    const gone = new Set([url]);
    if (info) for (const [key, kept] of lookups) if (kept === info) gone.add(key);
    for (const key of gone) lookups.delete(key);
    return gone;
  }

  /** Whether a kept lookup is about the video a job downloaded: the video itself, or a playlist holding it. */
  function about(info, spec) {
    if (spec.url && info?.url === spec.url) return true;
    if (!spec.video_id || info?.site !== spec.site) return false;
    if (info.type === "playlist") return (Array.isArray(info.entries) ? info.entries : []).some((entry) => entry?.video_id === spec.video_id);
    return info.video_id === spec.video_id;
  }

  /** A job finished: kept lookups of its video (and playlists holding it) no longer say what's downloaded. */
  function forgetDownloaded(spec) {
    if (!spec) return;
    const gone = new Set();
    for (const [key, info] of [...lookups]) {
      if (key === spec.url || about(info, spec)) for (const link of forget(key)) gone.add(link);
    }
    // A picker built from a forgotten playlist is built again next time, unless it's on screen (where
    // it keeps what's been ticked).
    if (pickerUrl !== null && gone.has(pickerUrl) && current !== ROUTE.PLAYLIST) {
      playlistPicker = null;
      pickerUrl = null;
    }
  }

  /** A link's lookup: the kept one, the one under way, or a new one. */
  function lookupOnce(url) {
    if (lookups.has(url)) return Promise.resolve(lookups.get(url));
    if (!pending.has(url)) {
      const request = api.lookup(url).then((info) => {
        remember(url, info);
        return info;
      }).finally(() => pending.delete(url));
      pending.set(url, request);
    }
    return pending.get(url);
  }

  // --- settings: the store holds them ---

  async function loadSettings() {
    const before = getSettings();
    try {
      const settings = await api.getSettings();
      // A settings event that arrived meanwhile is newer than this answer.
      if (getSettings() === before) store.dispatch({ type: EVENT.SETTINGS, settings });
    } catch (error) {
      if (error?.kind !== "offline") toast.show(error.message, { kind: "error" });
    }
    return getSettings();
  }

  // --- the frame ---

  const tabLinks = new Map();
  const settingsDot = h("span", { class: "dot tab-dot", hidden: true });
  const settingsDotLabel = h("span", { class: "visually-hidden" });
  const nav = h("nav", { class: "tabs", "aria-label": "Snag" },
    TABS.map((tab) => {
      const link = h("a", { class: "tab", href: buildHash(tab.route) }, tab.label,
        tab.route === ROUTE.SETTINGS ? [settingsDot, settingsDotLabel] : null);
      tabLinks.set(tab.route, link);
      return link;
    }));
  const header = h("header", { class: "app-header" },
    h("div", { class: "app-header-inner" }, h("span", { class: "app-name" }, "Snag"), nav));
  const offline = h("p", { class: "note note-warning app-offline", role: "status", hidden: true });
  const views = {
    [ROUTE.DOWNLOAD]: h("div", { class: "view", hidden: true }),
    [ROUTE.PLAYLIST]: h("div", { class: "view", hidden: true }),
    [ROUTE.HISTORY]: h("div", { class: "view", hidden: true }),
    [ROUTE.SETTINGS]: h("div", { class: "view", hidden: true }),
  };
  const setupView = h("div", { class: "view", hidden: true });
  const main = h("main", { class: "app-main", id: "main" }, offline, Object.values(views), setupView);
  const queueBar = createQueueBar({ api, store, toast, pickFolder: picker });
  const footer = h("footer", { class: "app-queue" }, queueBar.element);

  // --- routing ---

  // Links opened from inside the page (History's Download again, Back, Choose videos): looked up
  // whatever their site. A link that arrives from outside (the address, a page that links here) is
  // looked up by itself only when it is YouTube's (see download-model.js isYouTubeLink).
  const opened = new Set();
  const mayLookUp = (url) => isYouTubeLink(url) || opened.has(url) || lookups.has(url);

  function navigate(route, params = {}) {
    if (params.url) opened.add(params.url);
    const hash = buildHash(route, params);
    if (win.location.hash === hash) render({ focus: true });
    else win.location.hash = hash;
  }

  /** Show route instead of the current one without a new history entry (Back skips the old one). */
  function redirect(route, params = {}) {
    win.location.replace(buildHash(route, params));
  }

  // --- the views (made when first shown) ---

  let downloadTab = null;
  let historyList = null;
  let settingsForm = null;
  let playlistPicker = null;
  let pickerUrl = null;
  let pickerCalls = 0; // showPlaylist calls: of several waiting on the same lookup, only the latest builds
  let setupScreenView = null;

  function showDownload(params, focus) {
    if (!downloadTab) {
      downloadTab = createDownloadTab({
        api, store, toast, getSettings, navigate, remember, recall: (url) => lookups.get(url) ?? null,
        replaceUrl: (url) => win.history.replaceState(null, "", buildHash(ROUTE.DOWNLOAD, { url })),
      });
      views[ROUTE.DOWNLOAD].append(downloadTab.element);
    }
    downloadTab.show({ ...params, lookUp: params.url ? mayLookUp(params.url) : true });
    if (focus) downloadTab.focus();
  }

  async function showPlaylist(params, focus) {
    if (playlistPicker && pickerUrl === params.url) {
      if (focus) playlistPicker.focus();
      return;
    }
    const url = params.url;
    if (!isLink(url)) {
      // Nothing to look up: the Download tab says the link didn't come through.
      pickerUrl = null;
      playlistPicker = null;
      redirect(ROUTE.DOWNLOAD, { url });
      return;
    }
    if (!mayLookUp(url) && !pending.has(url)) {
      // From outside, not YouTube's: the Download tab shows it in the box, waiting for Look up.
      pickerUrl = null;
      playlistPicker = null;
      redirect(ROUTE.DOWNLOAD, { url });
      return;
    }
    const call = ++pickerCalls;
    pickerUrl = url;
    playlistPicker = null;
    const view = views[ROUTE.PLAYLIST];
    const current = () => pickerCalls === call && pickerUrl === url;
    let info = lookups.get(url);
    if (!info) {
      replace(view, h("p", { class: "lookup-status small muted", role: "status" },
        h("span", { class: "spinner", "aria-hidden": "true" }), h("span", null, "Looking up the playlist…")));
      try {
        info = await lookupOnce(url);
      } catch (error) {
        if (!current()) return;
        const back = h("button", { type: "button", class: "link-button", onClick: () => navigate(ROUTE.DOWNLOAD) }, "Back to Download");
        replace(view, h("div", { class: "stack" },
          h("p", { class: "field-error", role: "alert" }, error?.message || "Snag couldn't look that up"),
          h("p", null, back)));
        pickerUrl = null;
        if (focus) back.focus();
        return;
      }
    }
    if (!current()) return;
    if (info?.type !== "playlist") {
      pickerUrl = null;
      redirect(ROUTE.DOWNLOAD, { url });
      return;
    }
    if (!getSettings()) await loadSettings();
    if (!current()) return; // another link was opened while the settings loaded
    playlistPicker = createPlaylistPicker({
      api, url, info, settings: getSettings(), jobs: jobList(store.getState()), toast, pickFolder: picker,
      onBack: () => navigate(ROUTE.DOWNLOAD, { url }),
      onQueued: () => {
        playlistPicker = null;
        pickerUrl = null;
        navigate(ROUTE.DOWNLOAD);
      },
    });
    replace(view, playlistPicker.element);
    if (focus) playlistPicker.focus();
  }

  function showHistory(focus) {
    if (!historyList) {
      historyList = createHistoryList({
        api, toast,
        // Never a kept lookup: a fresh one says the video was downloaded before.
        onDownloadAgain: (url) => {
          forget(url);
          downloadTab?.forget(url);
          navigate(ROUTE.DOWNLOAD, { url });
        },
      });
      views[ROUTE.HISTORY].append(historyList.element);
    }
    historyList.refresh();
    if (focus) historyList.focus();
  }

  function showSettings(focus) {
    if (!settingsForm) {
      settingsForm = createSettingsForm({
        api, store, toast, pickFolder: picker,
        onSettings: (settings) => {
          if (getSettings() !== settings) store.dispatch({ type: EVENT.SETTINGS, settings });
        },
      });
      views[ROUTE.SETTINGS].append(settingsForm.element);
    }
    settingsForm.load();
    if (focus) settingsForm.focus();
  }

  let current = null;
  let setupVisible = false;
  let everConnected = false;
  let failedOnce = false;

  /** Show the route in the hash. focus: the route changed from inside the page, so focus follows. */
  function render({ focus = false } = {}) {
    const { route, params } = parseHash(win.location.hash);
    current = route;
    for (const [name, view] of Object.entries(views)) view.hidden = setupVisible || name !== route;
    for (const [name, link] of tabLinks) {
      if (name === tabOf(route)) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    }
    if (setupVisible) return;
    doc.title = pageTitle(route);
    if (route === ROUTE.DOWNLOAD) showDownload(params, focus);
    else if (route === ROUTE.PLAYLIST) showPlaylist(params, focus);
    else if (route === ROUTE.HISTORY) showHistory(focus);
    else if (route === ROUTE.SETTINGS) showSettings(focus);
  }

  // --- live state: setup screen, Settings dot, connection, History refresh ---

  let historyTimer = null;
  function scheduleHistoryRefresh() {
    clearTimer(historyTimer);
    historyTimer = setTimer(() => historyList?.refresh(), 500);
  }

  function renderLive(state, event) {
    const screen = setupScreen(state.status, state.setup);
    if (screen) {
      if (!setupScreenView) {
        setupScreenView = createSetupScreen({ api, toast });
        setupView.append(setupScreenView.element);
      }
      setupScreenView.update(screen);
    }
    const showSetup = Boolean(screen);
    if (showSetup !== setupVisible) {
      setupVisible = showSetup;
      setupView.hidden = !showSetup;
      nav.hidden = showSetup;
      footer.hidden = showSetup;
      for (const [name, view] of Object.entries(views)) view.hidden = showSetup || name !== current;
      doc.title = pageTitle(showSetup ? "setup" : current);
      if (showSetup) setupScreenView.focus();
      else render({ focus: true });
    }

    const attention = settingsAttention(state.status);
    settingsDot.hidden = !attention;
    settingsDot.className = ["dot", "tab-dot", attention?.tone === "danger" && "dot-danger",
      attention?.tone === "warning" && "dot-warning"].filter(Boolean).join(" ");
    setText(settingsDotLabel, attention ? ` (${attention.label})` : "");

    if (state.connection === CONNECTION.CONNECTED) everConnected = true;
    if (event?.type === EVENT.CONNECTION && event.state === CONNECTION.DISCONNECTED) failedOnce = true;
    const note = connectionNote({ connection: state.connection, everConnected, failedOnce });
    offline.hidden = !note;
    setText(offline, note ?? "");
    if (event?.type === EVENT.CONNECTION && event.state === CONNECTION.CONNECTED && everConnected && failedOnce) {
      loadSettings(); // settings events sent while the connection was down were missed
      failedOnce = false;
    }

    if (event?.type === EVENT.JOB && END_JOB_STATES.includes(event.job?.state)) {
      forgetDownloaded(event.job.spec);
      if (current === ROUTE.HISTORY && historyList) scheduleHistoryRefresh();
    }
  }

  let unsubscribe = () => {};

  return {
    /** Draw the page, follow the hash and the store, and load the settings. */
    start() {
      doc.body.replaceChildren(header, main, footer, toast.element);
      unsubscribe = store.subscribe(renderLive);
      win.addEventListener("hashchange", () => render({ focus: true }));
      renderLive(store.getState(), null);
      return loadSettings().then(() => render());
    },
    destroy() {
      unsubscribe();
      clearTimer(historyTimer);
      queueBar.destroy();
      downloadTab?.destroy?.();
      historyList?.destroy();
      settingsForm?.destroy();
    },
    render,
    navigate,
    elements: { header, nav, main, offline, views, setupView, footer, settingsDot, settingsDotLabel, tabLinks },
  };
}
