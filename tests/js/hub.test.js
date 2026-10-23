import test from "node:test";
import assert from "node:assert/strict";

import { createHub, wsUrlFor, WS_PATH } from "../../ui/hub.js";

/** A WebSocket stand-in the test drives: open(), message(data), drop(). Like a browser's, close()
 * delivers onclose later, as its own task, not inside the call. */
class FakeSocket {
  static all = [];
  constructor(url) {
    this.url = url;
    this.closed = false;
    FakeSocket.all.push(this);
  }
  open() { this.onopen?.({}); }
  message(data) { this.onmessage?.({ data: typeof data === "string" ? data : JSON.stringify(data) }); }
  drop() { this.onerror?.({}); this.onclose?.({ code: 1006 }); }
  close() {
    if (this.closed) return;
    this.closed = true;
    setTimeout(() => this.onclose?.({ code: 1000 }), 0);
  }
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 5));
const live = () => FakeSocket.all.filter((socket) => !socket.closed);

/** Timers the test fires by hand. */
function fakeTimers() {
  const pending = new Map();
  let next = 1;
  return {
    setTimer: (fn, ms) => { const id = next++; pending.set(id, { fn, ms }); return id; },
    clearTimer: (id) => { pending.delete(id); },
    delays: () => [...pending.values()].map((timer) => timer.ms),
    fire() {
      const entries = [...pending.entries()];
      pending.clear();
      for (const [, timer] of entries) timer.fn();
    },
  };
}

function setup(options = {}) {
  FakeSocket.all = [];
  const timers = fakeTimers();
  const hub = createHub("ws://127.0.0.1:47823/ws", { WebSocket: FakeSocket, ...timers, ...options });
  const events = [];
  const states = [];
  hub.onEvent((event) => events.push(event));
  hub.onConnection((state) => states.push(state));
  return { hub, timers, events, states, socket: () => FakeSocket.all.at(-1) };
}

test("the WebSocket URL comes from the server's address", () => {
  assert.equal(WS_PATH, "/ws");
  assert.equal(wsUrlFor("http://127.0.0.1:47823"), "ws://127.0.0.1:47823/ws");
  assert.equal(wsUrlFor("http://127.0.0.1:47823/"), "ws://127.0.0.1:47823/ws");
  assert.equal(wsUrlFor("https://example.test"), "wss://example.test/ws");
  assert.equal(wsUrlFor("", { location: { protocol: "http:", host: "localhost:47823" } }), "ws://localhost:47823/ws");
});

test("the hub starts disconnected, like the store", async () => {
  const { initialState } = await import("../../ui/store.js");
  const { hub } = setup();
  assert.equal(hub.connection, "disconnected");
  assert.equal(initialState().connection, hub.connection);
});

test("connecting, then the snapshot and events are passed on in order", () => {
  const { hub, events, states, socket } = setup();
  assert.equal(hub.connection, "disconnected");
  hub.connect();
  assert.equal(socket().url, "ws://127.0.0.1:47823/ws");
  assert.deepEqual(states, ["connecting"]);
  socket().open();
  assert.deepEqual(states, ["connecting", "connected"]);
  assert.equal(hub.connection, "connected");
  socket().message({ type: "snapshot", jobs: [], queue: {}, status: {} });
  socket().message({ type: "job", job: { id: "a" } });
  assert.deepEqual(events.map((event) => event.type), ["snapshot", "job"]);
});

test("messages that aren't events are ignored", () => {
  const { hub, events, socket } = setup();
  hub.connect();
  socket().open();
  socket().message("not json {");
  socket().message("[1, 2]");
  socket().message({ no: "type" });
  socket().message("null");
  assert.deepEqual(events, []);
});

test("connect() twice keeps one socket", () => {
  const { hub } = setup();
  hub.connect();
  hub.connect();
  assert.equal(FakeSocket.all.length, 1);
});

