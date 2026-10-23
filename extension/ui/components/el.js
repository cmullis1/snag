// A tiny DOM builder. Text always becomes text nodes and markup is never parsed, so data from the
// server can never become script:
//   - innerHTML, outerHTML and srcdoc are refused;
//   - an on… prop must be an onEvent handler given a function (it becomes a listener, never an
//     attribute); anything else named on… is refused;
//   - URL attributes (href, src, action, formaction, xlink:href, poster, and data on <object>) take
//     only http:, https:, blob:, relative URLs and #fragments, plus data:image/ for <img src>;
//   - style is set through the style declaration (cssText or setProperty), never as markup;
//   - SVG <animate> and <set> are refused: they can change another attribute (a link's href) to any
//     text later, past the URL rule.
//   safeUrl(tag, attr, url) applies the same URL rule without throwing (null when refused);
//   thumbnailUrl(url) is the stricter rule for thumbnails from data: https: and data:image/ only.
//   setText(node, text) changes a node's text only when it differs, so live regions and focus are left
//   alone by updates that change nothing.
// Refusals throw, so a mistake shows up at once instead of becoming a hole.
//
//   h("button", {class: ["btn", primary && "btn-primary"], type: "button", onClick}, "Download")

const SVG_NS = "http://www.w3.org/2000/svg";
// Set as properties (live form state), not attributes.
const PROPERTIES = new Set(["value", "checked", "selected", "indeterminate"]);
// Compared in lower case: HTML attribute names ignore case.
const FORBIDDEN = new Set(["innerhtml", "outerhtml", "srcdoc"]);
const URL_ATTRIBUTES = new Set(["href", "src", "action", "formaction", "xlink:href", "poster"]);
const SAFE_SCHEMES = new Set(["http", "https", "blob"]);
const HANDLER = /^on[A-Z]/;
// The parts of a URL a browser ignores: C0 controls and spaces around it, tabs and newlines inside.
const URL_EDGES = /^[\u0000- ]+|[\u0000- ]+$/g;
const URL_TAB_OR_NEWLINE = /[\t\n\r]/g;
const SCHEME = /^([a-z][a-z0-9+.-]*):/i;
// SVG elements that animate other attributes (compared in lower case).
const FORBIDDEN_SVG = new Set(["animate", "set"]);

function appendChildren(parent, children) {
  for (const child of children) {
    if (child === null || child === undefined || typeof child === "boolean") continue;
    if (Array.isArray(child)) appendChildren(parent, child);
    else if (typeof child === "object" && "nodeType" in child) parent.append(child);
    else parent.append(document.createTextNode(String(child)));
  }
}

// A style property's CSS name: "fontWeight" -> "font-weight", "cssFloat" -> "float", "webkitLineClamp" ->
// "-webkit-line-clamp"; custom properties and names already in kebab case are kept.
function kebab(name) {
  if (name.startsWith("--")) return name;
  if (name === "cssFloat") return "float";
  return name.replace(/^(webkit|moz|ms|o)(?=[A-Z])/, "-$1").replace(/[A-Z]/g, (letter) => `-${letter.toLowerCase()}`);
}

function setStyle(node, style) {
  if (typeof style === "string") {
    node.style.cssText = style;
    return;
  }
  for (const [name, value] of Object.entries(style)) {
    if (value === null || value === undefined || value === false) continue;
    node.style.setProperty(kebab(name), String(value));
  }
}

function classText(value) {
  return Array.isArray(value) ? value.filter(Boolean).join(" ") : String(value);
}

function isUrlAttribute(tag, name) {
  return URL_ATTRIBUTES.has(name) || (name === "data" && tag === "object");
}

/** Whether a URL attribute's text is one el.js lets through (see the top of the file). */
function isSafeUrl(tag, name, text) {
  const url = text.replace(URL_EDGES, "").replace(URL_TAB_OR_NEWLINE, "");
  const scheme = SCHEME.exec(url)?.[1].toLowerCase();
  if (scheme === undefined) return true; // relative, or a #fragment
  if (SAFE_SCHEMES.has(scheme)) return true;
  return scheme === "data" && tag === "img" && name === "src" && /^data:image\//i.test(url);
}

