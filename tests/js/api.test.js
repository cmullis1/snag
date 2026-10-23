import test from "node:test";
import assert from "node:assert/strict";

import { createApi, ApiError } from "../../ui/api.js";

/** A fetch stand-in recording each call and answering with `respond(url, init)` (a Response, or a
 * function throwing to simulate a network failure). */
function stubFetch(respond = () => Response.json({ ok: true })) {
  const calls = [];
  const fetch = async (url, init = {}) => {
    calls.push({ url, method: init.method ?? "GET", headers: init.headers ?? {}, body: init.body, signal: init.signal });
    return respond(url, init);
  };
  return { fetch, calls };
}

const BASE = "http://127.0.0.1:47823";

test("lookup posts the URL as JSON and returns the parsed reply", async () => {
  const { fetch, calls } = stubFetch(() => Response.json({ type: "video", title: "A video" }));
  const api = createApi(BASE, { fetch });
  const info = await api.lookup("https://youtu.be/x");
  assert.deepEqual(info, { type: "video", title: "A video" });
  assert.equal(calls[0].url, `${BASE}/api/lookup`);
  assert.equal(calls[0].method, "POST");
  assert.equal(calls[0].headers["Content-Type"], "application/json");
  assert.deepEqual(JSON.parse(calls[0].body), { url: "https://youtu.be/x" });
});

test("cookies are sent only when given", async () => {
  const { fetch, calls } = stubFetch();
  const api = createApi(BASE, { fetch });
  const cookies = [{ name: "SID", value: "v", domain: ".youtube.com" }];
  await api.lookup("https://youtu.be/x", { cookies });
  await api.quickJob("https://youtu.be/x", { cookies });
  await api.quickJob("https://youtu.be/y");
  assert.deepEqual(JSON.parse(calls[0].body), { url: "https://youtu.be/x", cookies });
  assert.deepEqual(JSON.parse(calls[1].body), { url: "https://youtu.be/x", cookies });
  assert.deepEqual(JSON.parse(calls[2].body), { url: "https://youtu.be/y" });
});

test("a lookup can be aborted", async () => {
  const { fetch, calls } = stubFetch((url, init) => {
    throw Object.assign(new Error("aborted"), { name: "AbortError", signal: init.signal });
  });
  const api = createApi(BASE, { fetch });
  const controller = new AbortController();
  await assert.rejects(api.lookup("https://youtu.be/x", { signal: controller.signal }), { name: "AbortError" });
  assert.equal(calls[0].signal, controller.signal);
});

/** A fetch that fails the way browsers do once its signal aborts: with whatever error it likes. */
function failingOnAbort(error) {
  return stubFetch(() => {
    throw error;
  }).fetch;
}

test("a request aborted with a reason rejects with that reason, not offline", async () => {
  const controller = new AbortController();
  const reason = new Error("the user typed another link");
  controller.abort(reason);
  const api = createApi(BASE, { fetch: failingOnAbort(new TypeError("Failed to fetch")) });
  await assert.rejects(api.lookup("https://youtu.be/x", { signal: controller.signal }), (error) => error === reason);
});

test("a request that timed out rejects with the TimeoutError, not offline", async () => {
  const signal = AbortSignal.timeout(1);
  while (!signal.aborted) await new Promise((resolve) => setTimeout(resolve, 5)); // its own timer is unref'd
  const api = createApi(BASE, { fetch: failingOnAbort(new TypeError("Failed to fetch")) });
  await assert.rejects(api.lookup("https://youtu.be/x", { signal }), { name: "TimeoutError" });
});

test("an abort while reading the body rejects with the abort too", async () => {
  const controller = new AbortController();
  const api = createApi(BASE, {
    fetch: async () => ({
      ok: true,
      status: 200,
      text: async () => {
        controller.abort();
        throw new TypeError("network error");
      },
    }),
  });
  await assert.rejects(api.lookup("https://youtu.be/x", { signal: controller.signal }), { name: "AbortError" });
});

test("a failure with a signal that wasn't aborted is still offline", async () => {
  const controller = new AbortController();
  const api = createApi(BASE, { fetch: failingOnAbort(new TypeError("Failed to fetch")) });
  await assert.rejects(api.lookup("https://youtu.be/x", { signal: controller.signal }), { kind: "offline" });
});

