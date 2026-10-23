// The first-run screen that stands in for the tabs while Snag installs yt-dlp, ffmpeg and Deno, or
// after that failed: a title, what's happening, one progress bar per tool, and Retry
// (POST /api/setup/retry). What it shows comes from setup-model.js setupScreen().
//
//   const screen = createSetupScreen({api, toast});  screen.update(setupScreen(status, progress));

import { setInert } from "./busy.js";
import { h, replace } from "./el.js";
import { progressBar, setProgress } from "./job-row.js";

export function createSetupScreen({ api, toast = null }) {
  const title = h("h1", { id: "setup-title", class: "setup-title", tabindex: "-1" });
  const message = h("p", { class: "setup-message" });
  const tools = h("ul", { class: "rows setup-tools", role: "list" });
  const rows = new Map(); // tool -> {item, text, bar}
  const error = h("p", { class: "field-error", role: "alert" });
  const retry = h("button", { type: "button", class: "btn btn-primary", onClick: retrySetup }, "Retry");
  // Hidden while it holds nothing to show (no Retry, no error), so the card ends at the tools.
  const foot = h("div", { class: "setup-foot cluster" }, retry, error);
  const element = h("section", { class: "setup card", "aria-labelledby": "setup-title" },
    title, message, tools, foot);
  let busy = false;
  let canRetry = false;

  function rowFor(tool) {
    let row = rows.get(tool.name);
    if (!row) {
      const text = h("span", { class: "setup-tool-text small" });
      const bar = progressBar(`Installing ${tool.label}`);
      const item = h("li", { class: "row setup-tool" },
        h("div", { class: "row-main" },
          h("div", { class: "spread" }, h("span", { class: "row-title" }, tool.label), text),
          bar));
      row = { item, text, bar };
      rows.set(tool.name, row);
      tools.append(item);
    }
    return row;
  }

  async function retrySetup() {
    if (busy) return;
    busy = true;
    replace(error);
    refreshButton();
    try {
      await api.retrySetup();
    } catch (failure) {
      const text = failure?.message || "Snag couldn't start setting up again";
      replace(error, text);
      toast?.show(text, { kind: "error" });
    } finally {
      busy = false;
      refreshButton();
    }
  }

  function refreshButton() {
    retry.hidden = !canRetry;
    setInert(retry, busy);
    replace(retry, busy ? "Retrying…" : "Retry");
    foot.hidden = !canRetry && !error.textContent;
  }

  return {
    element,
    /** Focus the title (when the screen takes over from the tabs). */
    focus() {
      title.focus();
    },
    /** view: setupScreen()'s result (null hides nothing here; the app hides the screen). */
    update(view) {
      if (!view) return;
      replace(title, view.title);
      replace(message, view.message);
      for (const tool of view.tools) {
        const row = rowFor(tool);
        replace(row.text, tool.text);
        row.text.classList.toggle("danger-text", tool.tone === "danger");
        row.text.classList.toggle("success-text", tool.tone === "done");
        row.text.classList.toggle("muted", !tool.tone);
        setProgress(row.bar, { value: tool.value ?? 0, tone: null, indeterminate: tool.indeterminate });
        row.bar.classList.toggle("is-done", tool.tone === "done");
        row.bar.classList.toggle("is-danger", tool.tone === "danger");
      }
      canRetry = view.canRetry;
      refreshButton();
    },
  };
}
