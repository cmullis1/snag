"""The extension folder as Chrome and Edge load it: the manifest, the icons, the synced copy of ui/, no
remote code, and the extension's own JavaScript tests (tests/js/extension)."""
from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

import snag
from snag.platform.icon import draw_icon
from snag.server.extension import EXTENSION_ID, EXTENSION_PUBLIC_KEY, extension_id_from_key
from tests.test_js import MIN_NODE_MAJOR, node_major

ROOT = Path(__file__).resolve().parent.parent
EXTENSION = ROOT / "extension"
MANIFEST = EXTENSION / "manifest.json"
SCRIPTS = ROOT / "scripts"
ICON_SIZES = (16, 32, 48, 128)
EXTENSION_JS_TESTS = "tests/js/extension/*.test.js"

# No "tabs": the host permissions already show web pages' addresses, and tabs.query({url}) finds an
# open Snag tab through the host permission for 127.0.0.1:47823. No "storage": nothing stores anything.
PERMISSIONS = ["contextMenus", "cookies", "notifications", "alarms", "activeTab"]
# Web pages only (cookies for whichever site a video is on), not file:// or other schemes; Snag's own
# address listed for what it is, though http://*/* covers it.
HOST_PERMISSIONS = ["http://*/*", "https://*/*", "http://127.0.0.1:47823/*"]


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


# ----- the manifest -----

def test_the_manifest_is_mv3_named_snag_at_snags_version(manifest):
    assert manifest["manifest_version"] == 3
    assert manifest["name"] == "Snag"
    assert manifest["version"] == snag.__version__


def test_the_key_gives_the_id_the_server_allows(manifest):
    assert manifest["key"] == EXTENSION_PUBLIC_KEY
    assert extension_id_from_key(manifest["key"]) == EXTENSION_ID


def test_permissions_are_exactly_what_snag_needs(manifest):
    assert manifest["permissions"] == PERMISSIONS
    assert manifest["host_permissions"] == HOST_PERMISSIONS
    assert "optional_permissions" not in manifest and "optional_host_permissions" not in manifest


def test_the_extension_is_kept_out_of_incognito_windows(manifest):
    # Private-window cookies must never reach Snag mixed in with the normal profile's.
    assert manifest["incognito"] == "not_allowed"


def test_the_background_is_a_module_service_worker(manifest):
    assert manifest["background"] == {"service_worker": "background.js", "type": "module"}


def test_the_youtube_content_script(manifest):
    assert manifest["content_scripts"] == [{
        "matches": ["https://www.youtube.com/*"],
        "js": ["content/youtube.js"],
        "run_at": "document_idle",
    }]


def test_icons_are_listed_at_every_size(manifest):
    expected = {str(size): f"icons/icon-{size}.png" for size in ICON_SIZES}
    assert manifest["icons"] == expected
    assert manifest["action"]["default_icon"] == expected
    assert manifest["action"]["default_title"] == "Snag"


def referenced_files(manifest: dict) -> list[str]:
    files = [manifest["background"]["service_worker"], *manifest["icons"].values()]
    files += manifest["action"]["default_icon"].values()
    if "default_popup" in manifest["action"]:
        files.append(manifest["action"]["default_popup"])
    for script in manifest["content_scripts"]:
        files += script.get("js", []) + script.get("css", [])
    return files


def test_the_toolbar_button_opens_the_popup(manifest):
    assert manifest["action"]["default_popup"] == "popup/popup.html"


def test_every_file_the_manifest_names_is_there(manifest):
    missing = [name for name in referenced_files(manifest) if not (EXTENSION / name).is_file()]
    assert missing == []


def test_no_content_security_policy_loosening(manifest):
    # The MV3 default (script-src 'self'; object-src 'self') is what Snag wants: no remote code, no eval.
    assert "content_security_policy" not in manifest
    assert "externally_connectable" not in manifest
    assert "web_accessible_resources" not in manifest


