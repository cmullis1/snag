// Toasts on a fake DOM: which one goes when there are too many, pausing while pointed at or focused,
// and where focus goes when a focused toast closes.
import "./fake-dom.js";
import test from "node:test";
import assert from "node:assert/strict";

import { byClass } from "./fake-dom.js";
import { h } from "../../ui/components/el.js";
import { createToaster } from "../../ui/components/toast.js";

function make(options = {}) {
  const timers = new Map();
  let next = 0;
  const toaster = createToaster({
    max: 3, ...options,
    setTimer: (fn, ms) => { next += 1; timers.set(next, { fn, ms }); return next; },
    clearTimer: (id) => timers.delete(id),
  });
  const fireAll = () => {
    for (const [id, timer] of [...timers]) {
      timers.delete(id);
      timer.fn();
    }
  };
  const texts = () => byClass(toaster.element, "toast-text").map((node) => node.textContent);
  const item = (text) => byClass(toaster.element, "toast").find((node) => node.textContent.startsWith(text));
  return { toaster, timers, fireAll, texts, item };
}

test("with too many toasts, info goes before an older error", () => {
  const { toaster, texts } = make();
  toaster.show("Couldn't cancel", { kind: "error" });
  toaster.show("Queued", { kind: "success" });
  toaster.show("Reinstalling ffmpeg…");
  toaster.show("Queued 3 videos", { kind: "success" });
  assert.deepEqual(texts(), ["Couldn't cancel", "Reinstalling ffmpeg…", "Queued 3 videos"]);
  toaster.show("Second error", { kind: "error" });
  toaster.show("Third error", { kind: "error" });
  assert.deepEqual(texts(), ["Couldn't cancel", "Second error", "Third error"]);
  toaster.show("Fourth error", { kind: "error" });
  assert.deepEqual(texts(), ["Second error", "Third error", "Fourth error"], "only errors left: the oldest goes");
});

test("a toast pointed at or focused stays until it is left", () => {
  const { toaster, fireAll, texts, item, timers } = make();
  toaster.show("Couldn't open the folder", { kind: "error" });
  toaster.show("Queued");
  item("Couldn't").dispatch("mouseenter");
  item("Queued").dispatch("focusin", { relatedTarget: null });
  fireAll();
  assert.deepEqual(texts(), ["Couldn't open the folder", "Queued"]);
  item("Couldn't").dispatch("mouseleave");
  assert.deepEqual([...timers.values()].map((timer) => timer.ms), [8000], "a full error time again");
  item("Queued").dispatch("focusout", { relatedTarget: null });
  fireAll();
  assert.deepEqual(texts(), []);
});

test("closing a focused toast moves focus to the next toast, then back to where it came from", () => {
  const { toaster, item } = make();
  const outside = h("button", { type: "button" }, "Download");
  toaster.show("First", { kind: "error" });
  toaster.show("Second", { kind: "error" });
  const first = item("First");
  const second = item("Second");
  const [firstClose, secondClose] = byClass(toaster.element, "toast-close");
  firstClose.focus();
  first.dispatch("focusin", { relatedTarget: outside });
  firstClose.click();
  assert.equal(document.activeElement, secondClose, "the next toast's close button");
  second.dispatch("focusin", { relatedTarget: firstClose });
  secondClose.click();
  assert.equal(document.activeElement, outside, "back where it was before the toasts");
});

test("dismissing a toast that doesn't hold focus leaves focus alone", () => {
  const { toaster } = make();
  const outside = h("button", { type: "button" }, "Download");
  outside.focus();
  const dismiss = toaster.show("Queued");
  dismiss();
  assert.equal(document.activeElement, outside);
});

test("closing the middle of three focused toasts moves focus to the next one, not the first", () => {
  const { toaster, item } = make();
  toaster.show("A", { kind: "error" });
  toaster.show("B", { kind: "error" });
  toaster.show("C", { kind: "error" });
  const [, closeB, closeC] = byClass(toaster.element, "toast-close");
  closeB.focus();
  item("B").dispatch("focusin", { relatedTarget: null });
  closeB.click();
  assert.equal(document.activeElement, closeC);
});