test("the base URL may be empty (same origin) or end with a slash", async () => {
  const { fetch, calls } = stubFetch();
  await createApi("", { fetch }).getStatus();
  await createApi(`${BASE}/`, { fetch }).getStatus();
  assert.equal(calls[0].url, "/api/status");
  assert.equal(calls[1].url, `${BASE}/api/status`);
});

test("every endpoint uses its method and path", async () => {
  const { fetch, calls } = stubFetch();
  const api = createApi("", { fetch });
  const cases = [
    [() => api.createJob({ url: "u", format_id: "137" }), "POST", "/api/jobs", { url: "u", format_id: "137" }],
    [() => api.createBatch({ url: "p", entries: [1, 2], quality: { kind: "mp3" } }), "POST", "/api/jobs/batch",
      { url: "p", entries: [1, 2], quality: { kind: "mp3" } }],
    [() => api.listJobs(), "GET", "/api/jobs", undefined],
    [() => api.cancelJob("j1"), "POST", "/api/jobs/j1/cancel", undefined],
    [() => api.retryJob("j1"), "POST", "/api/jobs/j1/retry", {}],
    [() => api.retryJob("j1", { destDir: "D:\\Videos" }), "POST", "/api/jobs/j1/retry", { dest_dir: "D:\\Videos" }],
    [() => api.clearJob("j1"), "POST", "/api/jobs/j1/clear", undefined],
    [() => api.clearFinished(), "POST", "/api/jobs/clear-finished", undefined],
    [() => api.getBatch("b1"), "GET", "/api/batches/b1", undefined],
    [() => api.retryFailed("b1"), "POST", "/api/batches/b1/retry-failed", undefined],
    [() => api.pauseQueue(), "POST", "/api/queue/pause", undefined],
    [() => api.resumeQueue(), "POST", "/api/queue/resume", undefined],
    [() => api.getQueue(), "GET", "/api/queue", undefined],
    [() => api.removeHistoryEntry(7), "DELETE", "/api/history/7", undefined],
    [() => api.clearHistory(), "DELETE", "/api/history", undefined],
    [() => api.openHistoryFolder(7), "POST", "/api/history/7/open-folder", undefined],
    [() => api.getSettings(), "GET", "/api/settings", undefined],
    [() => api.updateSettings({ max_concurrent: 3 }), "PUT", "/api/settings", { max_concurrent: 3 }],
    [() => api.sendCookies("https://youtube.com/", [{ name: "a" }]), "POST", "/api/cookies",
      { url: "https://youtube.com/", cookies: [{ name: "a" }] }],
    [() => api.getStatus(), "GET", "/api/status", undefined],
    [() => api.pickFolder(), "POST", "/api/pick-folder", {}],
    [() => api.pickFolder("C:\\Music"), "POST", "/api/pick-folder", { initial: "C:\\Music" }],
    [() => api.checkForUpdate(), "POST", "/api/update/check", undefined],
    [() => api.installUpdate(), "POST", "/api/update/install", undefined],
    [() => api.reinstallTool("ffmpeg"), "POST", "/api/tools/ffmpeg/reinstall", undefined],
  ];
  for (const [call, method, path, body] of cases) {
    calls.length = 0;
    await call();
    assert.equal(calls[0].method, method, path);
    assert.equal(calls[0].url, path);
    assert.deepEqual(calls[0].body === undefined ? undefined : JSON.parse(calls[0].body), body, path);
  }
});

test("ids are escaped in paths", async () => {
  const { fetch, calls } = stubFetch();
  await createApi("", { fetch }).cancelJob("a/b?c");
  assert.equal(calls[0].url, "/api/jobs/a%2Fb%3Fc/cancel");
});

test("history paging builds a query and leaves out empty values", async () => {
  const { fetch, calls } = stubFetch(() => Response.json({ entries: [], total: 0 }));
  const api = createApi("", { fetch });
  await api.listHistory({ search: "cats & dogs", kind: "audio", limit: 50, offset: 100 });
  await api.listHistory({ search: "", kind: null, limit: 50 });
  await api.listHistory();
  assert.equal(calls[0].url, "/api/history?search=cats+%26+dogs&kind=audio&limit=50&offset=100");
  assert.equal(calls[1].url, "/api/history?limit=50");
  assert.equal(calls[2].url, "/api/history");
});

