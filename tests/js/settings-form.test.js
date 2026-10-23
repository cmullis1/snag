// The Settings form on a fake DOM: loading, per-field saves, refused saves, and the tools' rows.
import "./fake-dom.js";
import test from "node:test";
import assert from "node:assert/strict";

import { byClass, byText, find, findAll } from "./fake-dom.js";
import { createSettingsForm } from "../../ui/components/settings-form.js";
import { createStore } from "../../ui/store.js";

const settle = () => new Promise((resolve) => setImmediate(resolve));
const EXTRAS = {
  subtitles: true, subtitle_lang: "en", subtitle_mode: "embed", thumbnail_tags: true, chapters: true, sponsorblock: false,
  sponsorblock_mode: "remove", sponsorblock_categories: ["sponsor", "selfpromo", "interaction"], trim_start: null, trim_end: null,
};
const SETTINGS = {
  video_dir: "C:\\Users\\me\\Videos\\Snag", audio_dir: "C:\\Users\\me\\Music\\Snag", quick_max_height: null,
  quick_extras: EXTRAS, default_extras: EXTRAS, max_concurrent: 5, check_updates_daily: true, start_with_windows: true,
  remembered_dirs: [],
};

function make({ refuse = null } = {}) {
  const puts = [];
  const timers = new Map();
  let nextTimer = 0;
  let current = structuredClone(SETTINGS);
  const api = {
    getSettings: async () => structuredClone(current),
    updateSettings: async (changes) => {
      puts.push(changes);
      if (refuse) throw typeof refuse === "string" ? new Error(refuse) : refuse;
      current = { ...current, ...changes };
      return structuredClone(current);
    },
    pickFolder: async () => ({ path: "D:\\Clips" }),
    installUpdate: async () => ({ started: true }),
    checkForUpdate: async () => ({}),
    reinstallTool: async () => ({ started: true }),
  };
  const store = createStore();
  const told = [];
  const form = createSettingsForm({
    api, store, onSettings: (settings) => told.push(settings), now: () => new Date(Date.UTC(2026, 9, 7, 14, 30)),
    setTimer: (fn) => { nextTimer += 1; timers.set(nextTimer, fn); return nextTimer; },
    clearTimer: (id) => timers.delete(id),
    setRepeat: () => 0, clearRepeat: () => {},
  });
  const runTimers = async () => {
    while (timers.size) {
      const [id, fn] = timers.entries().next().value;
      timers.delete(id);
      fn();
    }
    await settle();
  };
  return { api, form, puts, store, told, runTimers };
}

const select = (form) => byClass(form.element, "settings-select")[0];
/** The sections' save notes, in order: Folders, One-click download, Defaults, Downloads, Snag. */
const notes = (form) => byClass(form.element, "settings-saved").map((node) => node.textContent);

test("loading shows every field", async () => {
  const { form, told } = make();
  await form.load();
  const folders = byClass(form.element, "folder-line-value");
  assert.deepEqual(folders.map((node) => node.title), [SETTINGS.video_dir, SETTINGS.audio_dir]);
  assert.equal(select(form).value, "");
  assert.equal(byClass(form.element, "stepper-value")[0].textContent, "5");
  assert.deepEqual(byClass(form.element, "switch").map((node) => node.checked), [true, true]);
  assert.equal(told.length, 1);
  assert.ok(form.element.textContent.includes("English subtitles, thumbnail and tags, chapters"));
});

test("a field saves just itself and says Saved", async () => {
  const { form, puts, told } = make();
  await form.load();
  select(form).value = "1080";
  select(form).dispatch("change");
  await settle();
  assert.deepEqual(puts, [{ quick_max_height: 1080 }]);
  assert.ok(form.element.textContent.includes("Saved"));
  assert.equal(told.at(-1).quick_max_height, 1080);
});

test("a refused save shows the server's message and puts the field back", async () => {
  const { form } = make({ refuse: "That setting isn't valid: quick_max_height: nope" });
  await form.load();
  select(form).value = "720";
  select(form).dispatch("change");
  await settle();
  assert.ok(form.element.textContent.includes("That setting isn't valid: quick_max_height: nope"));
  assert.equal(select(form).value, "");
});

