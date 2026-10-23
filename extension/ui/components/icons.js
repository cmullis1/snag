// Small line icons (16 px, currentColor), drawn with el.js's svg(). Always decorative: the button
// carrying one has its own aria-label.

import { svg } from "./el.js";

const PATHS = {
  folder: ["M2 4.5A1.5 1.5 0 0 1 3.5 3h2.6l1.5 1.5h4.9A1.5 1.5 0 0 1 14 6v5.5a1.5 1.5 0 0 1-1.5 1.5h-9A1.5 1.5 0 0 1 2 11.5z"],
  download: ["M8 2.5v8", "M4.5 7 8 10.5 11.5 7", "M3 13.5h10"],
  more: ["M3.5 8h.01", "M8 8h.01", "M12.5 8h.01"],
  back: ["M10 3.5 5.5 8l4.5 4.5"],
  search: ["M7 12a5 5 0 1 0 0-10 5 5 0 0 0 0 10z", "M10.6 10.6 14 14"],
  minus: ["M3.5 8h9"],
  plus: ["M8 3.5v9", "M3.5 8h9"],
};

/** An icon by name ("folder", "download", "more", "back", "search", "minus", "plus"). */
export function icon(name, { className = "" } = {}) {
  const strokeWidth = name === "more" ? "2.6" : null;
  return svg("svg", { class: ["icon", className], viewBox: "0 0 16 16", "aria-hidden": "true", focusable: "false" },
    PATHS[name].map((d) => svg("path", { d, "stroke-width": strokeWidth })));
}
