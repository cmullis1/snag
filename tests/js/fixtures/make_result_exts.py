"""Writes result-exts.json: for every format row in the recorded yt-dlp samples (tests/samples), with
its own audio and with every other audio track, the containers the server says the finished file can
have (snag.jobs.command.result_exts of snag.jobs.models.choice_for_row). tests/js/choice.test.js checks
ui/choice.js resultExt against it, so the UI's File column keeps matching the server's merge rule.

Regenerate after changing the samples or the container rules (snag/info/containers.py):

    .venv/Scripts/python tests/js/fixtures/make_result_exts.py
"""
from __future__ import annotations

import json
from pathlib import Path

from snag.info.lookup import classify_url, parse_info
from snag.info.models import FormatRow, VideoInfo
from snag.jobs.command import result_exts
from snag.jobs.models import choice_for_row
from snag.server.schemas import format_row_json

HERE = Path(__file__).resolve().parent
SAMPLES = HERE.parent.parent / "samples"
OUT = HERE / "result-exts.json"
# The untrimmed dump trim_sample.py reads: git ignores it, so it exists only on the PC that recorded it.
UNTRIMMED = "raw.json"

# The FormatRow fields the server's rule reads (choice_for_row, result_exts), as the UI receives them.
ROW_FIELDS = (
    "format_id", "kind", "ext", "vcodec", "acodec", "has_audio", "audio_id", "audio_ext", "audio_codec", "result_ext",
)


def _video(path: Path) -> VideoInfo | None:
    data = json.loads(path.read_text(encoding="utf-8"))
    url = data.get("webpage_url") if isinstance(data, dict) else None
    if not url or not data.get("formats"):
        return None
    info = parse_info(data, url, classify_url(url))
    return info if isinstance(info, VideoInfo) else None


def _slim(row: FormatRow) -> dict:
    data = format_row_json(row)
    return {field: data[field] for field in ROW_FIELDS}


def _sample(info: VideoInfo) -> dict:
    """{rows: {id: row}, tracks: {id: track}, cases: [[row id, track id | None, [ext, ...]]]}."""
    formats = info.formats
    rows = {row.format_id: row for row in (*formats.video_all, *formats.video_simple, *formats.audio)}
    tracks = {track.format_id: track for track in formats.audio_tracks}
    cases = []
    for row in rows.values():
        choices: list[FormatRow | None] = [None]
        if row.kind == "video" and not row.has_audio:
            choices += list(tracks.values())
        for track in choices:
            exts = sorted(result_exts(choice_for_row(row, track)))
            cases.append([row.format_id, track.format_id if track is not None else None, exts])
    return {
        "rows": {key: _slim(row) for key, row in rows.items()},
        "tracks": {key: _slim(track) for key, track in tracks.items()},
        "cases": cases,
    }


def _compact(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _entries(items: list[str], indent: str) -> list[str]:
    return [f"{indent}{item}{',' if index < len(items) - 1 else ''}" for index, item in enumerate(items)]


def _dump(samples: dict) -> str:
    """JSON with one row, track or case per line, so a regenerated fixture diffs readably."""
    blocks = []
    for name, sample in samples.items():
        rows = [f"{_compact(key)}: {_compact(row)}" for key, row in sample["rows"].items()]
        tracks = [f"{_compact(key)}: {_compact(track)}" for key, track in sample["tracks"].items()]
        cases = [_compact(case) for case in sample["cases"]]
        blocks.append("\n".join([
            f" {_compact(name)}: {{",
            '  "rows": {', *_entries(rows, "   "), "  },",
            '  "tracks": {', *_entries(tracks, "   "), "  },",
            '  "cases": [', *_entries(cases, "   "), "  ]",
            " }",
        ]))
    return "{\n" + ",\n".join(blocks) + "\n}\n"


def _samples() -> dict:
    samples = {}
    for path in sorted(p for p in SAMPLES.glob("*.json") if p.name != UNTRIMMED):
        info = _video(path)
        if info is not None:
            samples[path.stem] = _sample(info)
    return samples


def build() -> str:
    """result-exts.json's text as the samples and the server's rule make it now (tests/test_js_fixtures.py
    checks the committed file against it)."""
    return _dump(_samples())


def main() -> None:
    samples = _samples()
    OUT.write_text(_dump(samples), encoding="utf-8", newline="\n")
    count = sum(len(sample["cases"]) for sample in samples.values())
    print(f"wrote {count} cases from {', '.join(samples)} to {OUT.name}")


if __name__ == "__main__":
    main()
