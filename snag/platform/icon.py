"""Snag's icon: a white download arrow over a tray line, on a rounded blue badge.

The filled badge keeps the arrow readable on light and dark taskbars alike. Each size is drawn on
its own, with the shapes snapped to whole pixels of that size, so the 16 and 32 pixel icons (the
tray's sizes) stay crisp instead of being blurry shrinks of a large drawing.

snag/platform/snag.ico, used by Snag's shortcuts, is generated from this drawing; rewrite it after
changing the drawing with:  .venv/Scripts/python -m snag.platform.icon"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

BADGE = (36, 99, 228)
GLYPH = (255, 255, 255)
ICON_FILE = Path(__file__).resolve().with_name("snag.ico")
ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 256)

_SUPERSAMPLE = 8  # each pixel is drawn as 8x8 and averaged, which smooths the diagonal edges


def _even(value: float) -> int:
    """The nearest even whole number, so shapes centred on an even-sized icon sit on pixel edges."""
    return 2 * round(value / 2)


def draw_icon(size: int) -> Image.Image:
    """The icon at size x size pixels, RGBA with transparent corners."""
    k = _SUPERSAMPLE
    canvas = Image.new("RGBA", (size * k, size * k), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    def box(left: float, top: float, right: float, bottom: float) -> tuple[int, int, int, int]:
        # Pillow's rectangles include their last pixel; these coordinates are pixel edges.
        return round(left * k), round(top * k), round(right * k) - 1, round(bottom * k) - 1

    draw.rounded_rectangle(box(0, 0, size, size), radius=size * 0.2 * k, fill=BADGE)

    centre = size / 2
    shaft = max(2, _even(size * 0.22))
    head = max(6, _even(size * 0.58))
    shaft_top = round(size * 0.17)
    head_top = round(size * 0.42)
    tip = round(size * 0.70)
    line_top = round(size * 0.77)
    line_height = max(2, round(size * 0.09))
    line_inset = round(size * 0.22)

    draw.rectangle(box(centre - shaft / 2, shaft_top, centre + shaft / 2, head_top), fill=GLYPH)
    draw.polygon(
        [(round((centre - head / 2) * k), head_top * k), (round((centre + head / 2) * k), head_top * k),
         (round(centre * k), tip * k)],
        fill=GLYPH,
    )
    draw.rectangle(box(line_inset, line_top, size - line_inset, line_top + line_height), fill=GLYPH)

    # Pillow fills a polygon's two slanted edges slightly differently; the drawing is symmetric by
    # design, so mirror its left half to make it exactly so.
    half = canvas.width // 2
    canvas.paste(canvas.crop((0, 0, half, canvas.height)).transpose(Image.Transpose.FLIP_LEFT_RIGHT), (half, 0))

    return canvas.resize((size, size), Image.Resampling.BOX)


TRAY_SIZES = tuple(size for size in ICO_SIZES if size <= 64)


def tray_image() -> Image.Image:
    """The image for the tray icon. It carries a frame drawn for each size up to 64 pixels: the
    tray saves it as an .ico and has Windows load the small-icon size (16 pixels at 100% scaling,
    more on a scaled display; see snag.platform.tray), and Windows takes the frame drawn for that
    size, so the icon is crisp rather than shrunk. The frames reach the .ico through the image's
    encoderinfo, which Pillow 11.3 and later honour on every save, not just the first."""
    images = [draw_icon(size) for size in sorted(TRAY_SIZES, reverse=True)]
    image = images[0]
    image.encoderinfo = {"sizes": [i.size for i in images], "append_images": images[1:]}
    return image


def write_ico(path: Path) -> None:
    """A multi-size .ico holding the drawing at every size in ICO_SIZES."""
    images = [draw_icon(size) for size in sorted(ICO_SIZES, reverse=True)]
    images[0].save(path, format="ICO", sizes=[image.size for image in images], append_images=images[1:])


if __name__ == "__main__":
    write_ico(ICON_FILE)
    print(f"Wrote {ICON_FILE}")
