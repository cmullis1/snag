import test from "node:test";
import assert from "node:assert/strict";

import { h, svg, replace, safeUrl, setText, thumbnailUrl } from "../../ui/components/el.js";

// Just enough of the DOM for el.js.
class FakeText {
  constructor(data) { this.nodeType = 3; this.data = data; }
}

/** A style declaration that records what was set, the way el.js sets it. */
class FakeStyle {
  constructor() { this.properties = new Map(); this.cssText = ""; }
  setProperty(name, value) { this.properties.set(name, value); }
  getPropertyValue(name) { return this.properties.get(name) ?? ""; }
}

class FakeElement {
  constructor(tag, namespace = null) {
    this.nodeType = 1;
    this.tagName = tag.toUpperCase();
    this.namespaceURI = namespace;
    this.attributes = new Map();
    this.childNodes = [];
    this.style = new FakeStyle();
    this.dataset = {};
    this.listeners = {};
    this.className = "";
  }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  getAttribute(name) { return this.attributes.has(name) ? this.attributes.get(name) : null; }
  addEventListener(type, fn) { (this.listeners[type] ??= []).push(fn); }
  append(...nodes) { this.childNodes.push(...nodes); }
  replaceChildren(...nodes) { this.childNodes = [...nodes]; }
  get textContent() {
    return this.childNodes.map((node) => (node.nodeType === 3 ? node.data : node.textContent)).join("");
  }
}

globalThis.document = {
  createElement: (tag) => new FakeElement(tag),
  createElementNS: (namespace, tag) => new FakeElement(tag, namespace),
  createTextNode: (data) => new FakeText(data),
};

test("an element with text and child elements", () => {
  const node = h("p", null, "Saved to ", h("strong", null, "Videos"), " · ", 3, " files");
  assert.equal(node.tagName, "P");
  assert.equal(node.textContent, "Saved to Videos · 3 files");
  assert.equal(node.childNodes[1].tagName, "STRONG");
});

test("children may be nested arrays; null, undefined and booleans are skipped", () => {
  const node = h("ul", null, [h("li", null, "a"), [null, h("li", null, "b")]], false, undefined, true, "");
  assert.deepEqual(node.childNodes.filter((child) => child.nodeType === 1).map((child) => child.textContent), ["a", "b"]);
  assert.equal(node.childNodes.length, 3, "the empty string is kept as a text node");
});

test("text is always text, never markup", () => {
  const node = h("div", null, "<img src=x onerror=alert(1)>");
  assert.equal(node.childNodes[0].nodeType, 3);
  assert.equal(node.childNodes[0].data, "<img src=x onerror=alert(1)>");
});

test("innerHTML and outerHTML are refused", () => {
  assert.throws(() => h("div", { innerHTML: "<b>x</b>" }), /innerHTML/);
  assert.throws(() => h("div", { outerHTML: "<b>x</b>" }), /outerHTML/);
});

test("class accepts text or a list with gaps", () => {
  assert.equal(h("div", { class: "row" }).className, "row");
  assert.equal(h("div", { class: ["row", false, null, "is-active"] }).className, "row is-active");
  assert.equal(h("div", { className: "x" }).className, "x");
});

test("attributes: text, true as empty, false and null left out", () => {
  const node = h("button", { type: "button", "aria-pressed": "true", hidden: true, disabled: false, title: null, tabindex: 0 });
  assert.equal(node.getAttribute("type"), "button");
  assert.equal(node.getAttribute("aria-pressed"), "true");
  assert.equal(node.getAttribute("hidden"), "");
  assert.equal(node.getAttribute("disabled"), null);
  assert.equal(node.getAttribute("title"), null);
  assert.equal(node.getAttribute("tabindex"), "0");
});

test("form state is set as properties", () => {
  const input = h("input", { type: "checkbox", checked: true, value: "x", indeterminate: false });
  assert.equal(input.checked, true);
  assert.equal(input.value, "x");
  assert.equal(input.indeterminate, false);
  assert.equal(input.getAttribute("checked"), null);
});

