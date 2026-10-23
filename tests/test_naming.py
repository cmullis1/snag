import pytest

from snag.jobs.naming import playlist_dest, playlist_prefix, safe_name


def test_safe_name_replaces_illegal_characters():
    assert safe_name('a<b>c:"d/e\\f|g?h*') == "a_b_c__d_e_f_g_h_"


def test_safe_name_trims_and_falls_back():
    assert safe_name("  name.. ") == "name"
    assert safe_name("...") == "Untitled"
    assert safe_name("", fallback="Playlist") == "Playlist"


@pytest.mark.parametrize("name", [
    "CON", "CON.txt", "nul.tar", "CONIN$", "conout$.log", "COM¹", "lpt³.mp4", "con .mp4", "COM0", "LPT9.a.b", "Aux",
])
def test_safe_name_avoids_reserved_names(name):
    assert safe_name(name) == f"_{name}"


@pytest.mark.parametrize("name", ["CONSOLE", "COM10", "my con.mp4", "NULL.txt", "COM¹0", "x.CON"])
def test_safe_name_keeps_names_that_only_look_reserved(name):
    assert safe_name(name) == name


def test_playlist_prefix_pads_to_count():
    assert playlist_prefix(3, 12) == "03"
    assert playlist_prefix(7, 150) == "007"
    assert playlist_prefix(4, None) == "04"


def test_playlist_dest(tmp_path):
    assert playlist_dest(tmp_path, "My: List") == tmp_path / "My_ List"
