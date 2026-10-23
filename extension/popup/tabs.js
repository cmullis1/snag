// The popup's use of the browser's tabs, without the "tabs" permission:
// - the active tab's address: tabs.query fills in tab.url for web pages through the host permissions
//   (and activeTab, granted by the toolbar click), and leaves it out for chrome://, edge:// and the like;
// - "Open app" / "Choose videos": an open full tab is found by its address (the host permission for
//   Snag's address lets tabs.query match it), brought forward and, for a link into it, sent there;
//   tabs.update and windows.update need no permission. Otherwise a new tab opens.

import { APP_TAB_PATTERNS, appLink } from "./model.js";

/** The active tab's address in the popup's window, or null when the popup can't see it. */
export async function activeTabUrl(chrome) {
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    return typeof tab?.url === "string" && tab.url ? tab.url : null;
  } catch {
    return null;
  }
}

async function openTabs(chrome) {
  try {
    return await chrome.tabs.query({ url: [...APP_TAB_PATTERNS] });
  } catch {
    return [];
  }
}

/** Show url (the full tab, or a link into it) in the full tab already open, the one used last, or in a
 * new tab. Opening just the app leaves an open tab on whatever page it shows. */
export async function openApp(chrome, url = appLink()) {
  const tabs = await openTabs(chrome);
  const tab = tabs.reduce((best, item) => (!best || (item.lastAccessed ?? 0) > (best.lastAccessed ?? 0) ? item : best), null);
  if (tab) {
    try {
      const change = url === appLink() || tab.url === url ? { active: true } : { active: true, url };
      await chrome.tabs.update(tab.id, change);
      await chrome.windows.update(tab.windowId, { focused: true });
      return;
    } catch {
      // Closed meanwhile: open a new one.
    }
  }
  await chrome.tabs.create({ url });
}
