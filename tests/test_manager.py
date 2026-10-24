import json
import socket
import sys

import pytest

from octantcompass import shortcut
from octantcompass.config import DEFAULTS, EDITABLE, Config
from octantcompass.engine import DBError, summary
from octantcompass.engine.fake import FakeVectorEngine
from octantcompass.lifecycle import connection_path
from octantcompass.manager import Manager, Refused
from octantcompass.places import Places

from test_lifecycle import await_connection, server  # noqa: F401 (fixture)


def manager_for(data_dir, engine=None):
    """The window's side of a OctantCompass kept in data_dir; a host it starts uses the synthetic DB, and so does the
    DB section."""
    return Manager(Places.at(data_dir), ["--fake-engine"], engine or FakeVectorEngine())


def without_db(status):
    return {key: status[key] for key in ("running", "starting", "start", "host")}


def stop(manager):
    code, _ = manager.host("/manage/stop", {})
    assert code == 200


def test_settings_change_while_the_host_is_stopped(tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps({"trusted_keys": {"10c26904c2f7bb19": "c3ludGhldGlj"},
                                                         "unknown_key": "kept"}))
    manager = manager_for(tmp_path)
    assert without_db(manager.status()) == {"running": False, "starting": False, "start": None, "host": None}
    view = manager.settings()
    assert view["running"] is False and view["readable"] is True and view["path"] == str(tmp_path / "settings.json")
    rows = {r["key"]: r for r in view["settings"]}
    # Only what a user changes; the internal waits stay in settings.json.
    assert list(rows) == list(EDITABLE)
    assert rows["trusted_keys"]["saved"] == ["10c26904c2f7bb19"]
    assert rows["trusted_keys"]["edit"] == "10c26904c2f7bb19 c3ludGhldGlj"
    assert rows["host"] == {"key": "host", "group": "listen", "value": None, "saved": "127.0.0.1",
                            "edit": "127.0.0.1", "default": "127.0.0.1"}
    # Saved here, used when the host starts: nothing runs to apply them to.
    for key, value in (("host", "0.0.0.0"), ("port", "38123"), ("allow_unsigned_db", "true"),
                       ("trusted_keys", "aaaa AAAA\nbbbb BBBB"), ("index_url", "/synthetic/index.json"),
                       ("pair_code_digits", "8"), ("log_lines", "3"), ("pair_code_seconds", "300"),
                       ("idle_minutes", "30")):
        assert manager.save_setting(key, value) == {"saved": key, "applied": False}, key
    for key, value in (("host", "not an address"), ("port", "70000"), ("port", "x"), ("allow_unsigned_db", "yes"),
                       ("trusted_keys", "only-an-id"), ("unknown_key", "changed"), ("db", "/elsewhere"),
                       ("poll_seconds", "1")):
        with pytest.raises(Refused) as refused:
            manager.save_setting(key, value)
        assert refused.value.code == "invalid_setting", key
    # The defaults (pair_code_seconds, idle_minutes) stay out of the file, and so do the ones it had before.
    assert json.loads((tmp_path / "settings.json").read_text()) == {
        "trusted_keys": {"aaaa": "AAAA", "bbbb": "BBBB"}, "unknown_key": "kept", "host": "0.0.0.0", "port": 38123,
        "allow_unsigned_db": True, "index_url": "/synthetic/index.json", "pair_code_digits": 8, "log_lines": 3}
    assert manager.save_setting("host", "") == {"saved": "host", "applied": False}
    assert "host" not in json.loads((tmp_path / "settings.json").read_text())