test("event handlers are added as listeners", () => {
  const clicked = [];
  const node = h("button", { onClick: () => clicked.push(1), onKeydown: () => clicked.push(2) });
  assert.equal(node.listeners.click.length, 1);
  assert.equal(node.listeners.keydown.length, 1);
  node.listeners.click[0]();
  assert.deepEqual(clicked, [1]);
});

test("a handler left out (null or undefined) adds nothing", () => {
  const node = h("button", { onClick: null, onKeydown: undefined });
  assert.deepEqual(node.listeners, {});
  assert.equal(node.attributes.size, 0);
});

test("on… props that aren't onEvent handlers with a function are refused, never attributes", () => {
  assert.throws(() => h("img", { onerror: "alert(1)" }), /onerror/);
  assert.throws(() => h("img", { ONERROR: "alert(1)" }), /ONERROR/);
  assert.throws(() => h("img", { onError: "alert(1)" }), /onError/);
  assert.throws(() => h("div", { onclick: () => {} }), /onclick/, "lower-case on… is an attribute name, not a handler");
  assert.throws(() => h("div", { on: () => {} }), /on/);
  assert.throws(() => svg("svg", { onload: "alert(1)" }), /onload/);
  assert.throws(() => h("a", { OnClick: () => {} }), /OnClick/);
});

test("srcdoc is refused in any case", () => {
  assert.throws(() => h("iframe", { srcdoc: "<script>alert(1)</script>" }), /srcdoc/);
  assert.throws(() => h("iframe", { srcDoc: "<b>x</b>" }), /srcDoc/);
  assert.throws(() => h("div", { innerhtml: "<b>x</b>" }), /innerhtml/);
});

test("URL attributes take http, https, blob, relative URLs and fragments", () => {
  assert.equal(h("a", { href: "https://www.youtube.com/watch?v=x" }).getAttribute("href"), "https://www.youtube.com/watch?v=x");
  assert.equal(h("a", { href: "HTTP://example.test/" }).getAttribute("href"), "HTTP://example.test/");
  assert.equal(h("a", { href: "#queue" }).getAttribute("href"), "#queue");
  assert.equal(h("a", { href: "/api/status" }).getAttribute("href"), "/api/status");
  assert.equal(h("a", { href: "history.html?x=1:2" }).getAttribute("href"), "history.html?x=1:2");
  assert.equal(h("a", { href: "//example.test/x" }).getAttribute("href"), "//example.test/x");
  assert.equal(h("a", { href: "" }).getAttribute("href"), "");
  assert.equal(h("img", { src: "blob:http://127.0.0.1/abc" }).getAttribute("src"), "blob:http://127.0.0.1/abc");
  assert.equal(h("img", { src: "data:image/png;base64,AAAA" }).getAttribute("src"), "data:image/png;base64,AAAA");
  assert.equal(h("form", { action: "/api/jobs" }).getAttribute("action"), "/api/jobs");
  assert.equal(h("video", { poster: "https://i.ytimg.com/x.jpg" }).getAttribute("poster"), "https://i.ytimg.com/x.jpg");
  assert.equal(h("object", { data: "https://example.test/x.pdf" }).getAttribute("data"), "https://example.test/x.pdf");
  assert.equal(h("div", { data: "javascript:anything" }).getAttribute("data"), "javascript:anything", "data is a URL only on <object>");
  assert.equal(svg("use", { href: "#icon-play" }).getAttribute("href"), "#icon-play");
});