test("a dropped connection reconnects with capped exponential backoff", () => {
  const { hub, timers, states, socket } = setup({ minDelay: 500, maxDelay: 4000 });
  hub.connect();
  const delays = [];
  for (let attempt = 0; attempt < 5; attempt += 1) {
    socket().drop();
    delays.push(...timers.delays());
    timers.fire();
  }
  assert.deepEqual(delays, [500, 1000, 2000, 4000, 4000]);
  assert.equal(FakeSocket.all.length, 6);
  assert.deepEqual(states.slice(0, 4), ["connecting", "disconnected", "connecting", "disconnected"]);
});

test("a server that sends the snapshot and then closes still backs off", () => {
  const { hub, timers, socket } = setup({ minDelay: 500, maxDelay: 4000, stableAfter: 5000 });
  hub.connect();
  const delays = [];
  for (let attempt = 0; attempt < 4; attempt += 1) {
    socket().open();
    socket().message({ type: "snapshot", jobs: [], queue: {}, status: {} });
    socket().drop();
    delays.push(...timers.delays());
    timers.fire();
  }
  assert.deepEqual(delays, [500, 1000, 2000, 4000]);
});

test("the backoff starts over once a connection with its snapshot stays up", () => {
  const { hub, timers, socket } = setup({ minDelay: 500, maxDelay: 4000, stableAfter: 5000 });
  hub.connect();
  socket().drop();
  timers.fire();
  socket().drop();
  timers.fire();
  socket().open();
  socket().message({ type: "snapshot", jobs: [], queue: {}, status: {} });
  assert.deepEqual(timers.delays(), [5000]);
  timers.fire();
  socket().drop();
  assert.deepEqual(timers.delays(), [500]);
});

test("a server that accepts and then closes at once still backs off", () => {
  const { hub, timers, socket } = setup({ minDelay: 500, maxDelay: 4000, stableAfter: 5000 });
  hub.connect();
  const delays = [];
  for (let attempt = 0; attempt < 4; attempt += 1) {
    socket().open();
    socket().drop();
    delays.push(...timers.delays());
    timers.fire();
  }
  assert.deepEqual(delays, [500, 1000, 2000, 4000]);
});

test("a connection that stays up long enough starts the backoff over, even without a message", () => {
  const { hub, timers, socket } = setup({ minDelay: 500, maxDelay: 4000, stableAfter: 5000 });
  hub.connect();
  socket().drop();
  timers.fire();
  socket().drop();
  timers.fire();
  socket().open();
  assert.deepEqual(timers.delays(), [5000]);
  timers.fire();
  socket().drop();
  assert.deepEqual(timers.delays(), [500]);
});

test("a WebSocket that can't even be created is retried too", (t) => {
  t.mock.method(console, "error", () => {});
  FakeSocket.all = [];
  const timers = fakeTimers();
  let tries = 0;
  class Throwing {
    constructor() {
      tries += 1;
      throw new Error("SecurityError");
    }
  }
  const hub = createHub("ws://x/ws", { WebSocket: Throwing, ...timers });
  hub.connect();
  assert.equal(hub.connection, "disconnected");
  assert.deepEqual(timers.delays(), [500]);
  timers.fire();
  assert.equal(tries, 2);
});

test("connect() while waiting to reconnect tries at once", () => {
  const { hub, timers, socket } = setup();
  hub.connect();
  socket().drop();
  assert.equal(timers.delays().length, 1);
  hub.connect();
  assert.equal(timers.delays().length, 0);
  assert.equal(FakeSocket.all.length, 2);
});

test("a listener that reconnects when disconnected leaves one socket", () => {
  const { hub, timers, socket } = setup();
  hub.onConnection((state) => { if (state === "disconnected") hub.connect(); });
  hub.connect();
  socket().drop();
  assert.equal(FakeSocket.all.length, 2, "connect() from the listener tried at once");
  assert.deepEqual(timers.delays(), [], "and the retry it replaced is gone");
  timers.fire();
  assert.equal(FakeSocket.all.length, 2);
  assert.equal(hub.connection, "connecting");
});

