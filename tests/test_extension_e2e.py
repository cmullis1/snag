"""The popup's conversation with Snag, end to end over real HTTP and a real WebSocket (uvicorn on an
ephemeral port, the fake yt-dlp): every request carries the extension's Origin, as Chrome sends it
from chrome-extension://<EXTENSION_ID>/popup/popup.html. The CORS preflight, cookies sent only when
Snag answers that sign-in is required (spec 3.5: a lookup and a quick download are asked without them
first), the quick download of an ordinary page with none, and the hub reporting the job to the
extension."""
import asyncio
import json

import httpx
from websockets.asyncio.client import connect

from tests.server_env import EXTENSION, core, github, snag_defaults  # noqa: F401 (fixtures)
from tests.test_server_uvicorn import WS, next_event, server  # noqa: F401 (fixture)

COOKIES = [{"domain": "signin", "name": "SID", "value": "secret", "path": "/", "hostOnly": True,
            "secure": False, "httpOnly": True, "session": True}]


async def test_the_extension_sends_a_sites_cookies_only_when_sign_in_is_required(server, core):
    port, start = server
    await start(WS)
    base = f"http://127.0.0.1:{port}"
    origin = EXTENSION["origin"]
    async with httpx.AsyncClient(base_url=base, trust_env=False, timeout=15, headers=EXTENSION) as http, \
            connect(f"ws://127.0.0.1:{port}/ws", origin=origin) as hub:
        assert json.loads(await hub.recv())["type"] == "snapshot"

        # The browser asks first: a JSON POST from another origin is preflighted.
        preflight = await http.options("/api/lookup", headers={
            "access-control-request-method": "POST", "access-control-request-headers": "content-type"})
        assert preflight.status_code in (200, 204)
        assert preflight.headers["access-control-allow-origin"] == origin

        # Right-click or the YouTube button on a page that needs sign-in: asked without cookies first,
        # Snag says sign-in is required (the extension then asks again with them) and queues nothing.
        quick_refused = await http.post("/api/jobs/quick", json={"url": "fake://signin/quick"})
        assert quick_refused.status_code == 422, quick_refused.text
        assert quick_refused.json()["error"]["kind"] == "sign_in"
        assert core.queue.jobs == []

        # The popup on that page: the same, then the lookup with its cookies, which Snag keeps for the
        # download.
        refused = await http.post("/api/lookup", json={"url": "fake://signin"})
        assert refused.status_code == 422 and refused.json()["error"]["kind"] == "sign_in"
        lookup = await http.post("/api/lookup", json={"url": "fake://signin", "cookies": COOKIES})
        assert lookup.status_code == 200, lookup.text
        assert lookup.headers["access-control-allow-origin"] == origin
        assert lookup.json()["type"] == "video" and lookup.json()["title"] == "Fake Video"
        assert "secret" in core.cookies.get("fake://signin")

        # The one-click default for an ordinary page, which needs no cookies, followed on the hub to the end.
        quick = await http.post("/api/jobs/quick", json={"url": "fake://downloadable/e2e"})
        assert quick.status_code == 201, quick.text
        assert quick.headers["access-control-allow-origin"] == origin
        job_id = quick.json()["id"]
        async with asyncio.timeout(30):
            while True:
                event = await next_event(hub, "job")
                if event["job"]["id"] == job_id and event["job"]["state"] in ("done", "failed"):
                    break
        assert event["job"]["state"] == "done", event["job"]["error"]
