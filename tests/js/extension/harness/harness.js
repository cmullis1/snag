// Runs the real popup (extension/popup/popup.js) in an ordinary tab against the scratch Snag that
// serve.py starts, with a stand-in for the parts of `chrome` the popup uses:
//   ?tab=<address>   the active tab's address (left out: a tab whose address the popup can't see,
//                    like chrome://extensions)
//   ?cookies=0       the site has no cookies (by default it has one sign-in cookie)
//   ?offline=1       Snag doesn't answer: requests to it fail as they do when nothing listens on its
//                    port (the page itself still comes from the scratch Snag, so it can't be stopped)
// What the popup asks the browser to do (open or focus tabs, close itself) is recorded in
// window.harness.calls and the console instead of happening.

const params = new URLSearchParams(location.search);
const tabUrl = params.get("tab");
const calls = [];
globalThis.harness = { calls };

function record(...call) {
  calls.push(call);
  console.info("harness:", ...call);
}

const COOKIE = {
  domain: ".youtube.com", name: "SID", value: "harness", path: "/", secure: true, httpOnly: true,
  hostOnly: false, session: false, expirationDate: 1_900_000_000,
};

globalThis.chrome = {
  tabs: {
    query: async (filter) => {
      record("tabs.query", filter);
      return filter.active ? [{ id: 1, windowId: 1, ...(tabUrl ? { url: tabUrl } : {}) }] : [];
    },
    update: async (id, props) => {
      record("tabs.update", id, props);
      return { id, ...props };
    },
    create: async (props) => {
      record("tabs.create", props);
      return { id: 2, windowId: 1, ...props };
    },
  },
  windows: {
    update: async (id, props) => {
      record("windows.update", id, props);
      return { id };
    },
  },
  cookies: {
    getAll: async (filter) => {
      record("cookies.getAll", filter);
      return params.get("cookies") === "0" ? [] : [COOKIE];
    },
  },
};
globalThis.close = () => record("window.close");

if (params.get("offline") === "1") {
  const snag = (url) => String(url).startsWith("http://127.0.0.1:47823/") || String(url).startsWith("ws://127.0.0.1:47823/");
  const realFetch = globalThis.fetch.bind(globalThis);
  globalThis.fetch = (input, init) => (snag(input?.url ?? input)
    ? Promise.reject(new TypeError("Failed to fetch"))
    : realFetch(input, init));
  const RealWebSocket = globalThis.WebSocket;
  globalThis.WebSocket = function WebSocket(url, protocols) {
    if (!snag(url)) return new RealWebSocket(url, protocols);
    const socket = new EventTarget();
    setTimeout(() => socket.onclose?.({ code: 1006 }), 0);
    socket.close = () => {};
    return socket;
  };
}

await import("/extension/popup/popup.js");
