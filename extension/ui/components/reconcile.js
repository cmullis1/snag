// Put a list's rows in order without disturbing the ones already in place: rows that left go first,
// then a row is moved only when the row before it isn't the one that should be. Moving an element
// takes it out of the page for a moment, which drops keyboard focus, so a list that only gains, loses
// or updates rows leaves the focused one alone; when the focused row itself has to move, focus goes
// back to it.
//
//   placeChildren(list, rows.map((row) => row.element));
//   keepFocus(container, () => { ...moves rows between lists... });
//
// When the focused row leaves, or the focused button is hidden, focus has nowhere to stay: the
// caller finds where it was (focusWithin) before the change and, after it, gives focus to a row
// that stays (neighbourOf) or to a sensible control (hiddenWithin tells when its button went).

/** Run work (which may move elements around inside container); if that took focus away from an element
 * still inside container, give it back. Returns what work returns. */
export function keepFocus(container, work) {
  const doc = globalThis.document;
  const active = doc?.activeElement ?? null;
  const result = work();
  if (active && doc.activeElement !== active && container.contains(active) && typeof active.focus === "function") {
    active.focus({ preventScroll: true });
  }
  return result;
}

/** Make parent's element children exactly `nodes`, in order, moving as few as it can. */
export function placeChildren(parent, nodes) {
  keepFocus(parent, () => {
    const wanted = new Set(nodes);
    for (const child of [...parent.children]) if (!wanted.has(child)) child.remove();
    let previous = null;
    for (const node of nodes) {
      const inPlace = node.parentNode === parent && node.previousElementSibling === previous;
      if (!inPlace) parent.insertBefore(node, previous ? previous.nextElementSibling : parent.firstElementChild);
      previous = node;
    }
  });
}

/** The focused element when it is inside container, else null. */
export function focusWithin(container) {
  const active = globalThis.document?.activeElement ?? null;
  return active && active !== container && container.contains(active) ? active : null;
}

/** Whether node, or an ancestor of it up to and including root, is hidden. */
export function hiddenWithin(node, root) {
  for (let current = node; current; current = current.parentNode) {
    if (current.hidden) return true;
    if (current === root) return false;
  }
  return false;
}

/** The row to give focus to when the focused element (held) is in a row that is leaving: the next row
 * in `order` (the rows as shown before the change) that stays, else the nearest one before it that
 * stays. null when held isn't in a leaving row, or no row stays. */
export function neighbourOf(order, held, stays) {
  const index = order.findIndex((node) => node.contains(held));
  if (index < 0 || stays(order[index])) return null;
  for (let i = index + 1; i < order.length; i += 1) if (stays(order[i])) return order[i];
  for (let i = index - 1; i >= 0; i -= 1) if (stays(order[i])) return order[i];
  return null;
}