def test_the_db_section_works_while_the_host_is_stopped(tmp_path):
    """What the DB in use says, from its manifest (nothing is loaded), and a switch handed to the host for its next
    start (2026-10-19: the window gives the DB's place to OctantVector, so it needs no running host)."""
    info = {**FakeVectorEngine().info, "dbVersion": "2026.10.01", "builtAt": "2026-10-01T00:00:00Z",
            "views": {"tags": {"credits": ["BeatSaver"]}, "audio": {}}, "notice": "# Terms"}
    engine = FakeVectorEngine(info=info)
    manager = manager_for(tmp_path, engine)
    status = manager.status()
    assert status["running"] is False and status["window"]["dataDir"] == str(tmp_path)
    assert status["db"] == {"path": str(tmp_path / "db"), "info": summary(info), "problem": None, "pending": None,
                            "work": None, "indexUrl": None}
    assert summary(info)["views"] == ["tags", "audio"] and summary(info)["credits"] == {"tags": ["BeatSaver"]}
    assert engine.calls == [("read_info", tmp_path / "db")]

    other = tmp_path / "other"
    other.mkdir()
    assert manager.switch_db(str(other)) == {"dbVersion": "2026.10.01", "path": str(other)}
    # The DB in use stays until the host adopts the new one (at its next start); the section shows both.
    db = manager.status()["db"]
    assert db["path"] == str(tmp_path / "db") and db["pending"] == str(other) and db["work"] is None
    with pytest.raises(Refused) as refused:
        manager.switch_db("other")
    assert refused.value.code == "path_not_absolute"
    engine.error = DBError("synthetic")
    with pytest.raises(Refused) as refused:
        manager.switch_db(str(other))
    assert refused.value.code == "db_switch_failed"
    assert manager.status()["db"]["problem"] == "db_invalid"

    # An update needs an https index: the setting's, unless one is given.
    for index, code in ((None, "no_index"), ("http://example.invalid/index.json", "index_not_https")):
        with pytest.raises(Refused) as refused:
            manager.check_update(index)
        assert refused.value.code == code, index
    manager.save_setting("index_url", "https://example.invalid/index.json")
    assert manager.status()["db"]["indexUrl"] == "https://example.invalid/index.json"
    assert manager.cancel_update() == {"cancelled": False} and manager.busy() is False


def test_a_settings_file_that_does_not_load(tmp_path):
    (tmp_path / "settings.json").write_text('{"port": 70000}')
    manager = manager_for(tmp_path)
    view = manager.settings()
    assert view["readable"] is False and all(row["saved"] is None and row["edit"] == "" for row in view["settings"])
    with pytest.raises(Refused):
        manager.save_setting("log_lines", "3")
    # Nothing is started with it: the reason is the file.
    result = manager.start()
    assert result["ok"] is False and result["problem"].startswith("settings.json: ") and "port" in result["problem"]
    assert manager.status()["start"] == result and not connection_path(tmp_path).exists()


def test_the_host_api_through_the_window(tmp_path):
    manager = manager_for(tmp_path)
    with pytest.raises(Refused) as refused:
        manager.host("/manage/status")
    assert (refused.value.code, refused.value.status) == ("not_running", 503)
    with pytest.raises(Refused) as refused:
        manager.host("/v1/health")  # only the management API passes through
    assert refused.value.code == "not_found"


def test_start_says_why_the_host_stopped_and_starts_once_the_setting_is_fixed(tmp_path):
    manager = manager_for(tmp_path)
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        held = taken.getsockname()[1]
        manager.save_setting("port", str(held))
        result = manager.start()
        assert result["ok"] is False and f"could not start or complete the command: 127.0.0.1:{held}: " in result["problem"]
        assert without_db(manager.status()) == {"running": False, "starting": False, "start": result, "host": None}
    # The setting is fixed in the window while the host is stopped; the next start uses it.
    manager.save_setting("port", "0")
    try:
        assert manager.start() == {"ok": True, "problem": None}
        status = manager.status()
        assert status["running"] is True and status["host"]["status"] == "ready" and status["host"]["listen"]
        # It keeps running on its own: the window does not hold it.
        assert manager.start() == {"ok": True, "problem": None}
        # Saved while it runs: used at once, except the address and port (a restart).
        assert manager.save_setting("log_lines", "3") == {"saved": "log_lines", "applied": True}
        assert manager.save_setting("port", "0") == {"saved": "port", "applied": False}
        rows = {r["key"]: r for r in manager.settings()["settings"]}
        assert manager.settings()["running"] is True and rows["log_lines"]["value"] == 3 == rows["log_lines"]["saved"]
    finally:
        stop(manager)
    process = manager.process
    assert process is not None and process.wait(timeout=15) == 0
    assert manager.status()["running"] is False and manager.process is None


def test_start_finds_a_host_already_running(tmp_path, server):  # noqa: F811
    process = server("--fake-engine")
    await_connection(tmp_path, process)
    manager = manager_for(tmp_path)
    assert manager.start() == {"ok": True, "problem": None} and manager.process is None
    assert manager.status()["host"]["pid"] == process.pid
    stop(manager)
    assert process.wait(timeout=15) == 0


