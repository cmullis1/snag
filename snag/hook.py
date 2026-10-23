"""Snag's test hook: SNAG_TEST_HOOK=1 in the environment, set only by Snag's own tests.

What it turns on (nothing else ever reads it):
- lookups and downloads accept fake://<scenario> addresses, which only the tests' stand-in for
  yt-dlp (tests/fake_ytdlp.py) understands (snag/server/routes/lookup.py);
- `python -m snag` honours SNAG_FAKE_TOOLS, SNAG_PORT and POST /api/quit (see snag/__main__.py);
- SNAG_START_MENU stands in for the user's Start menu folder, where Snag's shortcuts are looked
  for and made (snag/platform/startup.py), so setup.ps1's tests never touch the real ones.
Any value other than "1", or none, leaves Snag as it ships."""
from __future__ import annotations

import os
from collections.abc import Mapping

TEST_HOOK = "SNAG_TEST_HOOK"


def test_hook_on(environ: Mapping[str, str] | None = None) -> bool:
    return (os.environ if environ is None else environ).get(TEST_HOOK) == "1"
