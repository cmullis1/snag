"""Screenshots of the popup (through harness.html) and of the full tab, from the scratch Snag that
serve.py runs, into docs/screenshots/. Start serve.py first (it checks that it is talking to that scratch
Snag, never your real one, before doing anything), then:

    .venv/Scripts/python tests/js/extension/harness/screenshots.py

It drives headless Chrome (or Edge) over the DevTools protocol: each page at its width (the popup at
380 px, the tab at 1280 px), in light and dark where listed, waiting for the view to be drawn, and
captures the whole page. It queues a few downloads first (two that finish, for History, and one slow
one, so the queue shows progress) and switches the scratch Snag's updater state for the setting-up and
update screens."""
from __future__ import annotations

import asyncio
import base64
import itertools
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

import httpx
from websockets.asyncio.client import connect

REPO = Path(__file__).resolve().parents[4]
OUT = REPO / "docs" / "screenshots"
BASE = "http://127.0.0.1:47823"
HARNESS = f"{BASE}/harness/harness.html"
BROWSERS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
]

BBB = "https://www.youtube.com/watch?v=aqz-KE-bpKQ"
LIST = "PLav47HAVZMjnTFVZL-aImCQIC0uLZtNCz"
PLAYLIST = f"https://www.youtube.com/playlist?list={LIST}"
DUBBED = "https://www.youtube.com/watch?v=0e3GPea1Tyg"
ARCHIVE = "https://archive.org/details/Cops1922"
SIGN_IN = "https://www.youtube.com/watch?v=SignInOnly1"
SLOW = "https://www.youtube.com/watch?v=SlowSample1"  # downloads for minutes: the queue's progress


def popup(tab: str | None = None, **params: str) -> str:
    query = [f"tab={quote(tab, safe=':/')}"] if tab else []
    query += [f"{key}={value}" for key, value in params.items()]
    return HARNESS + ("?" + "&".join(query) if query else "")


def app(hash_: str = "") -> str:
    return f"{BASE}/{hash_}"


# (file stem, url, width, JS condition that says the view is drawn, schemes, updater mode, JS to run first)
POPUP_SHOTS = [
    ("popup-video", popup(BBB), ".popup-view .panel", None, None),
    ("popup-video-in-playlist", popup(f"{BBB}&list={LIST}"), ".popup-view .panel .note .panel-playlist-name", None, None),
    ("popup-playlist", popup(PLAYLIST), ".popup-playlist", None, None),
    ("popup-nothing", popup(None), ".popup-empty .popup-paste", None, None),
    ("popup-sign-in-error", popup(SIGN_IN), ".popup-empty .note-danger", None, None),
    ("popup-not-running", popup(BBB, offline="1"), ".popup-empty .note-warning", None, None),
    ("popup-setting-up", popup(BBB), ".popup-setup", "setup", None),
    ("popup-setup-failed", popup(BBB), ".popup-setup.note-danger", "setup-error", None),
    ("popup-update-available", popup(BBB), ".popup-notice:not([hidden]) ~ .popup-view .panel", "update", None),
]

SHOW_ALL = """(() => { const b = [...document.querySelectorAll('button')].find((x) => /^Show all/.test(x.textContent.trim()));
  if (b) b.click(); return Boolean(b); })()"""

TAB_SHOTS = [
    ("tab-download", app(f"#download?url={quote(BBB, safe='')}"), ".lookup-result .panel", ("light", "dark"), None, None),
    ("tab-formats-expanded", app(f"#download?url={quote(BBB, safe='')}"), ".lookup-result .panel", ("light",), None, SHOW_ALL),
    ("tab-playlist-picker", app(f"#playlist?url={quote(PLAYLIST, safe='')}"), ".picker .picker-row", ("light", "dark"), None, None),
    ("tab-history", app("#history"), ".history-row", ("light",), None, None),
    ("tab-settings", app("#settings"), ".settings .settings-section", ("light",), None, None),
    ("tab-setup", app(), ".setup", ("light",), "setup", None),
]


def check_scratch() -> None:
    """Exits unless the Snag on BASE is serve.py's scratch one: the screenshots queue downloads and switch
    the updater's state, which must never happen to your real Snag."""
    serve = ".venv/Scripts/python tests/js/extension/harness/serve.py"
    try:
        answer = httpx.get(f"{BASE}/scratch/hello", headers={"origin": BASE}, timeout=10, trust_env=False)
    except httpx.HTTPError:
        sys.exit(f"Nothing answers on {BASE}. Start the scratch Snag first: {serve}")
    try:
        data = answer.json() if answer.status_code == 200 else None
    except ValueError:
        data = None
    scratch = isinstance(data, dict) and data.get("scratch") is True
    if not scratch:
        sys.exit(f"The Snag on {BASE} isn't the scratch one, so nothing was done. Quit Snag, then start the scratch one: {serve}")


def find_browser() -> str:
    for path in BROWSERS:
        if Path(path).is_file():
            return path
    sys.exit("Neither Chrome nor Edge was found.")


