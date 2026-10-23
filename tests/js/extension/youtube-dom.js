// A tiny model of the parts of the DOM the YouTube content script touches: elements in a tree,
// attributes, text, click listeners, shadow roots, and a querySelector that answers from a table the
// test fills in (selector → element), since the script only ever looks up its candidate selectors.
// Elements have a layout box the test sets (rect), and the Popover API when the document is made with
// {popover: true}. The document notes where each child-list change happened (takeChanges()). Also a
// window (scroll/resize listeners, text direction), a manual clock and a MutationObserver the test
// triggers by hand, which only hears about changes under the node it observes.

export class FakeNode {
  constructor(ownerDocument, tagName = "#node") {
    this.ownerDocument = ownerDocument;
    this.tagName = tagName.toUpperCase();
    this.childNodes = [];
    this.parentNode = null;
    this.attributes = new Map();
    this.listeners = new Map();
    this.text = "";
    this.shadow = null;
    this.style = {};
  }

  /** The layout box: the test sets node.rect. */
  getBoundingClientRect() {
    return { top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0, ...this.rect };
  }

  get id() {
    return this.attributes.get("id") ?? "";
  }

  set id(value) {
    this.attributes.set("id", String(value));
  }

  get className() {
    return this.attributes.get("class") ?? "";
  }

  set className(value) {
    this.attributes.set("class", String(value));
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
  }

  getAttribute(name) {
    return this.attributes.has(name) ? this.attributes.get(name) : null;
  }

  removeAttribute(name) {
    this.attributes.delete(name);
  }

  hasAttribute(name) {
    return this.attributes.has(name);
  }

  get textContent() {
    return this.text + this.childNodes.map((child) => child.textContent).join("");
  }

  set textContent(value) {
    for (const child of [...this.childNodes]) this.removeChild(child);
    this.text = String(value);
  }

  get nextSibling() {
    if (!this.parentNode) return null;
    const siblings = this.parentNode.childNodes;
    return siblings[siblings.indexOf(this) + 1] ?? null;
  }

  /** The node or its nearest ancestor with that tag name (the only selectors the script passes). */
  closest(selector) {
    if (!/^[a-z][a-z0-9-]*$/i.test(selector)) throw new Error(`the fake DOM can't match ${selector}`);
    for (let node = this; node; node = node.parentNode) if (node.tagName === selector.toUpperCase()) return node;
    return null;
  }

  /** True when node is this node or inside it (not across a shadow root's boundary). */
  contains(node) {
    for (let at = node; at; at = at.parentNode) if (at === this) return true;
    return false;
  }

  get isConnected() {
    let node = this;
    while (node.parentNode) node = node.parentNode;
    return node === this.ownerDocument.documentElement || node.isShadowOf?.isConnected === true;
  }

  appendChild(child) {
    return this.insertBefore(child, null);
  }

  insertBefore(child, reference) {
    if (reference !== null && reference.parentNode !== this) throw new Error("reference isn't a child");
    child.parentNode?.removeChild(child);
    const index = reference === null ? this.childNodes.length : this.childNodes.indexOf(reference);
    this.childNodes.splice(index, 0, child);
    child.parentNode = this;
    this.ownerDocument.changed(this);
    return child;
  }

  append(...children) {
    for (const child of children) this.appendChild(child);
  }

  removeChild(child) {
    const index = this.childNodes.indexOf(child);
    if (index < 0) throw new Error("not a child");
    this.childNodes.splice(index, 1);
    child.parentNode = null;
    this.ownerDocument.changed(this);
    return child;
  }

  remove() {
    this.parentNode?.removeChild(this);
  }

  attachShadow({ mode }) {
    if (this.shadow) throw new Error("already has a shadow root");
    this.shadow = new FakeNode(this.ownerDocument, "#shadow-root");
    this.shadow.mode = mode;
    this.shadow.isShadowOf = this;
    return this.shadow;
  }

  addEventListener(type, listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(listener);
  }

  removeEventListener(type, listener) {
    this.listeners.set(type, (this.listeners.get(type) ?? []).filter((l) => l !== listener));
  }

  dispatch(type, event = {}) {
    return (this.listeners.get(type) ?? []).map((listener) => listener({ type, target: this, ...event }));
  }

  /** Every descendant (not inside shadow roots), depth first. */
  *descendants() {
    for (const child of this.childNodes) {
      yield child;
      yield* child.descendants();
    }
  }

  /** Every descendant, shadow roots included. */
  *deepDescendants() {
    for (const child of [...(this.shadow ? [this.shadow] : []), ...this.childNodes]) {
      yield child;
      yield* child.deepDescendants();
    }
  }
}

