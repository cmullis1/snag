import io
from pathlib import Path

import pytest
from PIL import Image

from snag.platform.icon import BADGE, GLYPH, ICO_SIZES, ICON_FILE, TRAY_SIZES, draw_icon, tray_image, write_ico

LIGHT_TASKBAR = (243, 243, 243)
DARK_TASKBAR = (32, 32, 32)


def _luminance(rgb: tuple[int, ...]) -> float:
    def channel(value: int) -> float:
        c = value / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(v) for v in rgb[:3])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a: tuple[int, ...], b: tuple[int, ...]) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


@pytest.mark.parametrize("taskbar", [LIGHT_TASKBAR, DARK_TASKBAR])
def test_the_badge_stands_out_on_light_and_dark_taskbars(taskbar):
    assert _contrast(BADGE, taskbar) >= 3


def test_the_arrow_stands_out_on_the_badge():
    assert _contrast(GLYPH, BADGE) >= 4.5


@pytest.mark.parametrize("size", [16, 20, 24, 32, 48, 256])
def test_draws_a_square_rgba_image_of_the_size(size):
    image = draw_icon(size)
    assert image.mode == "RGBA"
    assert image.size == (size, size)


@pytest.mark.parametrize("size", [16, 32])
def test_the_arrow_is_crisp_at_tray_sizes(size):
    """The shaft is solid glyph colour down the middle, with badge colour either side of it: the
    geometry is snapped to whole pixels, so small sizes don't blur into a smudge."""
    image = draw_icon(size)
    middle = size // 2
    shaft_row = round(size * 0.3)
    assert image.getpixel((middle - 1, shaft_row)) == (*GLYPH, 255)
    assert image.getpixel((middle, shaft_row)) == (*GLYPH, 255)
    assert image.getpixel((2, shaft_row)) == (*BADGE, 255)
    assert image.getpixel((size - 3, shaft_row)) == (*BADGE, 255)


@pytest.mark.parametrize("size", [16, 32, 256])
def test_the_rounded_corners_are_transparent(size):
    image = draw_icon(size)
    for corner in ((0, 0), (size - 1, 0), (0, size - 1), (size - 1, size - 1)):
        assert image.getpixel(corner)[3] == 0


@pytest.mark.parametrize("size", [16, 32])
def test_the_drawing_is_symmetric(size):
    image = draw_icon(size)
    assert image.transpose(Image.Transpose.FLIP_LEFT_RIGHT).tobytes() == image.tobytes()


def test_write_ico_holds_every_size_drawn_for_that_size(tmp_path):
    ico = tmp_path / "snag.ico"
    write_ico(ico)
    with Image.open(ico) as image:
        assert set(image.info["sizes"]) == {(s, s) for s in ICO_SIZES}
        for size in (16, 32):
            assert image.ico.getimage((size, size)).convert("RGBA").tobytes() == draw_icon(size).tobytes()


def test_the_tray_image_saves_as_an_ico_of_frames_drawn_for_each_size():
    """pystray hands Windows the tray image by saving it as an .ico, with no options."""
    buffer = io.BytesIO()
    tray_image().save(buffer, format="ICO")
    buffer.seek(0)
    with Image.open(buffer) as image:
        assert set(image.info["sizes"]) == {(s, s) for s in TRAY_SIZES}
        for size in TRAY_SIZES:
            assert image.ico.getimage((size, size)).convert("RGBA").tobytes() == draw_icon(size).tobytes()


def test_the_committed_icon_matches_the_drawing():
    """snag/platform/snag.ico is generated: python -m snag.platform.icon rewrites it."""
    assert ICON_FILE == Path(__file__).resolve().parent.parent / "snag" / "platform" / "snag.ico"
    with Image.open(ICON_FILE) as image:
        assert set(image.info["sizes"]) == {(s, s) for s in ICO_SIZES}
        for size in ICO_SIZES:
            committed = image.ico.getimage((size, size)).convert("RGBA")
            drawn = draw_icon(size)
            # A little slack for resampling differences between Pillow versions.
            worst = max(abs(a - b) for a, b in zip(committed.tobytes(), drawn.tobytes()))
            assert worst <= 8, f"{size}px differs by {worst}; regenerate with python -m snag.platform.icon"


def test_the_tray_image_keeps_its_frames_when_saved_again():
    """The tray saves the image again whenever Windows asks for the icon anew (after a display
    change). Pillow 11.1 and 11.2 dropped the frames set on an image after its first save; from
    11.3 on they are kept."""
    image = tray_image()
    for _ in range(2):
        buffer = io.BytesIO()
        image.save(buffer, format="ICO")
        buffer.seek(0)
        with Image.open(buffer) as saved:
            assert set(saved.info["sizes"]) == {(s, s) for s in TRAY_SIZES}