test("the folder picker saves a new folder", async () => {
  const { form, puts } = make();
  await form.load();
  byClass(form.element, "folder-line")[1].children.find((node) => node.textContent === "Change").click();
  await settle();
  await settle();
  assert.deepEqual(puts, [{ audio_dir: "D:\\Clips" }]);
  assert.equal(byClass(form.element, "folder-line-value")[1].title, "D:\\Clips");
});

test("the stepper stays from 1 to 10 and saves once it settles", async () => {
  const { form, puts, runTimers } = make();
  await form.load();
  const more = find(form.element, (node) => node.getAttribute("aria-label") === "More at once");
  for (let i = 0; i < 8; i += 1) more.click();
  assert.equal(byClass(form.element, "stepper-value")[0].textContent, "10");
  assert.equal(more.getAttribute("aria-disabled"), "true");
  await runTimers();
  assert.deepEqual(puts, [{ max_concurrent: 10 }]);
});

test("switches save true or false", async () => {
  const { form, puts } = make();
  await form.load();
  const [startup] = byClass(form.element, "switch");
  startup.checked = false;
  startup.dispatch("change");
  await settle();
  assert.deepEqual(puts, [{ start_with_windows: false }]);
});

test("a saved field shows what the server saved, not what was sent", async () => {
  const { api, form, told } = make();
  await form.load();
  // Windows kept Snag in its startup list: the server answers start_with_windows true.
  api.updateSettings = async () => structuredClone(SETTINGS);
  const [startup] = byClass(form.element, "switch");
  startup.checked = false;
  startup.dispatch("change");
  await settle();
  assert.equal(startup.checked, true, "the switch shows what Windows will do");
  assert.equal(told.at(-1).start_with_windows, true);
  assert.deepEqual(notes(form), ["", "", "", "", "Windows didn't keep this"], "not Saved: the switch bounced back");
  // A folder the server tidied shows tidied.
  api.updateSettings = async (changes) => ({ ...SETTINGS, video_dir: changes.video_dir.replace(/\\+$/, "") });
  api.pickFolder = async () => ({ path: "D:\\Clips\\" });
  byClass(form.element, "folder-line")[0].children.find((node) => node.textContent === "Change").click();
  await settle();
  await settle();
  assert.equal(byClass(form.element, "folder-line-value")[0].title, "D:\\Clips");
  assert.equal(notes(form)[0], "Snag adjusted this");
});

test("a saved field the server kept as sent says Saved", async () => {
  const { form } = make();
  await form.load();
  const [startup] = byClass(form.element, "switch");
  startup.checked = false;
  startup.dispatch("change");
  await settle();
  assert.equal(notes(form)[4], "Saved");
});

test("extras save after the edits pause, and only when they're valid", async () => {
  const { form, puts, runTimers } = make();
  await form.load();
  const editors = byClass(form.element, "extras-editor");
  const sponsor = byText(editors[1], "SponsorBlock", "label").children[0];
  sponsor.checked = true;
  sponsor.dispatch("change");
  await runTimers();
  assert.equal(puts.length, 1);
  assert.deepEqual(Object.keys(puts[0]), ["default_extras"]);
  assert.equal(puts[0].default_extras.sponsorblock, true);
  const trim = byClass(editors[1], "extras-trim-input")[0];
  trim.value = "nonsense";
  trim.dispatch("input");
  await runTimers();
  assert.equal(puts.length, 1, "an invalid trim isn't saved");
});

