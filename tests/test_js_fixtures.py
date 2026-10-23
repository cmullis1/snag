"""The JavaScript tests' fixtures made from the server's own rules stay current: result-exts.json
(tests/js/choice.test.js checks ui/choice.js resultExt against it) is regenerated in memory and must
match the committed file, so a change to the samples or the container rules fails here until
tests/js/fixtures/make_result_exts.py is run again."""
from __future__ import annotations

import importlib.util
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "js" / "fixtures"


def _maker():
    spec = importlib.util.spec_from_file_location("make_result_exts", FIXTURES / "make_result_exts.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_result_exts_fixture_is_current() -> None:
    maker = _maker()
    committed = maker.OUT.read_text(encoding="utf-8")  # read in text mode: a CRLF checkout compares the same
    assert maker.build() == committed, (
        "tests/js/fixtures/result-exts.json is out of date: run "
        ".venv/Scripts/python tests/js/fixtures/make_result_exts.py"
    )