test("an empty reply is null", async () => {
  const { fetch } = stubFetch(() => new Response(null, { status: 204 }));
  assert.equal(await createApi("", { fetch }).clearHistory(), null);
});

test("a server error carries its message, kind and action", async () => {
  const error = { kind: "folder", message: "Can't save to D:\\Gone", action: "change_folder", can_retry: false, details: "" };
  const { fetch } = stubFetch(() => Response.json({ error }, { status: 400 }));
  const api = createApi("", { fetch });
  await assert.rejects(api.createJob({}), (thrown) => {
    assert.ok(thrown instanceof ApiError);
    assert.equal(thrown.message, "Can't save to D:\\Gone");
    assert.equal(thrown.kind, "folder");
    assert.equal(thrown.action, "change_folder");
    assert.equal(thrown.status, 400);
    assert.deepEqual(thrown.error, error);
    return true;
  });
});

test("FastAPI's own error text is used when there's no error object", async () => {
  const { fetch } = stubFetch(() => Response.json({ detail: "Not Found" }, { status: 404 }));
  await assert.rejects(createApi("", { fetch }).cancelJob("x"), { message: "Not Found", kind: "http", status: 404 });
});

test("an error without JSON gets a plain message", async () => {
  const { fetch } = stubFetch(() => new Response("Internal Server Error", { status: 500 }));
  await assert.rejects(createApi("", { fetch }).getStatus(), {
    name: "ApiError", message: "Snag couldn't do that (error 500)", kind: "http", action: null, status: 500,
  });
});

test("an error whose detail isn't text gets a plain message", async () => {
  const { fetch } = stubFetch(() => Response.json({ detail: [{ msg: "field required" }] }, { status: 422 }));
  await assert.rejects(createApi("", { fetch }).createJob({}), { message: "Snag couldn't do that (error 422)" });
});

test("when the server can't be reached the error says Snag isn't running", async () => {
  const { fetch } = stubFetch(() => { throw new TypeError("fetch failed"); });
  await assert.rejects(createApi(BASE, { fetch }).lookup("https://youtu.be/x"), {
    name: "ApiError", message: "Snag isn't running", kind: "offline", action: null, status: null,
  });
});

test("a reply cut off mid-way counts as offline", async () => {
  const broken = { ok: true, status: 200, text: async () => { throw new TypeError("terminated"); } };
  const { fetch } = stubFetch(() => broken);
  await assert.rejects(createApi("", { fetch }).getStatus(), { kind: "offline" });
});

test("ApiError is an Error with defaults", () => {
  const error = new ApiError("Nope");
  assert.ok(error instanceof Error);
  assert.equal(error.name, "ApiError");
  assert.equal(error.kind, "unknown");
  assert.equal(error.action, null);
  assert.equal(error.status, null);
  assert.equal(error.error, null);
});

test("a finished job's folder is opened by its id", async () => {
  const { fetch, calls } = stubFetch(() => Response.json({ opened: "D:\Videos\Snag\a.mp4" }));
  const api = createApi("", { fetch });
  assert.deepEqual(await api.openJobFolder("j 1"), { opened: "D:\Videos\Snag\a.mp4" });
  assert.equal(calls[0].method, "POST");
  assert.equal(calls[0].url, "/api/jobs/j%201/open-folder");
  assert.equal(calls[0].body, undefined);
});

test("a job folder that can't be opened rejects with the server's message", async () => {
  const { fetch } = stubFetch(() => Response.json(
    { error: { kind: "not_found", message: "There's no such download. It may have been cleared." } }, { status: 404 }));
  const api = createApi("", { fetch });
  await assert.rejects(api.openJobFolder("j1"), { name: "ApiError", status: 404, kind: "not_found",
    message: "There's no such download. It may have been cleared." });
});

test("first-run setup is retried with a POST", async () => {
  const { fetch, calls } = stubFetch(() => Response.json({ started: true, status: {} }, { status: 202 }));
  const api = createApi("", { fetch });
  assert.deepEqual(await api.retrySetup(), { started: true, status: {} });
  assert.equal(calls[0].method, "POST");
  assert.equal(calls[0].url, "/api/setup/retry");
});
