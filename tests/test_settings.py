import json
import logging
import threading
from dataclasses import fields
from pathlib import Path

import pytest

import snag.store.settings as settings_module
from snag.jobs.models import ChoiceKind, Extras, FormatChoice, SponsorBlockMode, SubtitleMode, extras_to_dict
from snag.store.db import open_db
from snag.store.settings import Settings, SettingsStore, default_dest, default_settings


@pytest.fixture
def conn(tmp_path):
    conn = open_db(tmp_path / "snag.db")
    yield conn
    conn.close()


@pytest.fixture
def defaults(tmp_path):
    return default_settings(tmp_path / "Videos", tmp_path / "Music")


@pytest.fixture
def store(conn, defaults):
    return SettingsStore(conn, defaults)


def put_raw(conn, key: str, raw: str) -> None:
    conn.execute("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, raw))


def test_defaults(tmp_path, store):
    settings = store.get()
    assert settings.video_dir == str(tmp_path / "Videos" / "Snag")
    assert settings.audio_dir == str(tmp_path / "Music" / "Snag")
    assert settings.max_concurrent == 5
    assert settings.quick_max_height is None  # no limit: best available (user decision 2026-10-07)
    assert settings.quick_extras == Extras(subtitles=True, thumbnail_tags=True, chapters=True)
    assert settings.default_extras.sponsorblock is False
    assert settings.default_extras.subtitle_mode is SubtitleMode.EMBEDDED
    assert settings.default_extras.sponsorblock_mode is SponsorBlockMode.REMOVE
    assert settings.check_updates_daily is True


def test_default_folders_come_from_the_known_folders(monkeypatch, tmp_path):
    monkeypatch.setattr(settings_module, "videos_folder", lambda: tmp_path / "OneDrive" / "Videos")
    monkeypatch.setattr(settings_module, "music_folder", lambda: tmp_path / "OneDrive" / "Music")
    settings = default_settings()
    assert settings.video_dir == str(tmp_path / "OneDrive" / "Videos" / "Snag")
    assert settings.audio_dir == str(tmp_path / "OneDrive" / "Music" / "Snag")


def test_update_persists(tmp_path, store):
    store.update(max_concurrent=3, quick_extras=Extras(sponsorblock=True))
    conn = open_db(tmp_path / "snag.db")
    reopened = SettingsStore(conn, default_settings(tmp_path / "Videos", tmp_path / "Music")).get()
    conn.close()
    assert reopened.max_concurrent == 3
    assert reopened.quick_extras == Extras(sponsorblock=True)


def test_update_returns_the_new_settings(store, tmp_path):
    folder = str(tmp_path / "Elsewhere")
    result = store.update(video_dir=folder, check_updates_daily=False)
    assert result.video_dir == folder and result.check_updates_daily is False
    assert result == store.get()


@pytest.mark.parametrize("changes", [
    {"max_concurrent": 0},
    {"max_concurrent": 11},
    {"max_concurrent": True},
    {"max_concurrent": 3.0},
    {"max_concurrent": "3"},
    {"quick_max_height": 999},
    {"quick_max_height": True},
    {"video_dir": ""},
    {"video_dir": "relative\\folder"},
    {"audio_dir": "C:drive-relative"},
    {"audio_dir": 5},
    {"check_updates_daily": 1},
    {"start_with_windows": False},  # Windows' own Startup shortcut says, not a stored value
    {"default_extras": "not extras"},
    {"default_extras": extras_to_dict(Extras())},
    {"quick_extras": Extras(sponsorblock_categories=("sponsor", "ads"))},
    {"quick_extras": Extras(sponsorblock=True, sponsorblock_categories=())},
    {"quick_extras": Extras(trim_start="1 minute")},
    {"quick_extras": Extras(subtitles=1)},
    {"no_such_setting": 1},
])
def test_invalid_updates_rejected(store, defaults, changes):
    with pytest.raises(ValueError):
        store.update(**changes)
    assert store.get() == defaults


def test_rejected_update_writes_nothing(store):
    with pytest.raises(ValueError):
        store.update(max_concurrent=3, quick_max_height=999)
    assert store.get().max_concurrent == 5


def test_start_with_windows_is_not_a_stored_setting(store, defaults, conn, caplog):
    """Windows' Startup shortcut is the truth (snag.platform.startup.starts_with_windows); a value an
    older Snag stored is ignored quietly."""
    assert "start_with_windows" not in {f.name for f in fields(defaults)}
    put_raw(conn, "start_with_windows", "false")
    with caplog.at_level(logging.WARNING, logger="snag.store.settings"):
        assert store.get() == defaults
    assert caplog.records == []


@pytest.mark.parametrize("key, raw", [
    ("max_concurrent", "true"),
    ("max_concurrent", "42"),
    ("max_concurrent", "{not json"),
    ("video_dir", '"relative"'),
    ("quick_extras", '{"subtitle_mode": "sideways"}'),
    ("quick_extras", '{"sponsorblock_categories": 5}'),
    ("quick_extras", '{"sponsorblock_categories": ["ads"]}'),
    ("quick_extras", '"subtitles please"'),
    pytest.param("max_concurrent", "[" * 100_000 + "]" * 100_000, id="max_concurrent-too-deeply-nested"),
    pytest.param("quick_extras", "[" * 100_000 + "]" * 100_000, id="quick_extras-too-deeply-nested"),
])
def test_bad_stored_value_falls_back_to_that_keys_default(store, defaults, conn, key, raw, caplog):
    store.update(check_updates_daily=False)
    put_raw(conn, key, raw)
    with caplog.at_level(logging.WARNING, logger="snag.store.settings"):
        settings = store.get()
    assert getattr(settings, key) == getattr(defaults, key)
    assert settings.check_updates_daily is False  # other keys unaffected
    assert key in caplog.text


def test_unknown_stored_key_is_ignored(store, defaults, conn, caplog):
    put_raw(conn, "retired_setting", "1")
    with caplog.at_level(logging.WARNING, logger="snag.store.settings"):
        assert store.get() == defaults
    assert "retired_setting" in caplog.text


def test_bad_stored_value_does_not_block_updates(store, conn):
    put_raw(conn, "max_concurrent", "{not json")
    assert store.update(quick_max_height=720).quick_max_height == 720
    # Writing the bad key itself replaces it.
    assert store.update(max_concurrent=2).max_concurrent == 2
    assert store.get().max_concurrent == 2


def test_extras_tolerate_older_stored_shapes(store, conn):
    data = extras_to_dict(Extras(sponsorblock=True))
    del data["trim_end"]
    data["retired_flag"] = True
    put_raw(conn, "quick_extras", json.dumps(data))
    assert store.get().quick_extras == Extras(sponsorblock=True)


def test_concurrent_updates_and_reads(store):
    errors: list[BaseException] = []

    def worker(n: int) -> None:
        try:
            for i in range(30):
                store.update(max_concurrent=1 + (n + i) % 10)
                assert isinstance(store.get(), Settings)
        except BaseException as error:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(error)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert 1 <= store.get().max_concurrent <= 10


def test_every_setting_is_validated():
    assert set(settings_module._VALIDATORS) == {f.name for f in fields(Settings)}


def test_default_dest(store):
    settings = store.get()
    assert default_dest(settings, FormatChoice(ChoiceKind.MP3)) == Path(settings.audio_dir)
    assert default_dest(settings, FormatChoice(ChoiceKind.QUICK, height=1080)) == Path(settings.video_dir)


# --- settings as plain JSON (what the UI sends and reads) ---

from snag.store.settings import settings_from_json, settings_to_json  # noqa: E402


def test_settings_from_json_turns_extras_objects_into_extras():
    extras = extras_to_dict(Extras(subtitles=True, subtitle_mode=SubtitleMode.FILE, sponsorblock_categories=("sponsor",)))
    changes = settings_from_json({"quick_extras": extras, "default_extras": {}, "max_concurrent": 3})
    assert changes == {
        "quick_extras": Extras(subtitles=True, subtitle_mode=SubtitleMode.FILE, sponsorblock_categories=("sponsor",)),
        "default_extras": Extras(),
        "max_concurrent": 3,
    }


@pytest.mark.parametrize("data", [{"quick_extras": "subtitles please"}, {"default_extras": {"subtitle_mode": "sideways"}}, []])
def test_settings_from_json_rejects_what_cannot_be_extras(data):
    with pytest.raises(ValueError):
        settings_from_json(data)


def test_update_from_json_saves_plain_json(store, tmp_path):
    sent = json.loads(json.dumps({
        "video_dir": str(tmp_path / "V"),
        "quick_extras": extras_to_dict(Extras(sponsorblock=True, sponsorblock_mode=SponsorBlockMode.MARK)),
        "check_updates_daily": False,
    }))
    settings = store.update_from_json(sent)
    assert settings.video_dir == str(tmp_path / "V")
    assert settings.quick_extras == Extras(sponsorblock=True, sponsorblock_mode=SponsorBlockMode.MARK)
    assert settings.check_updates_daily is False
    assert store.get() == settings


@pytest.mark.parametrize("data", [
    {"quick_extras": {"sponsorblock": True, "sponsorblock_categories": []}},  # validation is unchanged
    {"quick_extras": {"subtitles": "yes"}},
    {"max_concurrent": 11},
    {"retired_setting": 1},
])
def test_update_from_json_validates_and_saves_nothing_on_error(store, defaults, data):
    with pytest.raises(ValueError):
        store.update_from_json({"check_updates_daily": False, **data})
    assert store.get() == defaults


def test_settings_to_json_round_trips_through_real_json(store, tmp_path):
    store.update(audio_dir=str(tmp_path / "A"), default_extras=Extras(chapters=True, trim_start="5"))
    data = json.loads(json.dumps(settings_to_json(store.get())))
    assert data["default_extras"]["trim_start"] == "5"
    assert data["default_extras"]["sponsorblock_categories"] == list(Extras().sponsorblock_categories)
    assert settings_from_json(data) == {f.name: getattr(store.get(), f.name) for f in fields(Settings)}


def test_log_messages_start_with_a_capital(store, conn, caplog):
    put_raw(conn, "retired_setting", "1")
    put_raw(conn, "max_concurrent", "{not json")
    with caplog.at_level(logging.WARNING, logger="snag.store.settings"):
        store.get()
    assert len(caplog.records) == 2
    assert all(record.getMessage()[0].isupper() for record in caplog.records)


# --- remembered destination folders ---

from snag.store.settings import MAX_REMEMBERED_DIRS, remember_dir  # noqa: E402


def test_remembered_dirs_default_to_none(store):
    assert store.get().remembered_dirs == ()


def test_remember_dir_puts_the_newest_first(store, tmp_path):
    remember_dir(store, tmp_path / "A")
    settings = remember_dir(store, str(tmp_path / "B"))
    assert settings.remembered_dirs == (str(tmp_path / "B"), str(tmp_path / "A"))
    assert store.get() == settings


def test_remember_dir_moves_a_known_folder_to_the_front_ignoring_case(store, tmp_path):
    remember_dir(store, tmp_path / "Clips")
    remember_dir(store, tmp_path / "Other")
    settings = remember_dir(store, str(tmp_path / "CLIPS"))
    assert settings.remembered_dirs == (str(tmp_path / "CLIPS"), str(tmp_path / "Other"))


def test_remember_dir_ignores_a_trailing_separator(store, tmp_path):
    remember_dir(store, tmp_path / "Clips")
    settings = remember_dir(store, f"{tmp_path / 'Clips'}\\")
    assert settings.remembered_dirs == (str(tmp_path / "Clips"),)


def test_remember_dir_keeps_the_newest_twenty(store, tmp_path):
    for n in range(MAX_REMEMBERED_DIRS + 5):
        settings = remember_dir(store, tmp_path / f"D{n}")
    assert MAX_REMEMBERED_DIRS == 20
    assert settings.remembered_dirs == tuple(str(tmp_path / f"D{n}") for n in range(24, 4, -1))


def test_remember_dir_refuses_a_relative_folder(store):
    with pytest.raises(ValueError):
        remember_dir(store, "relative\folder")
    assert store.get().remembered_dirs == ()


def test_remember_dir_keeps_every_concurrent_folder(store, tmp_path):
    def worker(n: int) -> None:
        for i in range(5):
            remember_dir(store, tmp_path / f"T{n}-{i}")

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(store.get().remembered_dirs) == 20


@pytest.mark.parametrize("value", [
    [r"C:\Videos"],  # a list: the JSON shape goes through settings_from_json
    ("relative",),
    ("",),
    (5,),
    r"C:\Videos",
    tuple(rf"C:\D{n}" for n in range(21)),
    (r"C:\Same", r"c:\same"),
    (r"C:\Same", "C:\\Same\\"),
])
def test_invalid_remembered_dirs_rejected(store, value):
    with pytest.raises(ValueError):
        store.update(remembered_dirs=value)
    assert store.get().remembered_dirs == ()


def test_remembered_dirs_are_a_list_in_json(store, tmp_path):
    remember_dir(store, tmp_path / "A")
    data = json.loads(json.dumps(settings_to_json(store.get())))
    assert data["remembered_dirs"] == [str(tmp_path / "A")]
    assert settings_from_json(data)["remembered_dirs"] == (str(tmp_path / "A"),)


def test_update_from_json_sets_remembered_dirs(store, tmp_path):
    settings = store.update_from_json({"remembered_dirs": [str(tmp_path / "A"), str(tmp_path / "B")]})
    assert settings.remembered_dirs == (str(tmp_path / "A"), str(tmp_path / "B"))


@pytest.mark.parametrize("data", [{"remembered_dirs": r"C:\Videos"},{"remembered_dirs": ["relative"]}])
def test_update_from_json_rejects_bad_remembered_dirs(store, data):
    with pytest.raises(ValueError):
        store.update_from_json(data)


@pytest.mark.parametrize("raw", ['"C:\\Videos"', '["relative"]', "[5]", "{not json"])
def test_bad_stored_remembered_dirs_fall_back_to_none(store, conn, raw):
    put_raw(conn, "remembered_dirs", raw)
    assert store.get().remembered_dirs == ()


def test_stored_remembered_dirs_load_as_a_tuple(store, conn, tmp_path):
    put_raw(conn, "remembered_dirs", json.dumps([str(tmp_path / "A")]))
    assert store.get().remembered_dirs == (str(tmp_path / "A"),)


# --- the quick download's height cap: none by default (user decision 2026-10-07) ---

def test_quick_max_height_can_be_no_limit_again(store):
    store.update(quick_max_height=720)
    assert store.update(quick_max_height=None).quick_max_height is None
    assert store.get().quick_max_height is None


def test_quick_max_height_null_round_trips_through_json(store):
    data = json.loads(json.dumps(settings_to_json(store.get())))
    assert data["quick_max_height"] is None
    assert store.update_from_json({"quick_max_height": None}).quick_max_height is None
    assert store.update_from_json({"quick_max_height": 1440}).quick_max_height == 1440


def test_a_stored_1080_cap_stays_valid(store, conn):
    put_raw(conn, "quick_max_height", "1080")
    assert store.get().quick_max_height == 1080
    put_raw(conn, "quick_max_height", "null")
    assert store.get().quick_max_height is None


# --- what counts as a folder ---

@pytest.mark.parametrize("folder", [
    "C:\\Users\\me\\Videos", "D:\\", "\\\\nas\\media\\Videos", "//nas/media", "\\\\nas\\media",
    "C:\\Console\\Connie", "C:\\Videos\\nullable", "C:\\Clips\\com10",
])
def test_full_paths_and_network_shares_are_folders(folder):
    assert settings_module.folder_problem(folder) is None


@pytest.mark.parametrize("folder, words", [
    ("", "full path"), ("   ", "full path"), (5, "full path"), ("relative\\folder", "full path"),
    ("C:drive-relative", "full path"), ("\\Videos", "full path"), ("\\\\nas", "full path"),
    ("\\\\.\\C:\\Videos", "device"), ("\\\\?\\C:\\Videos", "device"), ("//./C:/Videos", "device"),
    ("\\\\?\\UNC\\nas\\media", "device"), ("\\??\\C:\\Videos", "device"), ("\\\\.\\PhysicalDrive0", "device"),
    ("C:\\Videos\\NUL", "NUL"), ("C:\\con.txt\\x", "con.txt"), ("C:\\a\\COM1", "COM1"), ("C:\\a\\lpt9 \\b", "lpt9"),
    ("C:\\a\\CONIN$", "CONIN$"), ("\\\\nas\\media\\AUX", "AUX"),
    ("C:\\a|b", "characters"), ("C:\\a\\b:stream", "characters"), ("C:\\a\x00b", "characters"),
    ("C:\\a\nb", "characters"),
    ("\\\\?\\GLOBALROOT\\Device\\HarddiskVolume1\\x", "device"), ("\\\\.\\GLOBALROOT\\Device\\Null", "device"),
    ("C:\\Videos\\aux.mp4\\x", "aux.mp4"), ("C:\\Videos\\name.", "dot or a space"),
    ("C:\\Videos\\name \\x", "dot or a space"), ("\\\\nas\\media\\clips.", "dot or a space"),
])
def test_device_and_relative_paths_are_not_folders(folder, words):
    problem = settings_module.folder_problem(folder)
    assert problem is not None and words in problem
    assert problem.endswith(".")


@pytest.mark.parametrize("folder, normal", [
    ("C:/Videos/", "C:\\Videos"), ("C:\\Videos\\.\\x\\..\\y", "C:\\Videos\\y"),
    ("\\\\nas\\media\\Clips\\", "\\\\nas\\media\\Clips"), ("D:\\", "D:\\"),
    ("\\\\nas\\media\\..\\..\\x", "\\\\nas\\media\\x"),  # never above the share
])
def test_validate_folder_gives_the_normal_path(folder, normal):
    assert settings_module.validate_folder(folder) == Path(normal)


@pytest.mark.parametrize("folder", ["relative", "\\\\?\\C:\\x", "C:\\a\x00", "C:\\NUL", "C:\\x."])
def test_validate_folder_raises_the_plain_sentence(folder):
    with pytest.raises(ValueError) as raised:
        settings_module.validate_folder(folder)
    assert str(raised.value) == settings_module.folder_problem(folder)


def test_remember_dir_tells_strasse_from_strasse_with_eszett(store):
    remember_dir(store, "C:\\Straße")
    assert remember_dir(store, "C:\\Strasse").remembered_dirs == ("C:\\Strasse", "C:\\Straße")
    assert remember_dir(store, "C:\\STRASSE").remembered_dirs == ("C:\\STRASSE", "C:\\Straße")


@pytest.mark.parametrize("key", ["video_dir", "audio_dir"])
def test_folders_on_a_network_share_are_kept(store, key):
    assert getattr(store.update(**{key: "\\\\nas\\media\\Snag"}), key) == "\\\\nas\\media\\Snag"


@pytest.mark.parametrize("changes", [
    {"video_dir": "\\\\.\\C:\\Videos"}, {"audio_dir": "\\\\?\\C:\\Music"},
    {"remembered_dirs": ("C:\\ok", "\\\\?\\C:\\Videos")},
])
def test_device_folders_are_refused(store, defaults, changes):
    with pytest.raises(ValueError, match="device"):
        store.update(**changes)
    assert store.get() == defaults


def test_remember_dir_refuses_a_device_and_keeps_a_share(store):
    with pytest.raises(ValueError):
        remember_dir(store, "\\\\.\\C:\\Videos")
    assert remember_dir(store, "\\\\nas\\media\\Clips").remembered_dirs == ("\\\\nas\\media\\Clips",)


def test_validate_changes_checks_without_saving(store, defaults):
    settings_module.validate_changes({"max_concurrent": 3, "video_dir": "\\\\nas\\media"})
    with pytest.raises(ValueError, match="max_concurrent"):
        settings_module.validate_changes({"max_concurrent": 30})
    with pytest.raises(ValueError, match="unknown"):
        settings_module.validate_changes({"nonsense": 1})
    assert store.get() == defaults
