// Just enough of the DOM to build the UI components in Node and poke at them: elements made by el.js,
// their attributes, classes, children, text and event listeners. Not a browser: layout, CSS and focus
// order are checked in a real one. Import it before a component module so `document` exists.
//
// Browser behaviours it copies:
//   - taking the focused element (or an ancestor) out of its parent, even to insert it somewhere else
//     at once, loses focus (document.activeElement goes back to null, the page);
//   - every element counts the changes to its own children in `mutations` (what a live region
//     announces), so a test can tell that an update left an element alone;
//   - events that bubble in a browser (click, input, change, keydown, keyup, focusin, focusout, submit)
//     bubble up the parents here too, until stopPropagation(); others (focus, mouseenter, …) don't;
//   - click() on a checkbox toggles it (on a radio, checks it) and then fires click, input and change;
//   - disabling the focused element loses focus too (the page has it again); aria-disabled doesn't;
//   - hidden and disabled are kept in step with their attributes;
//   - a click whose default is prevented leaves a checkbox as it was and fires no input or change.
// What it doesn't do: CSS (so `hidden` is only the property and its ancestors, see isHidden), layout,
// selectors (querySelector), focus moving by itself (Tab order, focus on click, focus/blur events: a
// test dispatches focusin or focusout itself), form submission, and the document as a parent: elements
// are never "in the page" beyond their own tree (isConnected is always true).

const BUBBLES = new Set(["click", "input", "change", "keydown", "keyup", "focusin", "focusout", "submit"]);

/** A node is leaving its parent: if focus is inside it, the page loses focus, as in a browser. */
function blurInside(node) {
  const active = globalThis.document?.activeElement;
  if (!active || node.nodeType !== 1) return;
  for (let current = active; current; current = current.parentNode) {
    if (current === node) {
      globalThis.document.activeElement = null;
      return;
    }
  }
}

class FakeText {
  constructor(data) {
    this.nodeType = 3;
    this.data = String(data);
    this.parentNode = null;
  }

  get textContent() {
    return this.data;
  }

  remove() {
    this.parentNode?._detach(this);
  }
}

class FakeStyle {
  constructor() {
    this.properties = new Map();
    this.cssText = "";
  }

  setProperty(name, value) {
    this.properties.set(name, String(value));
  }

  getPropertyValue(name) {
    return this.properties.get(name) ?? "";
  }
}

class FakeClassList {
  constructor(element) {
    this.element = element;
  }

  get names() {
    return this.element.className.split(/\s+/).filter(Boolean);
  }

  set names(list) {
    this.element.className = list.join(" ");
  }

  add(...names) {
    this.names = [...new Set([...this.names, ...names])];
  }

  remove(...names) {
    this.names = this.names.filter((name) => !names.includes(name));
  }

  contains(name) {
    return this.names.includes(name);
  }

  toggle(name, force) {
    const on = force === undefined ? !this.contains(name) : Boolean(force);
    if (on) this.add(name);
    else this.remove(name);
    return on;
  }
}

export class FakeElement {
  constructor(tag, namespace = null) {
    this.nodeType = 1;
    this.tagName = tag.toUpperCase();
    this.namespaceURI = namespace;
    this.attributes = new Map();
    this.childNodes = [];
    this.parentNode = null;
    this.style = new FakeStyle();
    this.dataset = {};
    this.listeners = {};
    this.className = "";
    this.classList = new FakeClassList(this);
    this.value = "";
    this.checked = false;
    this.flags = { hidden: false, disabled: false };
    this.id = "";
    this.mutations = 0;
  }

  // hidden and disabled: boolean properties reflected as attributes.
  get hidden() {
    return this.flags.hidden;
  }

  set hidden(on) {
    this.flags.hidden = Boolean(on);
  }

  get disabled() {
    return this.flags.disabled;
  }

  set disabled(on) {
    this.flags.disabled = Boolean(on);
    this._loseFocusIfDisabled();
  }

  _loseFocusIfDisabled() {
    if (this.flags.disabled && globalThis.document?.activeElement === this) globalThis.document.activeElement = null;
  }

  setAttribute(name, value) {
    const text = String(value);
    if (name === "id") this.id = text;
    else if (name === "hidden" || name === "disabled") {
      this.flags[name] = true;
      this._loseFocusIfDisabled();
    } else if (name === "value") this.value = text;
    if (name !== "hidden" && name !== "disabled") this.attributes.set(name, text);
  }

  getAttribute(name) {
    if (name === "id") return this.id || null;
    if (name === "hidden" || name === "disabled") return this.flags[name] ? "" : null;
    return this.attributes.has(name) ? this.attributes.get(name) : null;
  }

  hasAttribute(name) {
    return this.getAttribute(name) !== null;
  }

  removeAttribute(name) {
    if (name === "hidden" || name === "disabled") this.flags[name] = false;
    this.attributes.delete(name);
  }