test("a listener that connects while connecting leaves one socket", () => {
  const { hub } = setup();
  let again = true;
  hub.onConnection((state) => {
    if (state === "connecting" && again) {
      again = false;
      hub.connect();
    }
  });
  hub.connect();
  assert.equal(FakeSocket.all.length, 1);
});

test("a listener that closes while connecting leaves no socket", () => {
  const { hub, timers } = setup();
  hub.onConnection((state) => { if (state === "connecting") hub.close(); });
  hub.connect();
  assert.deepEqual(live(), []);
  assert.equal(hub.connection, "disconnected");
  assert.deepEqual(timers.delays(), []);
});

test("a listener that closes when connected leaves no socket and no timers", () => {
  const { hub, timers, socket } = setup();
  hub.onConnection((state) => { if (state === "connected") hub.close(); });
  hub.connect();
  socket().open();
  assert.deepEqual(live(), []);
  assert.equal(hub.connection, "disconnected");
  assert.deepEqual(timers.delays(), []);
});

test("a listener that closes when disconnected cancels the retry", () => {
  const { hub, timers, socket } = setup();
  hub.onConnection((state) => { if (state === "disconnected") hub.close(); });
  hub.connect();
  socket().drop();
  assert.deepEqual(timers.delays(), []);
  assert.equal(FakeSocket.all.length, 1);
});

test("an event listener that closes the hub stops the rest of that socket's messages", () => {
  const { hub, events, socket } = setup();
  hub.onEvent(() => hub.close());
  hub.connect();
  socket().open();
  socket().message({ type: "snapshot", jobs: [], queue: {}, status: {} });
  socket().message({ type: "job", job: { id: "late" } });
  assert.deepEqual(events.map((event) => event.type), ["snapshot"]);
});

test("close() then connect(): the old socket's late close is ignored", async () => {
  const { hub, timers, states, socket } = setup();
  hub.connect();
  socket().open();
  const first = socket();
  hub.close();
  hub.connect();
  const second = socket();
  assert.notEqual(first, second);
  await settle();
  assert.equal(first.closed, true);
  assert.deepEqual(live(), [second]);
  assert.equal(hub.connection, "connecting");
  assert.deepEqual(timers.delays(), []);
  assert.deepEqual(states, ["connecting", "connected", "disconnected", "connecting"]);
});

test("close() stops for good and closes the socket", async () => {
  const { hub, timers, states, socket } = setup();
  hub.connect();
  socket().open();
  const first = socket();
  hub.close();
  assert.equal(first.closed, true);
  assert.equal(hub.connection, "disconnected");
  assert.equal(states.at(-1), "disconnected");
  assert.deepEqual(timers.delays(), []);
  await settle();
  assert.deepEqual(timers.delays(), [], "the socket's own close event doesn't schedule a retry");
  assert.equal(FakeSocket.all.length, 1);
});

test("close() while waiting cancels the retry", () => {
  const { hub, timers, socket } = setup();
  hub.connect();
  socket().drop();
  hub.close();
  assert.deepEqual(timers.delays(), []);
});

test("events from a socket that was replaced are ignored", () => {
  const { hub, timers, events, socket } = setup();
  hub.connect();
  const old = socket();
  old.drop();
  timers.fire();
  old.message({ type: "job", job: { id: "stale" } });
  old.onclose?.({ code: 1006 });
  assert.deepEqual(events, []);
  assert.equal(FakeSocket.all.length, 2);
  assert.deepEqual(timers.delays(), []);
});

test("listeners can unsubscribe, and one failing doesn't stop the others", (t) => {
  t.mock.method(console, "error", () => {});
  const { hub, events, socket } = setup();
  const seen = [];
  const off = hub.onEvent((event) => seen.push(event.type));
  hub.onEvent(() => { throw new Error("boom"); });
  hub.connect();
  socket().open();
  socket().message({ type: "queue", queue: {} });
  off();
  socket().message({ type: "status" });
  assert.deepEqual(seen, ["queue"]);
  assert.equal(events.length, 2);
});
