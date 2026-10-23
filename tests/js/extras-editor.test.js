// The extras editor on a fake DOM: its error messages are announced once, not on every keystroke.
import "./fake-dom.js";
import test from "node:test";
import assert from "node:assert/strict";

import { byClass, find } from "./fake-dom.js";
import { createExtrasEditor } from "../../ui/components/extras-editor.js";

const input = (editor, label) => find(editor.element, (node) => node.tagName === "INPUT" && node.getAttribute("aria-label") === label);
const errors = (editor) => byClass(editor.element, "field-error");

function type(node, text) {
  node.value = text;
  node.dispatch("input");
}

test("errors found while typing are polite live regions, not alerts", () => {
  const editor = createExtrasEditor({ extras: null });
  const [sponsor, start, end] = errors(editor);
  for (const node of [sponsor, start, end]) {
    assert.equal(node.getAttribute("role"), null);
    assert.equal(node.getAttribute("aria-live"), "polite");
  }
});

test("a trim error stays put while typing keeps it the same, and changes when it changes", () => {
  const editor = createExtrasEditor({ extras: null });
  const start = input(editor, "Trim from");
  const startError = errors(editor)[1];
  type(start, "x");
  start.dispatch("change");
  assert.equal(startError.textContent, "Use a time like 90, 1:30 or 1:02:03.5");
  const announced = startError.mutations;
  for (const text of ["xy", "xyz", "1:7x"]) type(start, text);
  assert.equal(startError.mutations, announced, "the same message isn't rewritten");
  type(start, "1:30");
  assert.equal(startError.textContent, "");
});

test("the server's message is shown once, and cleared by the next change", () => {
  const editor = createExtrasEditor({ extras: null });
  const serverError = errors(editor).at(-1);
  editor.showServerError("SponsorBlock can't remove Highlight");
  const shown = serverError.mutations;
  editor.showServerError("SponsorBlock can't remove Highlight");
  assert.equal(serverError.mutations, shown);
  type(input(editor, "Trim to"), "2:00");
  assert.equal(serverError.textContent, "");
});

test("the trim boxes take at most the server's 32 characters", () => {
  const editor = createExtrasEditor({ extras: null });
  for (const label of ["Trim from", "Trim to"]) assert.equal(input(editor, label).getAttribute("maxlength"), "32");
});

test("the server's message is a quiet copy: whoever asked the server announces it", () => {
  const editor = createExtrasEditor({ extras: null });
  editor.showServerError("SponsorBlock can't remove Highlight");
  const serverError = errors(editor).at(-1);
  assert.equal(serverError.textContent, "SponsorBlock can't remove Highlight");
  assert.equal(serverError.getAttribute("role"), null);
  assert.equal(serverError.getAttribute("aria-live"), null);
});
