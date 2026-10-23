// Transient messages ("Queued", "Couldn't cancel: …") in a polite live region.
//
//   const toasts = createToaster();  document.body.append(toasts.element);
//   toasts.show("Queued");  toasts.show(error.message, {kind: "error"});
//
// Errors stay twice as long. A toast being pointed at or holding focus stays until it is left, then gets
// its full time again. With more than `max`, the oldest info or success toast goes first; an error goes
// only when every toast is one. When a toast that holds focus closes, focus moves to the next toast (or
// the one before), else back to where it was before it entered the toasts. Timers are injectable for tests.

import { h } from "./el.js";

export function createToaster({
  duration = 4000, max = 3, setTimer = (fn, ms) => setTimeout(fn, ms), clearTimer = (id) => clearTimeout(id),
} = {}) {
  const element = h("div", { class: "toasts", role: "status", "aria-live": "polite" });
  const toasts = new Map(); // item -> {kind, ms, timer, hovered, focused, close}, oldest first
  let returnTo = null; // what had focus before it moved into the toasts
  const ours = new WeakSet(); // every toast's close button, even one already closed

  /** Where focus goes when item closes while holding it: the next toast's close button, else the one
   * before, else where focus was before the toasts. Found while item is still listed. */
  function focusAfter(item) {
    const items = [...toasts.keys()];
    const index = items.indexOf(item);
    const neighbour = items[index + 1] ?? items[index - 1] ?? null;
    return neighbour ? toasts.get(neighbour).close : returnTo;
  }

  function dismiss(item) {
    const toast = toasts.get(item);
    if (!toast) return;
    const doc = globalThis.document;
    const target = doc && item.contains(doc.activeElement) ? focusAfter(item) : null;
    clearTimer(toast.timer);
    toasts.delete(item);
    if (target?.isConnected && typeof target.focus === "function") target.focus();
    item.remove();
    if (!toasts.size) returnTo = null;
  }

  function start(item) {
    const toast = toasts.get(item);
    if (!toast || toast.hovered || toast.focused) return;
    clearTimer(toast.timer);
    toast.timer = setTimer(() => dismiss(item), toast.ms);
  }

  function hold(item, reason, on) {
    const toast = toasts.get(item);
    if (!toast) return;
    toast[reason] = on;
    if (toast.hovered || toast.focused) {
      clearTimer(toast.timer);
      toast.timer = null;
    } else {
      start(item);
    }
  }

  /** The toast to let go when there are too many: the oldest that isn't an error, else the oldest. */
  function evictable() {
    const items = [...toasts.keys()];
    return items.find((item) => toasts.get(item).kind !== "error") ?? items[0];
  }

  /** kind: "info" | "success" | "error". Returns a function that dismisses this toast. */
  function show(message, { kind = "info", timeout } = {}) {
    const close = h("button", {
      type: "button", class: "btn btn-ghost btn-sm btn-icon toast-close", "aria-label": "Dismiss",
      onClick: () => dismiss(item),
    }, "×");
    ours.add(close);
    const item = h("div", {
      class: ["toast", kind === "error" && "toast-danger", kind === "success" && "toast-success"],
      onMouseenter: () => hold(item, "hovered", true),
      onMouseleave: () => hold(item, "hovered", false),
      onFocusin: (event) => {
        const from = event.relatedTarget;
        if (from && !ours.has(from) && !element.contains(from)) returnTo = from;
        hold(item, "focused", true);
      },
      onFocusout: (event) => {
        if (!item.contains(event.relatedTarget ?? null)) hold(item, "focused", false);
      },
    },
    h("span", { class: "toast-text" }, message), close);
    element.append(item);
    toasts.set(item, {
      kind, ms: timeout ?? (kind === "error" ? duration * 2 : duration), timer: null, hovered: false, focused: false, close,
    });
    while (toasts.size > max) dismiss(evictable());
    start(item);
    return () => dismiss(item);
  }

  return {
    element,
    show,
    clear() {
      for (const item of [...toasts.keys()]) dismiss(item);
    },
  };
}