# ----- no remote code -----

REMOTE = r"""["'`](?:https?:)?//"""
REMOTE_CODE = [
    re.compile(rf"\bimport\s[^;]*?\bfrom\s*{REMOTE}"),          # import x from "https://..."
    re.compile(rf"\bimport\s*{REMOTE}"),                          # import "https://..."
    re.compile(rf"\bimport\s*\(\s*{REMOTE}"),                     # import("https://...")
    re.compile(r"\bimportScripts\s*\("),                          # classic-worker loading
    re.compile(rf"<script\b[^>]*\bsrc\s*=\s*{REMOTE}", re.I),     # <script src="https://...">
    re.compile(rf"<link\b[^>]*\bhref\s*=\s*{REMOTE}", re.I),      # remote stylesheets
    re.compile(rf"@import\s+(?:url\(\s*)?{REMOTE}", re.I),        # CSS @import of a remote sheet
    re.compile(r"\beval\s*\(|\bnew\s+Function\s*\("),             # code from strings
]


def remote_code(text: str) -> list[str]:
    return [match.group(0) for pattern in REMOTE_CODE for match in pattern.finditer(text)]


@pytest.mark.parametrize("text", [
    'import { x } from "https://cdn.example/x.js";',
    "import 'http://evil.example/a.js'",
    "await import(`https://cdn.example/m.js`)",
    "importScripts('lib.js')",
    '<script src="https://cdn.example/x.js"></script>',
    '<link rel="stylesheet" href="//fonts.example/a.css">',
    '@import url("https://fonts.example/a.css");',
    "eval(code)",
    "new Function('return 1')",
])
def test_remote_code_is_recognised(text):
    assert remote_code(text)


@pytest.mark.parametrize("text", [
    'import { createApi } from "../ui/api.js";',
    'export const SNAG_BASE = "http://127.0.0.1:47823";',
    '<script type="module" src="popup.js"></script>',
    "const evaluate = () => 1; evaluated(1);",
])
def test_local_code_is_not_flagged(text):
    assert remote_code(text) == []


def test_the_extension_loads_no_remote_code():
    found = {
        str(path.relative_to(EXTENSION)): hits
        for path in sorted(EXTENSION.rglob("*"))
        if path.suffix in (".js", ".mjs", ".html", ".css") and (hits := remote_code(path.read_text(encoding="utf-8")))
    }
    assert found == {}


# ----- the popup page and the modules the extension loads -----

POPUP = EXTENSION / "popup" / "popup.html"
SCRIPT_TAG = re.compile(r"<script\b([^>]*)>(.*?)</script>", re.I | re.S)
STYLESHEET = re.compile(r"""<link\b[^>]*\brel\s*=\s*["']stylesheet["'][^>]*\bhref\s*=\s*["']([^"']+)["']""", re.I)
INLINE_HANDLER = re.compile(r"""<[^>]*\son[a-z]+\s*=""", re.I)
# Static imports, re-exports and dynamic imports with a literal specifier.
MODULE_SPECIFIER = re.compile(r"""(?:\bimport\s[^;]*?\bfrom|\bexport\s[^;]*?\bfrom|\bimport)\s*\(?\s*["']([^"']+)["']""")


def test_the_popup_loads_one_module_script_and_no_inline_code():
    # The MV3 extension-page policy (script-src 'self') runs no inline script or handler attribute.
    page = POPUP.read_text(encoding="utf-8")
    scripts = SCRIPT_TAG.findall(page)
    assert [(re.search(r'type\s*=\s*"module"', attrs) is not None, body.strip()) for attrs, body in scripts] == [(True, "")]
    assert re.search(r'src\s*=\s*"popup\.js"', scripts[0][0])
    assert not INLINE_HANDLER.search(page)
    assert STYLESHEET.findall(page) == ["../ui/base.css", "popup.css"]
    for href in STYLESHEET.findall(page):
        assert (POPUP.parent / href).is_file(), href


