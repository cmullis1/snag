import test from "node:test";
import assert from "node:assert/strict";

import { connectionNote, pageTitle } from "../../web/shell-model.js";

test("no note while connected, or while connecting the first time", () => {
  assert.equal(connectionNote({ connection: "connected", everConnected: true, failedOnce: false }), null);
  assert.equal(connectionNote({ connection: "connecting", everConnected: false, failedOnce: false }), null);
});

test("Snag stopped after the page connected: reconnecting", () => {
  assert.equal(connectionNote({ connection: "connecting", everConnected: true, failedOnce: true }),
    "Lost touch with Snag. Reconnecting…");
  assert.equal(connectionNote({ connection: "disconnected", everConnected: true, failedOnce: true }),
    "Lost touch with Snag. Reconnecting…");
});

test("never reached: Snag isn't running", () => {
  assert.equal(connectionNote({ connection: "disconnected", everConnected: false, failedOnce: true }),
    "Snag isn't running. It starts with Windows, and you can also start it from the Start menu.");
  assert.equal(connectionNote({ connection: "connecting", everConnected: false, failedOnce: true }),
    "Snag isn't running. It starts with Windows, and you can also start it from the Start menu.");
});

test("each route names the page", () => {
  assert.equal(pageTitle("download"), "Snag");
  assert.equal(pageTitle("history"), "History · Snag");
  assert.equal(pageTitle("settings"), "Settings · Snag");
  assert.equal(pageTitle("playlist"), "Choose videos · Snag");
  assert.equal(pageTitle("setup"), "Setting up · Snag");
});