@pytest.mark.skipif(sys.platform in ("win32", "darwin"), reason="the Linux login entry")
def test_start_at_login_is_off_by_default_and_follows_the_setting(tmp_path, login_entries, systemctl, monkeypatch):
    unit = login_entries / "config/systemd/user/octantcompass.service"
    enabled = unit.parent / "default.target.wants/octantcompass.service"
    assert Config(tmp_path).start_at_login is False
    manager = manager_for(tmp_path)
    rows = {r["key"]: r for r in manager.settings()["settings"]}
    assert rows["start_at_login"] == {"key": "start_at_login", "group": "running", "value": None, "saved": False,
                                      "edit": "false", "default": DEFAULTS["start_at_login"]}
    assert not unit.exists()
    assert manager.save_setting("start_at_login", "true") == {"saved": "start_at_login", "applied": False}
    text = unit.read_text()
    # At login: serve, so no window; this data folder, since it is not the default one. A systemd user unit, so
    # `systemctl --user stop octantcompass` stops it.
    assert f"ExecStart={sys.executable} -m octantcompass serve --data-dir {tmp_path}" in text
    assert "WantedBy=default.target" in text and enabled.is_symlink()
    assert systemctl.calls == [("daemon-reload",), ("enable", "octantcompass.service")]
    assert json.loads((tmp_path / "settings.json").read_text())["start_at_login"] is True
    with pytest.raises(Refused):
        manager.save_setting("start_at_login", "yes")
    manager.save_setting("start_at_login", "")
    assert not unit.exists() and not enabled.exists()
    assert ("disable", "octantcompass.service") in systemctl.calls
    assert "start_at_login" not in json.loads((tmp_path / "settings.json").read_text())

    # If the entry cannot be written, the setting stays as it was.
    def fail(data_dir, on):
        raise RuntimeError("synthetic")
    monkeypatch.setattr(shortcut, "set_start_at_login", fail)
    with pytest.raises(Refused) as refused:
        manager.save_setting("start_at_login", "true")
    assert (refused.value.code, refused.value.status) == ("start_at_login_failed", 500)
    assert "start_at_login" not in json.loads((tmp_path / "settings.json").read_text())


@pytest.mark.skipif(sys.platform in ("win32", "darwin"), reason="the Linux login entry")
def test_start_at_login_uses_autostart_without_a_systemd_user_manager(tmp_path, login_entries, systemctl):
    unit = login_entries / "config/systemd/user/octantcompass.service"
    entry = login_entries / "config/autostart/octantcompass.desktop"
    systemctl.available = False
    assert shortcut.set_start_at_login(tmp_path, True) == entry
    assert f"Exec={sys.executable} -m octantcompass serve --data-dir {tmp_path}" in entry.read_text()
    assert not unit.exists()
    # With a user manager the unit replaces the autostart entry, so OctantCompass does not start twice.
    systemctl.available = True
    assert shortcut.set_start_at_login(tmp_path, True) == unit and not entry.exists()
    shortcut.set_start_at_login(tmp_path, False)
    assert not unit.exists() and not entry.exists()


@pytest.mark.skipif(sys.platform in ("win32", "darwin"), reason="the Linux login entry")
def test_serve_writes_the_login_entry_again_when_the_setting_is_on(tmp_path, login_entries, systemctl, monkeypatch):
    import octantcompass.cli as cli
    unit = login_entries / "config/systemd/user/octantcompass.service"
    async def serve(config, factory, **_):
        return 0
    monkeypatch.setattr(cli, "serve", serve)
    assert cli.main(["serve", "--fake-engine", "--data-dir", str(tmp_path)]) == 0 and not unit.exists()
    (tmp_path / "settings.json").write_text(json.dumps({"start_at_login": True}))
    assert cli.main(["serve", "--fake-engine", "--data-dir", str(tmp_path)]) == 0
    assert "serve --data-dir" in unit.read_text()
    # Unchanged, it is not written or enabled again.
    calls = len(systemctl.calls)
    assert cli.main(["serve", "--fake-engine", "--data-dir", str(tmp_path)]) == 0 and len(systemctl.calls) == calls


def test_unit_quote_keeps_systemd_from_expanding_anything():
    assert shortcut.unit_quote("/opt/beat compass/bin") == '"/opt/beat compass/bin"'
    assert shortcut.unit_quote("50%$HOME") == "50%%$$HOME"
    assert shortcut.unit_quote('a"b\\c') == '"a\\"b\\\\c"'
