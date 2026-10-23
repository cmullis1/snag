"""Stand-in for yt-dlp used by tests. The URL (last argument) picks the behaviour:
fake://<scenario> or fake://<scenario>/<token>.

Like the real yt-dlp it writes relative to its current folder (or -P when given), expands the title
fields of the -o template (%(title)s, %(title).<n>s, %(title).<n>B), downloads subtitles (format id NA)
before printing the chosen format (SNAGFMT), keeps each stream of a merge in "<name>.f<id>.<ext>.part"
and a single stream in "<name>.<ext>.part" while it downloads, runs MoveFiles last and prints the final
absolute path (SNAGFILE) at the end. Progress and printed lines go to stdout, post-processing lines to
stderr. Being run with --print, it prints none of yt-dlp's usual chatter: only the templates, warnings
and errors."""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

DL = "SNAGDL {status}|{done}|{total}|NA|{speed}|{eta}|{fid}"
PP = "SNAGPP {status}|{name}"
SUBTITLE_SIZE = 38
TWO_STREAMS = (("v", 4000), ("a", 1000))
ONE_STREAM = (("combined", 5000),)

INFO_VIDEO = {
    "_type": "video",
    "id": "abc123",
    "title": "Fake Video",
    "extractor_key": "Youtube",
    "webpage_url": "https://www.youtube.com/watch?v=abc123",
    "channel": "Fake Channel",
    "duration": 10,
    "formats": [
        {"format_id": "140", "ext": "m4a", "protocol": "https", "vcodec": "none", "acodec": "mp4a.40.2", "abr": 129.5, "filesize": 1000},
        {"format_id": "299", "ext": "mp4", "protocol": "https", "vcodec": "avc1.64002A", "acodec": "none", "height": 1080, "fps": 60, "filesize": 4000},
    ],
}

INFO_PLAYLIST = {
    "_type": "playlist",
    "id": "PL1",
    "title": "Fake Playlist",
    "extractor_key": "YoutubeTab",
    "webpage_url": "https://www.youtube.com/playlist?list=PL1",
    "entries": [
        {"id": "v1", "url": "https://www.youtube.com/watch?v=v1", "title": "One", "duration": 61},
        {"id": "v2", "url": "https://www.youtube.com/watch?v=v2", "title": "Two", "duration": 62},
    ],
}

# A playlist whose entries the fake can download (their URLs are fake://success/...).
INFO_FAKE_PLAYLIST = {
    "_type": "playlist",
    "id": "PLF",
    "title": "Fake: Playlist?",
    "extractor_key": "YoutubeTab",
    "webpage_url": "https://www.youtube.com/playlist?list=PLF",
    "entries": [
        {"id": "p1", "url": "fake://success/p1", "title": "One", "duration": 61},
        {"id": "p2", "url": "fake://success/p2", "title": "Two", "duration": 62},
        {"id": "p3", "url": "fake://success/p3", "title": "Three", "duration": 63},
    ],
}

# One page split into parts with no address of their own (yt-dlp's "multi_video"): each part is
# downloaded from the page with --playlist-items.
INFO_MULTIPART = {
    "_type": "multi_video",
    "id": "multi",
    "title": "Fake Parts",
    "extractor_key": "Generic",
    "webpage_url": "fake://success/multi",
    "entries": [
        {"id": "m1", "title": "Part 1", "duration": 30},
        {"id": "m2", "title": "Part 2", "duration": 31},
    ],
}


# yt-dlp's message when a subtitle download fails (YoutubeDL._write_subtitles), here rate-limited.
SUBTITLE_429 = "Unable to download video subtitles for {lang!r}: HTTP Error 429: Too Many Requests"

# yt-dlp's message when a download link answers 403 (YouTube's links go stale).
FORBIDDEN = "unable to download video data: HTTP Error 403: Forbidden"

_TITLE_FIELD = re.compile(r"%\(title\)(?:\.(\d+))?([sB])")


def out(line):
    print(line, flush=True)


def err(line):
    print(line, file=sys.stderr, flush=True)


def arg_after(args, flag):
    return args[args.index(flag) + 1] if flag in args else None


def fail(message):
    err(f"ERROR: {message}")
    return 1


def expand(template, title, ext):
    def title_field(match):
        limit, conversion = match.groups()
        if limit is None:
            return title
        if conversion == "B":
            return title.encode("utf-8")[:int(limit)].decode("utf-8", "ignore")
        return title[:int(limit)]

    return _TITLE_FIELD.sub(title_field, template).replace("%(ext)s", ext)


