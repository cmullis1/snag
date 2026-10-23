// The live connection to Snag's WebSocket hub (Plan 2a Task 5). The server sends a snapshot as soon
// as a client connects, then events (see fields.js). A dropped connection is retried with capped
// exponential backoff until close().

import { CONNECTION, CONNECTION_STATES } from "./fields.js";

export const WS_PATH = "/ws";

/** The hub's URL for a server address: "http://127.0.0.1:47823" → "ws://127.0.0.1:47823/ws". With an
 * empty address, the page's own origin. */
export function wsUrlFor(httpBase, { location = globalThis.location } = {}) {
  if (!httpBase) {
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    return `${scheme}://${location.host}${WS_PATH}`;
  }
  return `${httpBase.replace(/\/+$/, "").replace(/^http/, "ws")}${WS_PATH}`;
}

function notifyAll(listeners, value) {
  for (const listener of [...listeners]) {
    try {
      listener(value);
    } catch (error) {
      console.error("Snag: a hub listener failed", error);
    }
  }
}

/**
 * {connect(), close(), onEvent(fn) → unsubscribe, onConnection(fn) → unsubscribe, connection}.
 * connection: CONNECTION's "connecting" | "connected" | "disconnected"; "disconnected" until
 * connect(), like the store's initial state. The backoff starts over only once the connection proves
 * itself by staying up for `stableAfter` ms; a server that accepts (even sending its snapshot) and
 * then closes at once keeps backing off. Listeners may call connect() or close(): the
 * hub's own state is settled before they hear of a change, and checked again after. WebSocket and
 * the timers are injectable for tests.
 */
export function createHub(url, {
  WebSocket = globalThis.WebSocket,
  setTimer = (fn, ms) => setTimeout(fn, ms),
  clearTimer = (id) => clearTimeout(id),
  minDelay = 500,
  maxDelay = 10000,
  stableAfter = 5000,
} = {}) {
  const eventListeners = new Set();
  const connectionListeners = new Set();
  let socket = null;
  let retryTimer = null;
  let stableTimer = null;
  let attempt = 0;
  let stopped = true;
  let connection = CONNECTION.DISCONNECTED;

  function setConnection(state) {
    if (!CONNECTION_STATES.includes(state)) throw new Error(`Snag: unknown connection state ${state}`);
    if (state === connection) return;
    connection = state;
    notifyAll(connectionListeners, state);
  }

  function cancelRetry() {
    if (retryTimer !== null) {
      clearTimer(retryTimer);
      retryTimer = null;
    }
  }

  function cancelStable() {
    if (stableTimer !== null) {
      clearTimer(stableTimer);
      stableTimer = null;
    }
  }

  /** The connection proved itself: the next drop retries quickly again. */
  function proven() {
    attempt = 0;
    cancelStable();
  }

  function scheduleRetry() {
    if (!stopped) {
      const delay = Math.min(maxDelay, minDelay * 2 ** attempt);
      attempt += 1;
      retryTimer = setTimer(() => {
        retryTimer = null;
        if (!stopped && !socket) open();
      }, delay);
    }
    setConnection(CONNECTION.DISCONNECTED);
  }

  function open() {
    setConnection(CONNECTION.CONNECTING);
    // A listener may have closed the hub, or connected it already, while hearing "connecting".
    if (stopped || socket) return;
    let ws;
    try {
      ws = new WebSocket(url);
    } catch (error) {
      console.error("Snag: couldn't open the connection", error);
      scheduleRetry();
      return;
    }
    socket = ws;
    ws.onopen = () => {
      if (ws !== socket) return;
      cancelStable();
      stableTimer = setTimer(() => {
        stableTimer = null;
        if (ws === socket) proven();
      }, stableAfter);
      setConnection(CONNECTION.CONNECTED);
    };
    ws.onmessage = (message) => {
      if (ws !== socket) return;
      let event;
      try {
        event = JSON.parse(message.data);
      } catch {
        return;
      }
      if (event !== null && typeof event === "object" && typeof event.type === "string") {
        notifyAll(eventListeners, event);
      }
    };
    ws.onclose = () => {
      if (ws !== socket) return;
      socket = null;
      cancelStable();
      scheduleRetry();
    };
  }

  return {
    get connection() {
      return connection;
    },
    /** Start connecting; while waiting to retry, retry now. */
    connect() {
      stopped = false;
      if (socket) return;
      cancelRetry();
      open();
    },
    close() {
      stopped = true;
      cancelRetry();
      cancelStable();
      const ws = socket;
      socket = null;
      if (ws) ws.close();
      setConnection(CONNECTION.DISCONNECTED);
    },
    onEvent(listener) {
      eventListeners.add(listener);
      return () => eventListeners.delete(listener);
    },
    onConnection(listener) {
      connectionListeners.add(listener);
      return () => connectionListeners.delete(listener);
    },
  };
}
