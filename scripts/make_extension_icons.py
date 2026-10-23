"""Writes the extension's icons (extension/icons/icon-<size>.png) from Snag's icon drawing, the one the
tray and the shortcuts use (snag/platform/icon.py). Each size is drawn on its own, so the small ones stay
crisp. Run it after changing the drawing; the tests fail while the committed icons are out of date:

    .venv/Scripts/python scripts/make_extension_icons.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # run as a script from anywhere
    sys.path.insert(0, str(ROOT))

from snag.platform.icon import draw_icon  # noqa: E402

ICON_DIR = ROOT / "extension" / "icons"
SIZES = (16, 32, 48, 128)


def write_icons(folder: Path = ICON_DIR) -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    written = []
    for size in SIZES:
        path = folder / f"icon-{size}.png"
        draw_icon(size).save(path, format="PNG", optimize=True)
        written.append(path)
    return written


def main() -> None:
    for path in write_icons():
        print(f"Wrote {path.relative_to(ROOT).as_posix()}")


if __name__ == "__main__":
    main()
