// "Save to Videos\Snag  Change": the download's folder, changed with the native folder picker
// (POST /api/pick-folder, through the pickFolder dependency).
//
//   const line = createFolderLine({folder, pickFolder: async (initial) => path | null, onChange(path)});
//   line.setFolder(path); line.getFolder();

import { busy, isInert } from "./busy.js";
import { h, replace } from "./el.js";

/** The last two parts of a folder ("Videos\Snag"); the full path goes in the tooltip. */
export function folderLabel(path) {
  if (!path) return "";
  const text = String(path);
  const parts = text.split(/[\\/]+/).filter(Boolean);
  if (parts.length <= 1) return /^[A-Za-z]:$/.test(parts[0] ?? "") ? `${parts[0]}\\` : text;
  const separator = text.includes("\\") ? "\\" : "/";
  return parts.slice(-2).join(separator);
}

export function createFolderLine({ folder = "", pickFolder, onChange = () => {}, label = "Save to" }) {
  let current = folder;
  const value = h("span", { class: "folder-line-value truncate" });
  const error = h("span", { class: "field-error", role: "alert" });
  const change = h("button", { type: "button", class: "link-button", onClick: choose }, "Change");
  const element = h("div", { class: "folder-line" },
    h("span", { class: "folder-line-label muted" }, label), value, change, error);

  function show() {
    replace(value, folderLabel(current) || "Default folder");
    value.title = current || "";
    change.setAttribute("aria-label", current ? `Change folder (now ${current})` : "Change folder");
  }

  async function choose() {
    if (isInert(change)) return;
    replace(error);
    try {
      await busy(change, async () => {
        const path = await pickFolder(current || null);
        if (path) {
          current = path;
          show();
          onChange(path);
        }
      });
    } catch (failure) {
      replace(error, failure?.message || "Couldn't open the folder picker");
    }
  }

  show();
  return {
    element,
    getFolder: () => current,
    setFolder(path) {
      current = path ?? "";
      show();
    },
  };
}
