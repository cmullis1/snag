"""The harness's stand-in for yt-dlp: real recorded lookups (tests/samples) for the addresses the
screenshots use, and the tests' fake yt-dlp (tests/fake_ytdlp.py) for everything else.

Lookups (-J):
  youtube.com/watch?v=0e3GPea1Tyg   the dubbed sample (several audio languages)
  youtube.com/watch?v=SignInOnly1   an age-restricted video the login Snag has doesn't open
  youtube.com/watch?v=aqz-KE-bpKQ   Big Buck Bunny (with &list=..., Snag adds the playlist link itself)
  youtube.com/watch?v=<other id>    Big Buck Bunny's formats under that id, titled "Sample video <id>"
  youtube.com/playlist?list=...     the Blender Open Movies playlist
  archive.org/...                   Buster Keaton's "Cops"
  fake://...                        as tests/fake_ytdlp.py answers
  anything else                     "Unsupported URL", as yt-dlp says for a page it can't read
Downloads: the dubbed sample and archive.org finish in a moment (fake://medium); other web addresses
download slowly (about two minutes) so the queue shows progress; fake:// as the fake does."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

REPO = Path(__file__).resolve().parents[4]
SAMPLES = REPO / "tests" / "samples"


def _fake():
    spec = importlib.util.spec_from_file_location("fake_ytdlp", REPO / "tests" / "fake_ytdlp.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sample_for(url: str) -> Path | None:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host.endswith("youtube.com"):
        if parts.path == "/playlist":
            return SAMPLES / "youtube_playlist.json"
        if parts.path == "/watch":
            video = parse_qs(parts.query).get("v", [""])[0]
            return SAMPLES / ("youtube_dubbed.json" if video == "0e3GPea1Tyg" else "youtube_bbb.json")
    if host.endswith("archive.org"):
        return SAMPLES / "archive_org.json"
    return None


def quick(url: str) -> bool:
    return "0e3GPea1Tyg" in url or "archive.org" in url


def main(args: list[str]) -> int:
    fake = _fake()
    if not args or args == ["--version"] or args[-1].startswith("fake://"):
        return fake.main(args)
    url = args[-1]
    if "-J" in args:
        if "SignInOnly1" in url:
            return fake.fail("[youtube] SignInOnly1: Sign in to confirm your age. This video may be inappropriate for some users.")
        sample = sample_for(url)
        if sample is None:
            return fake.fail(f"Unsupported URL: {url}")
        info = json.loads(sample.read_text(encoding="utf-8"))
        video = parse_qs(urlsplit(url).query).get("v", [""])[0]
        if sample.name == "youtube_bbb.json" and video and video != info["id"]:
            info.update(id=video, title=f"Sample video {video}", webpage_url=f"https://www.youtube.com/watch?v={video}")
        fake.out(json.dumps(info))
        return 0
    if quick(url):
        return fake.main([*args[:-1], "fake://medium/harness"])
    return fake.download(args, steps=2400, delay=0.05)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