test("the tools' rows follow the status", async () => {
  const { form, store } = make();
  store.dispatch({
    type: "snapshot", jobs: [], queue: {},
    status: {
      tools: {
        ytdlp: { present: true, version: "2026.08.19" }, ffmpeg: { present: true, version: "7.1" },
        ffprobe: { present: true, version: "7.1" }, deno: { present: false, version: null, message: null },
      },
      updater: { setup: { state: "done", tools: {} }, update: { available: "2026.09.30", state: "idle" } },
      youtube_login: "2026-10-07T14:18:00Z",
    },
  });
  const text = form.element.textContent;
  assert.ok(text.includes("yt-dlp 2026.08.19"));
  assert.ok(byText(form.element, "Update to 2026.09.30", "button"));
  assert.ok(text.includes("Missing, so some YouTube formats won't show"));
  assert.ok(text.includes("From your browser · 12 min ago"));
  store.dispatch({ type: "status", youtube_login: null });
  assert.ok(form.element.textContent.includes("Not available: sign in to YouTube in Chrome or Edge"));
});

test("settings changed elsewhere redraw only the fields that changed", async () => {
  const { form, store, told } = make();
  await form.load();
  const editors = byClass(form.element, "extras-editor");
  const trim = byClass(editors[1], "extras-trim-input")[0];
  trim.value = "1:00";
  trim.dispatch("input"); // being edited here, not saved yet
  store.dispatch({ type: "settings", settings: { ...structuredClone(SETTINGS), video_dir: "E:\Movies" } });
  assert.equal(byClass(form.element, "folder-line-value")[0].title, "E:\Movies");
  assert.equal(trim.value, "1:00", "an unchanged field keeps what's being typed");
  assert.equal(told.at(-1).video_dir, "E:\Movies");
  const count = told.length;
  store.dispatch({ type: "settings", settings: { ...structuredClone(SETTINGS), video_dir: "E:\Movies" } });
  assert.equal(told.length, count, "the same settings again change nothing");
});

test("extras the server refuses open their editor with its message", async () => {
  const { ApiError } = await import("../../ui/api.js");
  const refusal = new ApiError("Pick at least one SponsorBlock category.", "invalid_extras", "edit_extras", { status: 400 });
  const { form, runTimers } = make({ refuse: refusal });
  await form.load();
  const editors = byClass(form.element, "extras-editor");
  const box = editors[1].parentNode;
  assert.equal(box.hidden, true);
  const sponsor = byText(editors[1], "SponsorBlock", "label").children[0];
  sponsor.checked = true;
  sponsor.dispatch("change");
  await runTimers();
  assert.equal(box.hidden, false, "the editor opens");
  assert.ok(editors[1].textContent.includes("Pick at least one SponsorBlock category."));
  assert.equal(byText(form.element, "Done", "button").getAttribute("aria-expanded"), "true");
});

test("an older save answered after a newer one is dropped", async () => {
  const { api, form, told } = make();
  await form.load();
  const answers = [];
  api.updateSettings = (changes) => new Promise((resolve) => answers.push(() => resolve({ ...SETTINGS, ...changes })));
  select(form).value = "720";
  select(form).dispatch("change");
  select(form).value = "1080";
  select(form).dispatch("change");
  answers[1](); // the newer answer first
  await settle();
  answers[0]();
  await settle();
  assert.equal(told.at(-1).quick_max_height, 1080, "the newer answer stands");
  assert.equal(select(form).value, "1080");
});

test("a refused extras draft: going back to the saved value clears the error, and load() restores it", async () => {
  const { ApiError } = await import("../../ui/api.js");
  const refusal = new ApiError("Pick at least one SponsorBlock category.", "invalid_extras", "edit_extras", { status: 400 });
  const { form, runTimers } = make({ refuse: refusal });
  await form.load();
  const editors = byClass(form.element, "extras-editor");
  const sponsor = byText(editors[1], "SponsorBlock", "label").children[0];
  const fieldError = () => byClass(editors[1].parentNode.parentNode, "settings-status")[0].textContent;
  sponsor.checked = true;
  sponsor.dispatch("change");
  await runTimers();
  assert.equal(fieldError(), "Pick at least one SponsorBlock category.");
  sponsor.checked = false;
  sponsor.dispatch("change");
  await runTimers();
  assert.equal(fieldError(), "", "back to the saved value: no error");

  sponsor.checked = true;
  sponsor.dispatch("change");
  await runTimers();
  assert.equal(fieldError(), "Pick at least one SponsorBlock category.");
  await form.load();
  assert.equal(sponsor.checked, false, "load() shows the saved value again");
  assert.equal(fieldError(), "");
});