test("URL attributes refuse script and other schemes, whatever the case or whitespace", () => {
  const bad = [
    "javascript:alert(1)", "JavaScript:alert(1)", "  javascript:alert(1)", "\tjava\nscript:alert(1)",
    "\u0000javascript:alert(1)", "\u0001 javascript:alert(1)", "java\rscript:alert(1)", "vbscript:x",
    "data:text/html,<script>alert(1)</script>", "file:///C:/Windows", "chrome-extension://abc/x.html",
  ];
  for (const url of bad) {
    assert.throws(() => h("a", { href: url }), /href/, `href ${JSON.stringify(url)}`);
    assert.throws(() => h("img", { src: url }), /src/, `src ${JSON.stringify(url)}`);
  }
  assert.throws(() => h("a", { href: "data:image/png;base64,AAAA" }), /href/, "data:image/ only for <img src>");
  assert.throws(() => h("iframe", { src: "data:image/svg+xml,<svg/>" }), /src/);
  assert.throws(() => h("form", { action: "javascript:alert(1)" }), /action/);
  assert.throws(() => h("button", { formAction: "javascript:alert(1)" }), /formAction/);
  assert.throws(() => h("button", { formaction: "javascript:alert(1)" }), /formaction/);
  assert.throws(() => h("video", { poster: "javascript:alert(1)" }), /poster/);
  assert.throws(() => h("object", { data: "javascript:alert(1)" }), /data/);
  assert.throws(() => h("a", { HREF: "javascript:alert(1)" }), /HREF/);
  assert.throws(() => svg("a", { href: "javascript:alert(1)" }), /href/);
  assert.throws(() => svg("a", { "xlink:href": "javascript:alert(1)" }), /xlink:href/);
  assert.throws(() => h("a", { href: { toString: () => "javascript:alert(1)" } }), /href/);
});

test("style text is set as cssText", () => {
  const node = h("div", { style: "color: red; --value: 42%" });
  assert.equal(node.style.cssText, "color: red; --value: 42%");
  assert.equal(node.getAttribute("style"), null);
});

test("style objects go through setProperty: custom properties, kebab or camel case", () => {
  const node = h("div", { style: { width: "42%", "--fill": "0.42", fontWeight: 600, "margin-top": "4px", color: null } });
  assert.equal(node.style.getPropertyValue("width"), "42%");
  assert.equal(node.style.getPropertyValue("--fill"), "0.42");
  assert.equal(node.style.getPropertyValue("font-weight"), "600");
  assert.equal(node.style.getPropertyValue("margin-top"), "4px");
  assert.equal(node.style.properties.has("color"), false);
});

test("style names that aren't plain camel case: cssFloat and vendor prefixes", () => {
  const node = h("div", { style: { cssFloat: "left", webkitLineClamp: 2, MozAppearance: "none", msOverflowStyle: "none" } });
  assert.equal(node.style.getPropertyValue("float"), "left");
  assert.equal(node.style.getPropertyValue("-webkit-line-clamp"), "2");
  assert.equal(node.style.getPropertyValue("-moz-appearance"), "none");
  assert.equal(node.style.getPropertyValue("-ms-overflow-style"), "none");
  assert.equal(node.style.properties.has("css-float"), false);
});

test("dataset", () => {
  const node = h("div", { dataset: { jobId: "abc" } });
  assert.equal(node.dataset.jobId, "abc");
});

test("svg elements use the SVG namespace and plain attributes", () => {
  const icon = svg("svg", { viewBox: "0 0 16 16", class: "icon", "aria-hidden": "true" }, svg("path", { d: "M1 1h14" }));
  assert.equal(icon.namespaceURI, "http://www.w3.org/2000/svg");
  assert.equal(icon.getAttribute("class"), "icon");
  assert.equal(icon.getAttribute("viewBox"), "0 0 16 16");
  assert.equal(icon.childNodes[0].getAttribute("d"), "M1 1h14");
});

test("SVG animation elements are refused: they could set a link's href to script", () => {
  for (const tag of ["animate", "set", "SET", "Animate"]) {
    assert.throws(() => svg(tag, { attributeName: "href", to: "javascript:alert(1)" }), /animate|set/i, tag);
  }
  assert.throws(() => svg("a", null, svg("animate", { attributeName: "href", values: "javascript:alert(1)" })), /animate/);
  assert.equal(svg("path", { d: "M0 0" }).tagName, "PATH");
});

