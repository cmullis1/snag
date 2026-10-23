// The queue banner: paused (with Resume), held for an update or setup (Retry setup after it failed),
// throttled until a time.
// Banners come from queue-model.js queueBanners(); this only draws them.
//
//   const banner = createBanner({onResume: async () => ..., onRetrySetup: async () => ..., toast});
//   banner.update(banners);
//
// toast: createToaster() (optional); an action that fails says why there (else in the console).

import { busy } from "./busy.js";
import { h, replace } from "./el.js";

const TONES = { paused: "note-accent", held: "note-accent", setup: "note-accent", throttled: "note-warning" };

export function createBanner({ onResume = null, onRetrySetup = null, toast = null } = {}) {
  const element = h("div", { class: "queue-banners", "aria-live": "polite" });
  const handlers = { resume: ["Resume", onResume], retry_setup: ["Retry setup", onRetrySetup] };
  let shown = "";

  function actionButton(action) {
    const [label, run] = handlers[action] ?? [];
    if (!run) return null;
    const button = h("button", {
      type: "button", class: "btn btn-sm",
      onClick: async () => {
        try {
          await busy(button, run);
        } catch (error) {
          if (toast) toast.show(error?.message || `Couldn't ${label.toLowerCase()}`, { kind: "error" });
          else console.error("Snag:", error);
        }
      },
    }, label);
    return button;
  }

  return {
    element,
    update(banners) {
      const key = JSON.stringify(banners);
      if (key === shown) return;
      shown = key;
      element.hidden = banners.length === 0;
      replace(element, banners.map((banner) => h("div", { class: ["note", "queue-banner", TONES[banner.kind]] },
        h("span", { class: "grow" }, banner.text),
        banner.action ? actionButton(banner.action) : null)));
    },
  };
}
