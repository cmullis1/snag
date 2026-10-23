// The fake DOM's own browser-like behaviours, so component tests can lean on them.
import "./fake-dom.js";
import test from "node:test";
import assert from "node:assert/strict";

import { h } from "../../ui/components/el.js";

test("events that bubble in a browser bubble here, until stopped; others don't", () => {
  const heard = [];
  const button = h("button", { type: "button", onClick: () => heard.push("button") });
  const row = h("li", { onClick: (event) => heard.push(`row:${event.target === button}`), onMouseenter: () => heard.push("enter") }, button);
  h("ul", { onClick: () => heard.push("list") }, row);
  button.click();
  button.dispatch("mouseenter");
  assert.deepEqual(heard, ["button", "row:true", "list"]);
  heard.length = 0;
  const stopper = h("span", { onClick: (event) => event.stopPropagation() });
  const outer = h("div", { onClick: () => heard.push("outer") }, stopper);
  stopper.click();
  assert.equal(outer.childNodes.length, 1);
  assert.deepEqual(heard, []);
});

test("clicking a checkbox toggles it and fires change; a radio only checks", () => {
  const changes = [];
  const box = h("input", { type: "checkbox", onChange: () => changes.push(box.checked) });
  box.click();
  box.click();
  assert.deepEqual(changes, [true, false]);
  const radio = h("input", { type: "radio", onChange: () => changes.push(`radio:${radio.checked}`) });
  radio.click();
  radio.click();
  assert.deepEqual(changes.slice(2), ["radio:true", "radio:true"]);
  box.disabled = true;
  box.click();
  assert.equal(changes.length, 4, "a disabled control ignores clicks");
});

test("hidden and disabled follow their attributes both ways", () => {
  const node = h("p", { hidden: true });
  assert.equal(node.hidden, true);
  assert.equal(node.getAttribute("hidden"), "");
  node.hidden = false;
  assert.equal(node.hasAttribute("hidden"), false);
  node.setAttribute("disabled", "");
  assert.equal(node.disabled, true);
  node.removeAttribute("disabled");
  assert.equal(node.disabled, false);
});

test("disabling the focused control drops focus to the page, as a browser does; aria-disabled keeps it", () => {
  const button = h("button", { type: "button" });
  const other = h("button", { type: "button" });
  h("div", null, button, other);
  button.focus();
  other.disabled = true;
  assert.equal(document.activeElement, button, "disabling another control leaves focus alone");
  button.setAttribute("aria-disabled", "true");
  assert.equal(document.activeElement, button, "aria-disabled doesn't move focus");
  button.disabled = true;
  assert.equal(document.activeElement, null);
  button.disabled = false;
  button.focus();
  button.setAttribute("disabled", "");
  assert.equal(document.activeElement, null, "the attribute does the same");
  button.removeAttribute("disabled");
  button.focus();
  button.disabled = false;
  assert.equal(document.activeElement, button, "enabling it again moves nothing");
});

test("a click whose default is prevented doesn't toggle a checkbox or fire input and change", () => {
  const events = [];
  const box = h("input", {
    type: "checkbox",
    onClick: (event) => { events.push("click"); event.preventDefault(); },
    onInput: () => events.push("input"),
    onChange: () => events.push("change"),
  });
  box.click();
  assert.equal(box.checked, false);
  assert.deepEqual(events, ["click"]);
});