test("a save answered after a settings event keeps the event's settings", async () => {
  const { api, form, store, told, puts } = make();
  await form.load();
  const answers = [];
  api.updateSettings = (changes) => {
    puts.push(changes);
    return new Promise((resolve) => answers.push(() => resolve({ ...structuredClone(SETTINGS), ...changes })));
  };
  select(form).value = "720";
  select(form).dispatch("change"); // PUT sent
  // Another tab saved 1080 after this PUT reached the server: its event arrives before this answer.
  store.dispatch({ type: "settings", settings: { ...structuredClone(SETTINGS), quick_max_height: 1080 } });
  assert.equal(select(form).value, "1080");
  answers[0]();
  await settle();
  assert.equal(told.at(-1).quick_max_height, 1080, "the answer doesn't overwrite the newer event");
  assert.equal(select(form).value, "1080");
  assert.ok(form.element.textContent.includes("Saved"), "the save itself still worked");

  // The reviewer's repro: choosing 720 again is a change from what's saved (1080), so it's sent.
  select(form).value = "720";
  select(form).dispatch("change");
  assert.deepEqual(puts, [{ quick_max_height: 720 }, { quick_max_height: 720 }]);
  answers[1]();
  await settle();
  assert.equal(told.at(-1).quick_max_height, 720);
});

test("the form's own saves passed back through the store don't count as newer settings", async () => {
  const { api, form, store, told } = make();
  const passBack = (settings) => {
    if (store.getState().settings !== settings) store.dispatch({ type: "settings", settings });
  };
  await form.load();
  passBack(told.at(-1));
  const answers = [];
  api.updateSettings = (changes) => new Promise((resolve) => answers.push(() => resolve({ ...structuredClone(SETTINGS), ...changes })));
  select(form).value = "720";
  select(form).dispatch("change");
  select(form).value = "1080";
  select(form).dispatch("change");
  answers[0]();
  await settle();
  passBack(told.at(-1)); // as the shell does with onSettings
  answers[1]();
  await settle();
  assert.equal(told.at(-1).quick_max_height, 1080, "the newer answer still stands");
});

test("a load answered after a settings event shows the event's settings", async () => {
  const { api, form, store, told } = make();
  let answer;
  api.getSettings = () => new Promise((resolve) => { answer = () => resolve(structuredClone(SETTINGS)); });
  const loading = form.load();
  store.dispatch({ type: "settings", settings: { ...structuredClone(SETTINGS), video_dir: "E:\Newer" } });
  answer();
  await loading;
  assert.equal(byClass(form.element, "folder-line-value")[0].title, "E:\Newer");
  assert.equal(told.at(-1).video_dir, "E:\Newer");
});

/** Live regions (alerts, status, aria-live) whose text holds text: what a screen reader announces it from. */
const announcers = (root, text) => findAll(root, (node) => (["alert", "status"].includes(node.getAttribute("role"))
  || ["polite", "assertive"].includes(node.getAttribute("aria-live"))) && node.textContent.includes(text));

test("refused extras are announced once: the field's alert, with a quiet copy in the editor", async () => {
  const { ApiError } = await import("../../ui/api.js");
  const message = "Pick at least one SponsorBlock category.";
  const { form, runTimers } = make({ refuse: new ApiError(message, "invalid_extras", "edit_extras", { status: 400 }) });
  await form.load();
  const editors = byClass(form.element, "extras-editor");
  const sponsor = byText(editors[1], "SponsorBlock", "label").children[0];
  sponsor.checked = true;
  sponsor.dispatch("change");
  await runTimers();
  const live = announcers(form.element, message);
  assert.equal(live.length, 1);
  assert.ok(live[0].classList.contains("settings-status"));
  assert.ok(editors[1].textContent.includes(message), "still shown by the extras");
});