def test_the_popup_harness_is_the_popup_page():
    # tests/js/extension/harness/harness.html stands in for popup.html in an ordinary tab (served by
    # serve.py, with the extension folder at /extension/): the same stylesheets and body.
    harness = (ROOT / "tests" / "js" / "extension" / "harness" / "harness.html").read_text(encoding="utf-8")
    popup = POPUP.read_text(encoding="utf-8")
    served = [f"/extension/{(POPUP.parent / href).resolve().relative_to(EXTENSION.resolve()).as_posix()}"
              for href in STYLESHEET.findall(popup)]
    assert STYLESHEET.findall(harness) == served
    assert re.search(r"<body\b[^>]*>", harness).group(0) == re.search(r"<body\b[^>]*>", popup).group(0)


def module_imports(path: Path) -> list[str]:
    return MODULE_SPECIFIER.findall(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("text, expected", [
    ('import { a } from "./a.js";', ["./a.js"]),
    ('import {\n  a,\n  b,\n} from "../ui/b.js";', ["../ui/b.js"]),
    ("import './side-effect.js';", ["./side-effect.js"]),
    ('export { x } from "./x.js";', ["./x.js"]),
    ('const m = await import("./lazy.js");', ["./lazy.js"]),
])
def test_module_specifiers_are_found(tmp_path, text, expected):
    path = tmp_path / "m.js"
    path.write_text(text, encoding="utf-8")
    assert module_imports(path) == expected


def test_every_module_the_extension_imports_is_inside_it():
    # The browser serves only the extension's own folder: a path out of it (../../ui/) fails to load,
    # and a bare name ("lodash") has nothing to resolve it.
    root = EXTENSION.resolve()
    problems = []
    for path in sorted(EXTENSION.rglob("*.js")):
        for specifier in module_imports(path):
            if not specifier.startswith(("./", "../")):
                problems.append(f"{path.relative_to(EXTENSION)}: {specifier}")
                continue
            target = (path.parent / specifier).resolve()
            if not target.is_relative_to(root) or not target.is_file():
                problems.append(f"{path.relative_to(EXTENSION)}: {specifier}")
    assert problems == []


# ----- icons -----

@pytest.mark.parametrize("size", ICON_SIZES)
def test_icons_are_the_tray_drawing_at_each_size(size):
    with Image.open(EXTENSION / "icons" / f"icon-{size}.png") as image:
        assert image.format == "PNG"
        assert image.size == (size, size)
        assert image.convert("RGBA").tobytes() == draw_icon(size).tobytes(), \
            "the icons are out of date: run .venv/Scripts/python scripts/make_extension_icons.py"


def test_the_icon_script_writes_each_size(tmp_path):
    written = load_script("make_extension_icons").write_icons(tmp_path)
    assert sorted(path.name for path in written) == sorted(f"icon-{size}.png" for size in ICON_SIZES)
    for size in ICON_SIZES:
        with Image.open(tmp_path / f"icon-{size}.png") as image:
            assert image.size == (size, size) and image.mode == "RGBA"


# ----- the synced copy of ui/ -----

def make_tree(root: Path, files: dict[str, bytes]) -> None:
    for name, data in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def tree(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_sync_copies_new_and_changed_files_and_deletes_stale_ones(tmp_path):
    sync = load_script("sync_ui")
    source, target = tmp_path / "ui", tmp_path / "extension" / "ui"
    make_tree(source, {"api.js": b"new api\r\n", "components/el.js": b"el", "base.css": b"css"})
    make_tree(target, {"api.js": b"old api\n", "stale.js": b"gone", "old/deep.js": b"gone", "base.css": b"css"})
    assert sync.differences(source, target) == ["api.js", "components/el.js", "old/deep.js", "stale.js"]

    sync.sync(source, target)

    assert tree(target) == tree(source)
    assert sync.differences(source, target) == []
    assert not (target / "old").exists()


def test_line_endings_alone_are_not_a_difference_for_text_files(tmp_path):
    # Git with core.autocrlf writes CRLF on checkout and commits LF either way.
    sync = load_script("sync_ui")
    source, target = tmp_path / "ui", tmp_path / "extension" / "ui"
    make_tree(source, {"api.js": b"a\r\nb\r\n", "base.css": b"x\r\n", "logo.png": b"\x89PNG\r\n"})
    make_tree(target, {"api.js": b"a\nb\n", "base.css": b"x\n", "logo.png": b"\x89PNG\n"})
    assert sync.differences(source, target) == ["logo.png"]


def test_sync_makes_the_target_when_it_is_missing(tmp_path):
    sync = load_script("sync_ui")
    source, target = tmp_path / "ui", tmp_path / "out" / "ui"
    make_tree(source, {"a.js": b"a"})
    sync.sync(source, target)
    assert tree(target) == {"a.js": b"a"}


def names(root: Path) -> list[str]:
    """The files under root as the file system spells them (case included)."""
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())


@pytest.mark.parametrize(("old", "new"), [
    ("api.js", "API.js"),                        # sorts before the old name
    ("API.js", "api.js"),                        # sorts after it
    ("components/el.js", "Components/el.js"),    # a folder renamed
    ("Components/el.js", "components/el.js"),
])
def test_sync_follows_a_rename_that_only_changes_case(tmp_path, old, new):
    # On Windows the two names are the same file: copying first and then deleting the old name would
    # delete the new copy.
    sync = load_script("sync_ui")
    source, target = tmp_path / "ui", tmp_path / "extension" / "ui"
    make_tree(source, {new: b"new", "keep.js": b"keep"})
    make_tree(target, {old: b"old", "keep.js": b"keep"})
    sync.sync(source, target)
    assert names(target) == sorted([new, "keep.js"])
    assert (target / new).read_bytes() == b"new"
    assert sync.differences(source, target) == []


@pytest.mark.parametrize("make_source", [
    lambda source: None,                                # missing
    lambda source: source.write_bytes(b"not a folder"),  # a file
])
def test_sync_refuses_a_source_that_isnt_a_folder_and_leaves_the_copy_alone(tmp_path, make_source):
    sync = load_script("sync_ui")
    source, target = tmp_path / "ui", tmp_path / "extension" / "ui"
    make_source(source)
    make_tree(target, {"api.js": b"api"})
    with pytest.raises(NotADirectoryError, match="ui"):
        sync.sync(source, target)
    with pytest.raises(NotADirectoryError):
        sync.differences(source, target)
    assert tree(target) == {"api.js": b"api"}


def test_extension_ui_is_identical_to_ui():
    sync = load_script("sync_ui")
    differing = sync.differences(ROOT / "ui", EXTENSION / "ui")
    assert differing == [], (
        f"extension/ui differs from ui/ in {', '.join(differing)}: "
        "run .venv/Scripts/python scripts/sync_ui.py"
    )


# ----- the extension's JavaScript tests -----

def test_extension_javascript_unit_tests() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip(f"node isn't on PATH, so the extension's JavaScript tests didn't run (install Node {MIN_NODE_MAJOR}+)")
    version = subprocess.run(
        [node, "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    ).stdout
    major = node_major(version)
    if major is None or major < MIN_NODE_MAJOR:
        pytest.skip(f"node {version.strip() or '(unknown version)'} is too old for {EXTENSION_JS_TESTS} "
                    f"(install Node {MIN_NODE_MAJOR}+)")
    assert any((ROOT / "tests" / "js" / "extension").glob("*.test.js")), "no tests found in tests/js/extension"
    result = subprocess.run(
        [node, "--test", "--test-reporter=spec", EXTENSION_JS_TESTS],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
    )
    assert result.returncode == 0, f"the extension's JavaScript tests failed:\n{result.stdout}\n{result.stderr}"