test("setText changes a node's text only when it differs", () => {
  const node = h("p", null, "Same");
  let changes = 0;
  const original = node.replaceChildren.bind(node);
  node.replaceChildren = (...nodes) => {
    changes += 1;
    original(...nodes);
  };
  assert.equal(setText(node, "Same"), false);
  assert.equal(changes, 0);
  assert.equal(setText(node, "New"), true);
  assert.equal(node.textContent, "New");
  assert.equal(changes, 1);
  assert.equal(setText(node, "New"), false);
  assert.equal(setText(node, null), true);
  assert.equal(node.textContent, "");
  assert.equal(setText(node, undefined), false, "null and undefined are both no text");
  assert.equal(setText(node, 42), true);
  assert.equal(node.textContent, "42");
});

test("replace swaps a container's children", () => {
  const list = h("ul", null, h("li", null, "old"));
  replace(list, [h("li", null, "new")], null, "tail");
  assert.equal(list.textContent, "newtail");
  replace(list);
  assert.equal(list.childNodes.length, 0);
});

test("safeUrl gives back a URL el.js would set, and null instead of throwing", () => {
  assert.equal(safeUrl("img", "src", "https://i.ytimg.com/vi/x/hq.jpg"), "https://i.ytimg.com/vi/x/hq.jpg");
  assert.equal(safeUrl("img", "src", "data:image/png;base64,AAAA"), "data:image/png;base64,AAAA");
  assert.equal(safeUrl("IMG", "SRC", "blob:http://127.0.0.1/abc"), "blob:http://127.0.0.1/abc");
  assert.equal(safeUrl("a", "href", "#history"), "#history");
  for (const url of ["javascript:alert(1)", "\tjava\nscript:alert(1)", "data:text/html,<b>", "file:///C:/x", "vbscript:x"]) {
    assert.equal(safeUrl("img", "src", url), null, JSON.stringify(url));
  }
  assert.equal(safeUrl("a", "href", "data:image/png;base64,AAAA"), null, "data:image/ only for <img src>");
  assert.equal(safeUrl("img", "src", null), null);
  assert.equal(safeUrl("img", "src", undefined), null);
  assert.equal(safeUrl("img", "alt", "javascript:x"), "javascript:x", "not a URL attribute: any text");
  for (const url of ["https://x.test/a.jpg", "javascript:alert(1)", "data:image/gif;base64,R0"]) {
    const allowed = safeUrl("img", "src", url) !== null;
    if (allowed) assert.equal(h("img", { src: url }).getAttribute("src"), url);
    else assert.throws(() => h("img", { src: url }), /src/);
  }
});

test("thumbnailUrl takes only https: and data:image/ URLs (the pages' img-src), else null", () => {
  assert.equal(thumbnailUrl("https://i.ytimg.com/vi/abc/hq.jpg"), "https://i.ytimg.com/vi/abc/hq.jpg");
  assert.equal(thumbnailUrl("HTTPS://i.ytimg.com/a.jpg"), "HTTPS://i.ytimg.com/a.jpg");
  assert.equal(thumbnailUrl(" https://x.test/a.jpg\n"), "https://x.test/a.jpg", "what a browser ignores is trimmed");
  assert.equal(thumbnailUrl("data:image/png;base64,iVBOR"), "data:image/png;base64,iVBOR");
  for (const url of ["http://i.ytimg.com/a.jpg", "blob:https://x.test/1", "/thumb.jpg", "thumb.jpg", "//x.test/a.jpg",
    "data:text/html,<b>x</b>", "javascript:alert(1)", "file:///C:/a.jpg", "", null, undefined, 42]) {
    assert.equal(thumbnailUrl(url), null, String(url));
  }
});