class Page:
    def __init__(self, ws):
        self.ws = ws
        self.ids = itertools.count(1)

    async def send(self, method: str, **params):
        call = next(self.ids)
        await self.ws.send(json.dumps({"id": call, "method": method, "params": params}))
        while True:
            message = json.loads(await self.ws.recv())
            if message.get("id") == call:
                if "error" in message:
                    raise RuntimeError(f"{method}: {message['error']}")
                return message.get("result", {})

    async def evaluate(self, expression: str):
        result = await self.send("Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=True)
        return result.get("result", {}).get("value")

    async def wait_for(self, selector: str, timeout: float = 20.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if await self.evaluate(f"Boolean(document.querySelector({json.dumps(selector)}))"):
                return
            await asyncio.sleep(0.2)
        raise TimeoutError(f"{selector} never appeared")

    async def shoot(self, url: str, path: Path, *, width: int, height: int, scheme: str, ready: str,
                    before: str | None = None) -> None:
        await self.send("Emulation.setDeviceMetricsOverride", width=width, height=height, deviceScaleFactor=1, mobile=False)
        await self.send("Emulation.setEmulatedMedia", media="screen",
                        features=[{"name": "prefers-color-scheme", "value": scheme}])
        await self.send("Page.navigate", url="about:blank")
        await self.send("Page.navigate", url=url)
        await self.wait_for(ready)
        if before:
            await self.evaluate(before)
        await asyncio.sleep(1.5)  # thumbnails, the hub's snapshot, the queue line
        # The whole page in one viewport, so what is fixed to its bottom (the queue bar) is drawn there.
        full = max(height, await self.evaluate("Math.ceil(document.documentElement.scrollHeight)"))
        await self.send("Emulation.setDeviceMetricsOverride", width=width, height=full, deviceScaleFactor=1, mobile=False)
        await asyncio.sleep(0.3)
        shot = await self.send("Page.captureScreenshot", format="png",
                               clip={"x": 0, "y": 0, "width": width, "height": full, "scale": 1})
        path.write_bytes(base64.b64decode(shot["data"]))
        print(f"  {path.relative_to(REPO)}", flush=True)


async def set_updater(http: httpx.AsyncClient, mode: str | None) -> None:
    (await http.post(f"/scratch/updater?mode={mode or 'normal'}")).raise_for_status()


async def seed(http: httpx.AsyncClient) -> None:
    """Two finished downloads for History, then one slow one for the queue's progress."""
    finished = []
    for url in (ARCHIVE, DUBBED):
        response = await http.post("/api/jobs/quick", json={"url": url})
        response.raise_for_status()
        finished.append(response.json()["id"])
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        jobs = {job["id"]: job for job in (await http.get("/api/jobs")).json()["jobs"]}
        if all(jobs.get(job_id, {}).get("state") in ("done", "failed") for job_id in finished):
            break
        await asyncio.sleep(0.5)
    (await http.post("/api/jobs/quick", json={"url": SLOW})).raise_for_status()


async def main() -> None:
    check_scratch()
    OUT.mkdir(parents=True, exist_ok=True)
    profile = Path(tempfile.mkdtemp(prefix="snagshot-"))
    browser = subprocess.Popen([
        find_browser(), "--headless=new", "--remote-debugging-port=0", f"--user-data-dir={profile}",
        "--no-first-run", "--no-default-browser-check", "--disable-extensions", "--hide-scrollbars", "about:blank",
    ])
    try:
        port_file = profile / "DevToolsActivePort"
        deadline = time.monotonic() + 30
        while not port_file.is_file() and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
        port = port_file.read_text().split()[0]
        async with httpx.AsyncClient(base_url=BASE, headers={"origin": BASE}, timeout=30, trust_env=False) as http:
            await set_updater(http, None)
            await seed(http)
            targets = httpx.get(f"http://127.0.0.1:{port}/json/list", trust_env=False).json()
            page_ws = next(t["webSocketDebuggerUrl"] for t in targets if t["type"] == "page")
            async with connect(page_ws, max_size=64 * 1024 * 1024) as ws:
                page = Page(ws)
                await page.send("Page.enable")
                print("popup:", flush=True)
                for stem, url, ready, mode, before in POPUP_SHOTS:
                    await set_updater(http, mode)
                    for scheme in ("light", "dark"):
                        await page.shoot(url, OUT / f"{stem}-{scheme}.png", width=380, height=200, scheme=scheme,
                                         ready=ready, before=before)
                print("tab:", flush=True)
                for stem, url, ready, schemes, mode, before in TAB_SHOTS:
                    await set_updater(http, mode)
                    for scheme in schemes:
                        name = stem if len(schemes) == 1 else f"{stem}-{scheme}"
                        await page.shoot(url, OUT / f"{name}.png", width=1280, height=800, scheme=scheme,
                                         ready=ready, before=before)
            await set_updater(http, None)
    finally:
        browser.terminate()
        try:
            browser.wait(10)
        except subprocess.TimeoutExpired:
            browser.kill()
        shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    os.environ.setdefault("NO_PROXY", "127.0.0.1")
    asyncio.run(main())
