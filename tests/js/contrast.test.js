// WCAG 2.1 AA contrast for base.css's colour tokens, light and dark: text ≥ 4.5:1, the outlines and
// fills that identify controls and state ≥ 3:1. The ratios are computed here from the tokens as
// written, so a token change that breaks a pair fails this test.

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const CSS = readFileSync(new URL("../../ui/base.css", import.meta.url), "utf8");

/** The custom properties declared in a block's body. */
function declarations(body) {
  const tokens = {};
  for (const match of body.matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)) tokens[match[1]] = match[2].trim();
  return tokens;
}

/** The body of the first `{ … }` block after `from`, braces balanced. */
function blockAfter(text, from) {
  const open = text.indexOf("{", from);
  let depth = 0;
  for (let index = open; index < text.length; index += 1) {
    if (text[index] === "{") depth += 1;
    else if (text[index] === "}" && (depth -= 1) === 0) return text.slice(open + 1, index);
  }
  throw new Error("unbalanced braces in base.css");
}

const lightRoot = blockAfter(CSS, CSS.search(/^:root\s*\{/m));
const darkMedia = blockAfter(CSS, CSS.indexOf("@media (prefers-color-scheme: dark)"));
const darkRoot = blockAfter(darkMedia, darkMedia.indexOf(":root"));
const THEMES = {
  light: declarations(lightRoot),
  dark: { ...declarations(lightRoot), ...declarations(darkRoot) },
};

/** A token's colour, following var(--other) references. */
function color(theme, name, seen = new Set()) {
  const value = THEMES[theme][name];
  assert.ok(value, `${theme}: ${name} is defined`);
  assert.ok(!seen.has(name), `${theme}: ${name} doesn't refer to itself`);
  const reference = /^var\((--[\w-]+)\)$/.exec(value);
  if (reference) return color(theme, reference[1], new Set([...seen, name]));
  assert.match(value, /^#[0-9a-f]{6}$/i, `${theme}: ${name} is a #rrggbb colour`);
  return value;
}

function luminance(hex) {
  const channel = (offset) => {
    const value = parseInt(hex.slice(offset, offset + 2), 16) / 255;
    return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * channel(1) + 0.7152 * channel(3) + 0.0722 * channel(5);
}

export function contrast(a, b) {
  const [light, dark] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (light + 0.05) / (dark + 0.05);
}

const SURFACES = ["--bg", "--surface", "--surface-sunken", "--surface-hover"];
const TEXT = 4.5;
const NON_TEXT = 3;

// [foreground, backgrounds, minimum, what it is]
const PAIRS = [
  ["--text", SURFACES, TEXT, "body text"],
  ["--text-muted", SURFACES, TEXT, "secondary text"],
  ["--text-faint", SURFACES, TEXT, "de-emphasised text and placeholders"],
  ["--accent-text", ["--bg", "--surface", "--accent-soft"], TEXT, "links, ghost buttons, the selected segment"],
  ["--on-accent", ["--accent", "--accent-hover", "--accent-active"], TEXT, "primary button text"],
  ["--danger", ["--surface", "--danger-soft"], TEXT, "danger text and badges"],
  ["--success", ["--surface", "--success-soft"], TEXT, "success text and badges"],
  ["--warning", ["--surface", "--warning-soft"], TEXT, "warning text and badges"],
  ["--text", ["--accent-soft"], TEXT, "accent notes"],
  ["--control-border", ["--bg", "--surface"], NON_TEXT, "input, select and button outlines"],
  ["--accent", ["--surface-sunken"], NON_TEXT, "the selected segment's outline against the switch"],
  ["--accent", ["--progress-track"], NON_TEXT, "progress bar"],
  ["--success", ["--progress-track"], NON_TEXT, "finished progress bar"],
  ["--danger", ["--progress-track"], NON_TEXT, "failed progress bar"],
  ["--progress-paused", ["--progress-track"], NON_TEXT, "paused progress bar"],
  ["--focus", ["--bg", "--surface"], NON_TEXT, "focus ring"],
];

for (const theme of Object.keys(THEMES)) {
  test(`${theme} theme meets WCAG AA contrast`, () => {
    const failures = [];
    for (const [foreground, backgrounds, minimum, what] of PAIRS) {
      for (const background of backgrounds) {
        const ratio = contrast(color(theme, foreground), color(theme, background));
        if (ratio < minimum) failures.push(`${foreground} on ${background} (${what}): ${ratio.toFixed(2)} < ${minimum}`);
      }
    }
    assert.deepEqual(failures, []);
  });
}

test("the WCAG ratio is computed correctly", () => {
  assert.equal(contrast("#000000", "#ffffff").toFixed(2), "21.00");
  assert.equal(contrast("#ffffff", "#ffffff").toFixed(2), "1.00");
  assert.equal(contrast("#767676", "#ffffff").toFixed(2), "4.54");
});

/** The declarations of the rule whose selector list is exactly `selector`. */
function rule(selector) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&").replace(/\s+/g, "\\s*");
  const match = new RegExp(`(^|[}/])\\s*${escaped}\\s*\\{([^}]*)\\}`, "m").exec(CSS);
  assert.ok(match, `base.css has a rule for ${selector}`);
  return match[2];
}

test("controls are outlined with --control-border", () => {
  assert.match(rule(".btn"), /border:[^;]*var\(--control-border\)/);
  assert.match(rule(".input,\n.select,\n.textarea"), /border:[^;]*var\(--control-border\)/);
  assert.match(rule(".segmented"), /border:[^;]*var\(--control-border\)/);
});

test("the selected segment has an accent outline, and a paused bar its own colour", () => {
  const selected = rule('.segmented > button[aria-checked="true"],\n.segmented > button[aria-pressed="true"]');
  assert.match(selected, /border-color:\s*var\(--accent\)/);
  assert.match(selected, /background:\s*var\(--accent-soft\)/);
  assert.match(selected, /color:\s*var\(--accent-text\)/);
  assert.match(rule(".progress.is-paused .progress-bar"), /background:\s*var\(--progress-paused\)/);
});

test("buttons and segments wrap between words at 360px, never mid-word", () => {
  for (const selector of [".btn", ".segmented > button"]) {
    const body = rule(selector);
    assert.match(body, /min-width:\s*0/, `${selector} can shrink`);
    // "anywhere" would let a button shrink to a letter's width and break "Download" mid-word.
    assert.match(body, /overflow-wrap:\s*break-word/, `${selector} breaks a word only when it can't fit`);
    assert.doesNotMatch(body, /overflow-wrap:\s*anywhere/, `${selector} isn't shrunk mid-word`);
    assert.doesNotMatch(body, /white-space:\s*nowrap/, `${selector} wraps`);
  }
});

test("spread rows shrink their text, not their buttons", () => {
  const text = rule(".spread > *");
  assert.match(text, /min-width:\s*0/);
  assert.match(text, /overflow-wrap:\s*anywhere/, "long text (paths, titles) wraps anywhere");
  assert.match(rule(".spread > .btn,\n.spread > button,\n.row-end"), /flex:\s*none/);
});
