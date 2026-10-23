"""Runs the UI's JavaScript unit tests (tests/js, Node's built-in test runner), so one pytest command
covers both languages."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
JS_TESTS = "tests/js/*.test.js"  # Node expands the pattern itself (a bare folder isn't accepted)
# `node --test` expands glob patterns itself from Node 21 on; older versions take them as file names.
MIN_NODE_MAJOR = 21


def node_major(version_text: str) -> int | None:
    """The major version from `node --version` output ("v22.21.0" → 22); None when it isn't one."""
    match = re.fullmatch(r"\s*v?(\d+)\.\d+\.\d+\S*\s*", version_text)
    return int(match.group(1)) if match else None


def test_node_major_reads_node_versions() -> None:
    assert node_major("v22.21.0\n") == 22
    assert node_major("v21.0.0") == 21
    assert node_major("v18.19.1-nightly2024") == 18
    assert node_major("not node") is None
    assert node_major("") is None


def test_javascript_unit_tests() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip(f"node isn't on PATH, so the JavaScript tests in tests/js didn't run (install Node {MIN_NODE_MAJOR}+)")
    version = subprocess.run(
        [node, "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    ).stdout
    major = node_major(version)
    if major is None or major < MIN_NODE_MAJOR:
        pytest.skip(
            f"node {version.strip() or '(unknown version)'} is too old for the JavaScript tests in tests/js: "
            f"`node --test` needs Node {MIN_NODE_MAJOR}+ to expand {JS_TESTS} (install Node 22+)"
        )
    assert any((ROOT / "tests" / "js").glob("*.test.js")), "no JavaScript tests found in tests/js"
    result = subprocess.run(
        [node, "--test", "--test-reporter=spec", JS_TESTS],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
    )
    assert result.returncode == 0, f"JavaScript tests failed:\n{result.stdout}\n{result.stderr}"