  addEventListener(type, fn) {
    (this.listeners[type] ??= []).push(fn);
  }

  removeEventListener(type, fn) {
    this.listeners[type] = (this.listeners[type] ?? []).filter((item) => item !== fn);
  }

  /** Fire an event at this element: its listeners, then its parents' when the event bubbles in a
   * browser (or fields.bubbles says so). Returns the event. */
  dispatch(type, fields = {}) {
    let stopped = false;
    const event = {
      type, target: this, currentTarget: this, defaultPrevented: false, bubbles: BUBBLES.has(type),
      preventDefault() { this.defaultPrevented = true; }, stopPropagation() { stopped = true; }, ...fields,
    };
    for (let node = this; node; node = event.bubbles && !stopped ? node.parentNode : null) {
      event.currentTarget = node;
      for (const fn of [...(node.listeners?.[type] ?? [])]) fn(event);
    }
    return event;
  }

  click() {
    if (this.disabled) return;
    const type = this.tagName === "INPUT" ? this.attributes.get("type") : null;
    if (type === "checkbox" || type === "radio") {
      const before = this.checked;
      this.checked = type === "radio" ? true : !this.checked;
      if (this.dispatch("click").defaultPrevented) {
        this.checked = before;
        return;
      }
      this.dispatch("input");
      this.dispatch("change");
      return;
    }
    this.dispatch("click");
  }

  focus() {
    globalThis.document.activeElement = this;
  }

  blur() {}

  get isConnected() {
    return true;
  }

  get children() {
    return this.childNodes.filter((node) => node.nodeType === 1);
  }

  get firstChild() {
    return this.childNodes[0] ?? null;
  }

  get firstElementChild() {
    return this.children[0] ?? null;
  }

  _sibling(step) {
    if (!this.parentNode) return null;
    const siblings = this.parentNode.children;
    return siblings[siblings.indexOf(this) + step] ?? null;
  }

  get previousElementSibling() {
    return this._sibling(-1);
  }

  get nextElementSibling() {
    return this._sibling(1);
  }

  _detach(node) {
    blurInside(node);
    this.childNodes = this.childNodes.filter((child) => child !== node);
    node.parentNode = null;
    this.mutations += 1;
  }

  _adopt(node) {
    if (node.parentNode) node.parentNode._detach(node);
    node.parentNode = this;
  }

  append(...nodes) {
    for (const node of nodes) {
      const child = typeof node === "object" ? node : new FakeText(node);
      this._adopt(child);
      this.childNodes.push(child);
      this.mutations += 1;
    }
  }

  prepend(...nodes) {
    const children = nodes.map((node) => (typeof node === "object" ? node : new FakeText(node)));
    for (const child of children) this._adopt(child);
    this.childNodes = [...children, ...this.childNodes];
    this.mutations += children.length;
  }

  insertBefore(node, reference) {
    this._adopt(node);
    const index = reference ? this.childNodes.indexOf(reference) : -1;
    if (index < 0) this.childNodes.push(node);
    else this.childNodes.splice(index, 0, node);
    this.mutations += 1;
    return node;
  }

  replaceChildren(...nodes) {
    for (const child of this.childNodes) {
      blurInside(child);
      child.parentNode = null;
    }
    if (this.childNodes.length) this.mutations += 1;
    this.childNodes = [];
    this.append(...nodes);
  }

  remove() {
    this.parentNode?._detach(this);
  }

  contains(node) {
    for (let current = node; current; current = current.parentNode) if (current === this) return true;
    return false;
  }

  get textContent() {
    return this.childNodes.map((node) => node.textContent).join("");
  }

  set textContent(text) {
    this.replaceChildren(String(text));
  }
}

/** Every element under root (root included) that passes test, in document order. */
export function findAll(root, test) {
  const found = [];
  const visit = (node) => {
    if (node.nodeType !== 1) return;
    if (test(node)) found.push(node);
    for (const child of node.childNodes) visit(child);
  };
  visit(root);
  return found;
}

export function find(root, test) {
  return findAll(root, test)[0] ?? null;
}

/** Elements by class name. */
export const byClass = (root, name) => findAll(root, (node) => node.classList.contains(name));

/** The first button (or element of tag) whose text is exactly text, ignoring outer spaces. */
export function byText(root, text, tag = null) {
  return find(root, (node) => (!tag || node.tagName === tag.toUpperCase()) && node.textContent.trim() === text);
}

/** Whether node or one of its ancestors is hidden. */
export function isHidden(node) {
  for (let current = node; current; current = current.parentNode) if (current.hidden) return true;
  return false;
}

export function installFakeDom() {
  globalThis.document ??= {
    createElement: (tag) => new FakeElement(tag),
    createElementNS: (namespace, tag) => new FakeElement(tag, namespace),
    createTextNode: (data) => new FakeText(data),
    activeElement: null,
    addEventListener() {},
    removeEventListener() {},
  };
  return globalThis.document;
}

installFakeDom();
