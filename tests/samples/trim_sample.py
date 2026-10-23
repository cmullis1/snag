"""Shrinks a `yt-dlp -J` dump (a video, or a playlist from `-J --flat-playlist`) to the fields Snag reads
(see snag/info/lookup.py and snag/info/formats.py), so the recorded sample stays small but still exercises
the real format and playlist logic.
Usage: python tests/samples/trim_sample.py <raw.json> <out.json>"""
import json
import sys

TOP_KEYS = ["_type", "id", "title", "channel", "uploader", "duration", "thumbnail", "upload_date",
            "extractor", "extractor_key", "webpage_url", "live_status", "chapters"]
FORMAT_KEYS = ["format_id", "ext", "protocol", "vcodec", "acodec", "height", "width", "fps",
               "abr", "tbr", "filesize", "filesize_approx", "format_note",
               "language", "language_preference", "dynamic_range"]
ENTRY_KEYS = ["_type", "ie_key", "id", "url", "webpage_url", "title", "duration", "thumbnail", "playlist_index"]
LIST_TYPES = ("playlist", "multi_video")


def _trim_format(fmt: dict) -> dict:
    return {key: fmt[key] for key in FORMAT_KEYS if key in fmt}


def _trim_entry(entry: dict | None) -> dict | None:
    if not entry:
        return entry  # yt-dlp lists unavailable entries as null
    out = {key: entry[key] for key in ENTRY_KEYS if key in entry}
    if entry.get("thumbnails"):
        out["thumbnails"] = [{"url": entry["thumbnails"][-1]["url"]}]  # Snag uses the last (largest) one
    return out


def trim(data: dict) -> dict:
    out = {key: data[key] for key in TOP_KEYS if key in data}
    for key in ("channel", "uploader"):
        if "@" in str(out.get(key) or ""):
            del out[key]  # some sites name the uploader by email address; it never goes into a sample
    if data.get("_type") in LIST_TYPES:
        out["entries"] = [_trim_entry(entry) for entry in data.get("entries") or []]
        return out
    out["subtitles"] = {lang: [] for lang in data.get("subtitles") or {}}
    out["automatic_captions"] = {lang: [] for lang in data.get("automatic_captions") or {}}
    out["formats"] = [_trim_format(f) for f in data.get("formats") or []]
    return out


if __name__ == "__main__":
    raw, target = sys.argv[1], sys.argv[2]
    with open(raw, encoding="utf-8") as source:
        data = json.load(source)
    with open(target, "w", encoding="utf-8") as sink:
        json.dump(trim(data), sink, indent=1, ensure_ascii=False)
