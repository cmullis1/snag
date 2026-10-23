"""pyproject.toml carries what the review rounds settled: the dependency bounds Snag's code relies
on, the icon shipped as package data, and the test markers."""
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet

ROOT = Path(__file__).resolve().parents[1]
PROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def requirement(name: str) -> Requirement:
    found = [Requirement(line) for line in PROJECT["project"]["dependencies"]]
    return next(req for req in found if req.name.lower() == name.lower())


def test_pillow_is_new_enough_to_keep_the_hand_drawn_icon_frames():
    assert requirement("Pillow").specifier == SpecifierSet(">=11.3")


def test_pystray_is_capped_below_0_20():
    """snag.platform.tray overrides a private pystray method; a new minor release may stop calling it."""
    assert requirement("pystray").specifier == SpecifierSet(">=0.19.5,<0.20")


def test_the_icon_is_package_data():
    assert "snag.ico" in PROJECT["tool"]["setuptools"]["package-data"]["snag.platform"]
    assert (ROOT / "snag" / "platform" / "snag.ico").is_file()


def test_the_slow_marker_is_registered():
    markers = PROJECT["tool"]["pytest"]["ini_options"]["markers"]
    assert any(marker.startswith("slow:") for marker in markers)