/** The Popover API on an element, as Chrome has it: showing needs the element in the page. */
function addPopover(node) {
  node.popoverOpen = false;
  node.showPopover = function showPopover() {
    if (this.getAttribute("popover") === null) throw new Error("NotSupportedError: not a popover");
    if (!this.isConnected) throw new Error("InvalidStateError: not connected");
    if (this.popoverOpen) throw new Error("InvalidStateError: already showing");
    this.popoverOpen = true;
  };
  node.hidePopover = function hidePopover() {
    this.popoverOpen = false;
  };
  node.matches = function matches(selector) {
    if (selector === ":popover-open") return this.popoverOpen;
    throw new Error(`the fake DOM can't match ${selector}`);
  };
}

export class FakeDocument {
  constructor({ popover = false } = {}) {
    this.popover = popover;
    this.changes = []; // the nodes whose children changed, since takeChanges() last ran
    this.selectors = new Map();
    this.queries = [];
    this.listeners = new Map();
    this.documentElement = new FakeNode(this, "html");
    this.documentElement.clientWidth = 1280;
    this.body = this.createElement("body");
    this.documentElement.appendChild(this.body);
  }

  changed(node) {
    this.changes.push(node);
  }

  /** The nodes whose children changed since the last call, in order, and forgets them. */
  takeChanges() {
    const changes = this.changes;
    this.changes = [];
    return changes;
  }

  createElement(tagName) {
    const node = new FakeNode(this, tagName);
    if (this.popover) addPopover(node);
    return node;
  }

  createElementNS(namespace, tagName) {
    const node = new FakeNode(this, tagName);
    node.namespaceURI = namespace;
    return node;
  }

  /** The table querySelector answers from; an element not in the document still answers, as a
   * stale reference would. */
  answer(selector, element) {
    this.selectors.set(selector, element);
  }

  querySelector(selector) {
    this.queries.push(selector);
    return this.selectors.get(selector) ?? null;
  }

  getElementById(id) {
    for (const node of this.documentElement.descendants()) if (node.id === id) return node;
    return null;
  }

  addEventListener(type, listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(listener);
  }

  removeEventListener(type, listener) {
    this.listeners.set(type, (this.listeners.get(type) ?? []).filter((l) => l !== listener));
  }

  dispatch(type) {
    for (const listener of this.listeners.get(type) ?? []) listener({ type });
  }
}

/** The page's window: scroll and resize listeners, and the text direction getComputedStyle reports. */
export class FakeWindow {
  constructor({ direction = "ltr" } = {}) {
    this.direction = direction;
    this.listeners = new Map();
  }

  addEventListener(type, listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(listener);
  }

  removeEventListener(type, listener) {
    this.listeners.set(type, (this.listeners.get(type) ?? []).filter((l) => l !== listener));
  }

  count(type) {
    return (this.listeners.get(type) ?? []).length;
  }

  dispatch(type) {
    for (const listener of this.listeners.get(type) ?? []) listener({ type });
  }

  getComputedStyle() {
    return { direction: this.direction };
  }
}

/** A page location the test can move: set href to navigate. */
export class FakeLocation {
  constructor(href) {
    this.href = href;
  }

  get pathname() {
    return new URL(this.href).pathname;
  }

  get search() {
    return new URL(this.href).search;
  }
}

/** setTimeout/clearTimeout on a clock the test advances. */
export class FakeClock {
  constructor() {
    this.now = 0;
    this.timers = new Map();
    this.nextId = 1;
    this.setTimeout = (callback, delay = 0) => {
      const id = this.nextId++;
      this.timers.set(id, { at: this.now + delay, callback });
      return id;
    };
    this.clearTimeout = (id) => {
      this.timers.delete(id);
    };
  }

  /** Moves time forward, running each timer that comes due, in order. */
  advance(ms) {
    const end = this.now + ms;
    for (;;) {
      const due = [...this.timers.entries()].filter(([, t]) => t.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
      if (!due) break;
      const [id, timer] = due;
      this.timers.delete(id);
      this.now = timer.at;
      timer.callback();
    }
    this.now = end;
  }
}

/** MutationObserver stand-in: the test calls trigger(node) to report a change to node's children; only
 * an observer whose target is node, or contains it when observing the subtree, hears it, and none
 * after disconnect(). */
export function fakeObserverClass() {
  const instances = [];
  class FakeMutationObserver {
    constructor(callback) {
      this.callback = callback;
      this.observing = null;
      instances.push(this);
    }

    observe(target, options) {
      this.observing = { target, options };
    }

    disconnect() {
      this.observing = null;
    }

    trigger(node) {
      if (!node) throw new Error("trigger(node): say where the change happened");
      if (!this.observing) return;
      const { target, options } = this.observing;
      if (!(options && options.subtree ? target.contains(node) : target === node)) return;
      this.callback([{ type: "childList", target: node }], this);
    }
  }
  return { FakeMutationObserver, instances };
}