def output_path(args, template, title, ext):
    """Where the finished file goes: relative to the current folder, or to -P like the real yt-dlp."""
    return Path(os.path.abspath(Path(arg_after(args, "-P") or ".") / expand(template, title, ext)))


def move_files_and_report(final):
    err(PP.format(status="started", name="MoveFiles"))
    err(PP.format(status="finished", name="MoveFiles"))
    out(f"SNAGFILE {final}")


def download(args, *, steps=4, delay=0.0, title="Fake Video", ext="mp4", streams=TWO_STREAMS, write=True):
    template = arg_after(args, "-o")
    if template is None:
        return fail("fake yt-dlp: missing -o")
    final = output_path(args, template, title, ext)
    if write:
        final.parent.mkdir(parents=True, exist_ok=True)
    if "--write-subs" in args:
        out(DL.format(status="downloading", done=SUBTITLE_SIZE, total=SUBTITLE_SIZE, speed="NA", eta="NA", fid="NA"))
        out(DL.format(status="finished", done=SUBTITLE_SIZE, total=SUBTITLE_SIZE, speed=9000.0, eta="NA", fid="NA"))
    out(f"SNAGFMT {'+'.join(fid for fid, _ in streams)}")
    merging = len(streams) > 1
    stream_files = []
    for fid, total in streams:
        part = final.with_name(f"{final.stem}.f{fid}.{ext}.part" if merging else f"{final.name}.part")
        for i in range(1, steps + 1):
            if write:
                part.write_bytes(b"x" * (total * i // steps))
            out(DL.format(status="downloading", done=total * i // steps, total=total, speed=1000.0, eta=steps - i, fid=fid))
            if delay:
                time.sleep(delay)
        out(DL.format(status="finished", done=total, total=total, speed=1000.0, eta="NA", fid=fid))
        if write:
            stream_files.append(part.replace(part.with_suffix("")))
    if merging:
        err(PP.format(status="started", name="Merger"))
        err(PP.format(status="finished", name="Merger"))
    if not write:
        return 0
    final.write_bytes(b"x" * sum(total for _, total in streams))
    for stream_file in stream_files:
        if stream_file != final:
            stream_file.unlink()
    if "--convert-subs" in args:
        lang = arg_after(args, "--sub-langs") or "en"
        final.with_name(f"{final.stem}.{lang}.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n", encoding="utf-8")
    move_files_and_report(final)
    return 0


def already_complete(args, *, title="Fake Video", ext="mp4", total=5000):
    """A file an earlier attempt finished: yt-dlp reports it finished without a byte count."""
    final = output_path(args, arg_after(args, "-o"), title, ext)
    final.parent.mkdir(parents=True, exist_ok=True)
    final.write_bytes(b"x" * total)
    out("SNAGFMT combined")
    out(DL.format(status="finished", done="NA", total=total, speed="NA", eta="NA", fid="combined"))
    move_files_and_report(final)
    return 0


SLEEPER = "import time; time.sleep(60)"
# Starts SLEEPER (which inherits the launcher's output, i.e. ours), writes its pid to argv[1] and exits
# at once, so the sleeper's parent is gone.
LAUNCHER = (
    "import subprocess, sys\n"
    f"sleeper = subprocess.Popen([sys.executable, '-c', {SLEEPER!r}])\n"
    "with open(sys.argv[1], 'w') as f:\n"
    "    f.write(str(sleeper.pid))\n"
)


def start_stray(*, orphaned=False):
    """Start a long sleeper that inherits (and so holds open) our output pipes, like an ffmpeg that
    yt-dlp lost track of. orphaned starts it through a process that exits at once, so nothing links it
    to us any more. Its pid goes to SNAG_FAKE_STATE/stray-pid."""
    pid_file = Path(os.environ["SNAG_FAKE_STATE"]) / "stray-pid"
    if orphaned:
        subprocess.run([sys.executable, "-c", LAUNCHER, str(pid_file)], check=True)
    else:
        pid_file.write_text(str(subprocess.Popen([sys.executable, "-c", SLEEPER]).pid))


def subtitles_rate_limited(args):
    """Real yt-dlp fetches subtitles before the video (and before printing the format), so the
    failure leaves no video data behind."""
    lang = arg_after(args, "--sub-langs") or "en"
    return fail(SUBTITLE_429.format(lang=lang))


def first_time(token):
    """True the first time a token is seen. Markers live in SNAG_FAKE_STATE (set per test by the
    fake_tools fixture) so runs and tests don't see each other's markers."""
    state_dir = Path(os.environ.get("SNAG_FAKE_STATE") or tempfile.gettempdir())
    marker = state_dir / f"snag-fake-{token}"
    if marker.exists():
        return False
    marker.write_text("seen")
    return True


VERSION = "2026.08.19"


def main(args):
    if args == ["--version"]:
        out(VERSION)
        return 0
    url = args[-1]
    scenario, _, token = url.removeprefix("fake://").partition("/")
    if "-J" in args:
        if scenario == "video":
            out(json.dumps(INFO_VIDEO))
            return 0
        if scenario == "playlist":
            out(json.dumps(INFO_PLAYLIST))
            return 0
        if scenario == "downloadable":
            # A video whose page URL (what a job downloads) is one the fake can download.
            out(json.dumps({**INFO_VIDEO, "id": token or INFO_VIDEO["id"], "webpage_url": f"fake://success/{token}"}))
            return 0
        if scenario == "live":
            # A live stream (or, with fake://live/is_upcoming, one that hasn't started).
            out(json.dumps({**INFO_VIDEO, "live_status": token or "is_live"}))
            return 0
        if scenario == "fakelist":
            out(json.dumps(INFO_FAKE_PLAYLIST))
            return 0
        if scenario == "multipart":
            out(json.dumps(INFO_MULTIPART))
            return 0
        if scenario == "signin":
            # Like an age-restricted video: only a lookup with a readable cookies file gets the info.
            cookies = arg_after(args, "--cookies")
            if cookies and Path(cookies).is_file() and Path(cookies).read_text(encoding="utf-8").strip():
                out(json.dumps(INFO_VIDEO))
                return 0
            return fail("[youtube] abc123: Sign in to confirm your age. This video may be inappropriate for some users.")
        return fail(f"Unsupported URL: {url}")
    if scenario == "success":
        return download(args)
    if scenario == "single":
        return download(args, streams=ONE_STREAM)
    if scenario == "slow-single":
        return download(args, steps=400, delay=0.05, streams=ONE_STREAM)
    if scenario == "resume-complete":
        return already_complete(args)
    if scenario == "stray-child":
        code = download(args, streams=ONE_STREAM)
        start_stray()
        return code
    if scenario == "slow-stray":
        start_stray(orphaned=True)
        return download(args, steps=400, delay=0.05)
    if scenario == "medium":
        return download(args, steps=10, delay=0.03)
    if scenario == "slow":
        return download(args, steps=400, delay=0.05)
    if scenario == "slow-once":
        # Slow the first time (so it can be interrupted), quick when it runs again after a restart.
        if first_time(token):
            return download(args, steps=400, delay=0.05)
        return download(args)
    if scenario == "unicode":
        return download(args, title="Café ünïcode ✓")
    if scenario == "no-file":
        return download(args, write=False)
    if scenario == "subs-fail":
        return subtitles_rate_limited(args) if "--write-subs" in args else download(args)
    if scenario == "fail-network":
        return fail("Unable to download webpage: <urlopen error [Errno 11001] getaddrinfo failed>")
    if scenario == "unavailable":
        return fail("[youtube] abc123: Video unavailable")
    if scenario == "signin":
        if "--cookies" in args:
            return download(args)
        return fail("[youtube] abc123: Sign in to confirm your age. This video may be inappropriate for some users.")
    if scenario == "throttle-once":
        if first_time(token):
            return fail("unable to download video data: HTTP Error 429: Too Many Requests")
        return download(args)
    if scenario == "forbidden-once":
        # A stale download link: YouTube answers 403 until yt-dlp runs again and extracts fresh ones.
        if first_time(token):
            return fail(FORBIDDEN)
        return download(args)
    if scenario == "forbidden":
        return fail(FORBIDDEN)
    if scenario == "nsig":
        return fail("[youtube] abc123: nsig extraction failed: Some formats may be missing")
    if scenario == "format-gone":
        if arg_after(args, "-f") == "gone+140":
            return fail("[youtube] abc123: Requested format is not available. Use --list-formats for a list of available formats")
        return download(args)
    return fail(f"fake yt-dlp: unknown scenario {scenario!r}")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