function applyProps(node, props, isSvg) {
  const tag = String(node.tagName ?? "").toLowerCase();
  for (const [key, value] of Object.entries(props ?? {})) {
    const name = key.toLowerCase();
    if (FORBIDDEN.has(name)) throw new Error(`el.js never sets ${key}; pass children instead`);
    if (name.startsWith("on")) {
      if (value === null || value === undefined) {
        if (HANDLER.test(key)) continue;
      } else if (HANDLER.test(key) && typeof value === "function") {
        node.addEventListener(key.slice(2).toLowerCase(), value);
        continue;
      }
      throw new Error(`el.js only takes on… props as onEvent handlers with a function, not ${key}`);
    }
    if (key === "class" || key === "className") {
      if (value === null || value === undefined || value === false) continue;
      if (isSvg) node.setAttribute("class", classText(value));
      else node.className = classText(value);
    } else if (key === "style") {
      if (value) setStyle(node, value);
    } else if (key === "dataset") {
      for (const [field, item] of Object.entries(value ?? {})) node.dataset[field] = String(item);
    } else if (PROPERTIES.has(key) && !isSvg) {
      node[key] = value;
    } else if (value === null || value === undefined || value === false) {
      continue;
    } else {
      const text = value === true ? "" : String(value);
      if (isUrlAttribute(tag, name) && !isSafeUrl(tag, name, text)) {
        throw new Error(`el.js refused ${key} on <${tag}>: only http, https, blob and relative URLs`);
      }
      node.setAttribute(key, text);
    }
  }
}

/** A URL as el.js would set it on tag's attr, or null where el.js would refuse it (it throws instead):
 * check URLs that come from data (a server's thumbnail) with this and leave out what it refuses.
 * Text for attributes that aren't URLs comes back as it is. */
export function safeUrl(tag, attr, url) {
  if (url === null || url === undefined) return null;
  const text = String(url);
  const tagName = String(tag).toLowerCase();
  const name = String(attr).toLowerCase();
  if (!isUrlAttribute(tagName, name)) return text;
  return isSafeUrl(tagName, name, text) ? text : null;
}

/** A thumbnail URL from data (a lookup's thumbnail) that the pages may show, or null: https: and
 * data:image/ only, as the page's Content-Security-Policy allows (img-src 'self' https: data:), so a
 * refused one is left out instead of loading nothing (or over plain http). Outer spaces and the tabs
 * and newlines a browser ignores are dropped. */
export function thumbnailUrl(url) {
  if (typeof url !== "string") return null;
  const text = url.replace(URL_EDGES, "").replace(URL_TAB_OR_NEWLINE, "");
  return /^https:/i.test(text) || /^data:image\//i.test(text) ? text : null;
}

/** An HTML element. props: attributes, plus class (text or list), style (text, or an object of
 * properties: custom properties like {"--value": "42%"}, kebab or camel case), dataset, on<Event>
 * handlers, and value/checked/selected/indeterminate as properties. */
export function h(tag, props, ...children) {
  const node = document.createElement(tag);
  applyProps(node, props, false);
  appendChildren(node, children);
  return node;
}

/** An SVG element (icons). */
export function svg(tag, props, ...children) {
  if (FORBIDDEN_SVG.has(String(tag).toLowerCase())) throw new Error(`el.js never makes SVG <${tag}>: it can animate an href`);
  const node = document.createElementNS(SVG_NS, tag);
  applyProps(node, props, true);
  appendChildren(node, children);
  return node;
}

/** Set a node's text, leaving it alone when it already has exactly that text (null and undefined are
 * no text). Returns whether it changed. */
export function setText(node, text) {
  const next = text === null || text === undefined ? "" : String(text);
  if (node.textContent === next) return false;
  replace(node, next);
  return true;
}

/** Replace a container's children (strings become text). */
export function replace(parent, ...children) {
  parent.replaceChildren();
  appendChildren(parent, children);
  return parent;
}
