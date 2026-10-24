import json
from dataclasses import fields, replace

import pytest

from octantcompass.config import DEFAULTS, EDITABLE, RELOADED, Config, LiveConfig, read_settings, same_value, save_setting


def test_the_defaults_come_from_the_bundled_file(tmp_path):
    """defaults.json names every setting with its default, and settings.json is read over it."""
    assert set(DEFAULTS) == {f.name for f in fields(Config)} - {"places"}
    config = Config(tmp_path)
    assert {key: getattr(config, key) for key in DEFAULTS if key != "db"} == {k: v for k, v in DEFAULTS.items() if k != "db"}
    assert DEFAULTS["db"] is None and config.db == tmp_path / "db"
    (tmp_path / "settings.json").write_text(json.dumps({"port": 4000, "unknown_key": 1}))
    loaded = Config.load(tmp_path)
    assert loaded.port == 4000 and loaded.host == DEFAULTS["host"] and loaded.log_lines == DEFAULTS["log_lines"]


def test_settings_json_keeps_only_what_the_user_changed(tmp_path):
    data = tmp_path / "data"  # without the settings conftest writes for hosts
    settings = data / "settings.json"
    save_setting(data, "port", DEFAULTS["port"])
    assert not settings.exists()  # nothing changed, so nothing is written
    save_setting(data, "port", 4000)
    save_setting(data, "idle_minutes", 10.0)
    save_setting(data, "allow_unsigned_db", True)
    assert read_settings(data) == {"port": 4000, "idle_minutes": 10.0, "allow_unsigned_db": True}
    # Saved back to the default (as the page writes it: 30.0 for 30), a key leaves the file.
    save_setting(data, "idle_minutes", float(DEFAULTS["idle_minutes"]))
    save_setting(data, "allow_unsigned_db", DEFAULTS["allow_unsigned_db"])
    assert read_settings(data) == {"port": 4000}
    assert same_value(30, 30.0) and not same_value(True, 1) and not same_value(False, 0)
    # Loading reads the file over the defaults and never writes it.
    text = settings.read_text()
    assert Config.load(data).port == 4000 and settings.read_text() == text


def test_internal_waits_are_changed_in_settings_json_only(tmp_path):
    waits = {"health_timeout", "poll_seconds", "startup_timeout", "shutdown_timeout"}
    assert waits <= set(DEFAULTS) and not waits & set(EDITABLE)
    # A restart from the page reads them again, like every setting but the DB folder (the DB switch's).
    live = LiveConfig(replace(Config.load(tmp_path), db=tmp_path / "selected"))
    (tmp_path / "settings.json").write_text(json.dumps({"poll_seconds": 0.25, "startup_timeout": 3, "db": "/elsewhere"}))
    current = live.reload(RELOADED)
    assert (current.poll_seconds, current.startup_timeout, current.db) == (0.25, 3, tmp_path / "selected")


def test_the_index_size_cap_is_a_setting(tmp_path):
    assert Config(tmp_path).index_max_bytes == DEFAULTS["index_max_megabytes"] * 1024 * 1024
    assert Config(tmp_path, index_max_megabytes=0.5).index_max_bytes == 524288
    for bad in (0, -1, float("inf"), "16", True):
        with pytest.raises(ValueError):
            Config(tmp_path, index_max_megabytes=bad)
