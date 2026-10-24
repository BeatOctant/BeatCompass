import json
import socket
import time
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

import pytest
from fastapi.testclient import TestClient

from octantcompass.api import create_app
from octantcompass.config import DEFAULTS, EDITABLE, Config, save_setting
from octantcompass.engine import EngineState
from octantcompass.engine.fake import FakeVectorEngine
from octantcompass.lifecycle import connection_path
from octantcompass.manage import KEY_HEADER
from octantcompass.manager import Manager, Refused
from octantcompass.pairing import Pairing
from octantcompass.places import Places

from test_connection import wait_ready
from test_lifecycle import await_connection, server  # noqa: F401 (fixture)
from conftest import standing_secret

LOCAL = ("127.0.0.1", 50000)
BASE = "http://127.0.0.1:23287"  # the window asks by this PC's own name


def client_for(tmp_path, *, client=LOCAL, engine=None):
    pairing = Pairing(tmp_path / "pairing.json")
    standing_secret(pairing, "local-secret")
    app = create_app(Config(tmp_path, idle_minutes=0), pairing, EngineState(engine or FakeVectorEngine()))
    return TestClient(app, client=client, base_url=BASE), pairing


def ui(client, path, body=None, headers=None):
    """A call as the window makes it: with this run's key (connection.json)."""
    headers = {KEY_HEADER: client.app.state.manage_key, **(headers or {})}
    return client.get(path, headers=headers) if body is None else client.post(path, json=body, headers=headers)


def test_api_needs_loopback_and_no_web_page(tmp_path):
    with client_for(tmp_path)[0] as client:
        # No page here: the window shows its own (window.py).
        assert client.get("/ui/").status_code == 426 and client.get("/manage/").status_code == 404
        assert ui(client, "/manage/status").status_code == 200
        assert ui(client, "/manage/status", headers={"Host": "localhost:23287"}).status_code == 200
        # A site, by its own name (DNS rebinding) or from any page, this host's address included (no page uses the
        # API): no key is needed to keep it out.
        for headers in ({"Host": "rebind.example:23287"}, {"Origin": "http://evil.example"}, {"Origin": BASE},
                        {"Origin": "http://127.0.0.1:9999"}, {"Origin": "null"}, {"Origin": "https://127.0.0.1:23287"}):
            assert ui(client, "/manage/status", headers=headers).status_code == 403, headers
            assert ui(client, "/manage/stop", {}, headers=headers).json()["error"]["code"] == "other_site", headers
        # A program that reaches loopback without being this PC's user (an app on the Quest through adb reverse)
        # has no key: refused, the pairing code included.
        for key in (None, "", "wrong", client.app.state.manage_key + "x"):
            headers = {} if key is None else {KEY_HEADER: key}
            assert client.get("/manage/status", headers=headers).json()["error"]["code"] == "no_key", key
            assert client.post("/manage/pair-code", json={}, headers=headers).status_code == 403, key
        assert client.post("/manage/link").status_code in (404, 405)
        # The window changes the DB itself, and nothing opens the window from here (2026-10-19).
        for gone in ("/manage/window", "/manage/db-switch", "/manage/db-import", "/manage/db-update",
                     "/manage/db-update/check"):
            assert ui(client, gone, {}).status_code == 404, gone
        # Contract 3 (OctantPath to OctantCompass) still guards its own paths.
        assert client.post("/v1/signals", json={}).status_code == 426
    with client_for(tmp_path, client=("192.0.2.10", 50000))[0] as client:
        assert ui(client, "/manage/status").status_code == 403
        assert ui(client, "/manage/stop", {}).status_code == 403
    with client_for(tmp_path, client=("::1", 50000))[0] as client:
        assert ui(client, "/manage/status", headers={"Host": "[::1]:23287"}).status_code == 200


def test_status_shows_the_db_notice_and_each_views_credits(tmp_path):
    engine = FakeVectorEngine(info={"dbVersion": "fake", "schemaVersion": "1.2", "builtAt": None, "calibrationId": "fake",
                                    "counts": {}, "skippedViews": [], "genres": [], "notice": "# Terms\nCC BY-NC-SA 4.0",
                                    "views": {"a@1": {"credits": ["Model A"]}, "b@1": {"credits": []}}})
    client, _ = client_for(tmp_path, engine=engine)
    with client:
        wait_ready(client)
        db = ui(client, "/manage/status").json()["db"]
        assert db["notice"] == "# Terms\nCC BY-NC-SA 4.0" and db["credits"] == {"a@1": ["Model A"]}
        assert db["views"] == ["a@1", "b@1"]


def test_status_presets_environments_and_log(tmp_path):
    client, pairing = client_for(tmp_path)
    with client:
        wait_ready(client)
        status = ui(client, "/manage/status").json()
        assert status["status"] == "ready" and status["db"]["dbVersion"] == "fake"
        assert status["dbPath"] == str(tmp_path / "db") and status["dataDir"] == str(tmp_path)
        assert status["canStop"] is False
        presets = ui(client, "/manage/presets").json()
        assert presets == client.app.state.engine.engine.presets(None)
        token = client.post("/v1/pair", json={"secret": "local-secret", "name": "synthetic"},
                            headers={"OctantPath-Api-Version": "2.0"}).json()["token"]
        environments = ui(client, "/manage/environments").json()["environments"]
        assert [(e["name"], e["signals"]) for e in environments] == [("synthetic", 0)]
        code = ui(client, "/manage/pair-code", {}).json()
        assert len(code["code"]) == 6 and code["seconds"] == 300
        assert ui(client, f"/manage/environments/{environments[0]['id']}/revoke", {}).json() == {"revoked": environments[0]["id"]}
        assert ui(client, "/manage/environments/unknown/revoke", {}).status_code == 404
        assert pairing.authenticate(token) is None
        assert ui(client, "/manage/environments").json()["environments"] == []
        lines = ui(client, "/manage/log").json()["lines"]
        text = json.dumps(lines)
        assert "DB loaded" in text and "paired a new environment: synthetic" in text and "revoked environment" in text
        assert token not in text and code["code"] not in text and "local-secret" not in text


def test_log_lines_are_bounded_by_the_setting(tmp_path):
    pairing = Pairing(tmp_path / "pairing.json")
    app = create_app(Config(tmp_path, idle_minutes=0, log_lines=2), pairing, EngineState(FakeVectorEngine()))
    with TestClient(app, client=LOCAL, base_url=BASE) as client:
        wait_ready(client)
        for _ in range(3):
            ui(client, "/manage/pair-code", {})
        assert len(ui(client, "/manage/log").json()["lines"]) == 2


def test_stop_calls_the_server_hook(tmp_path):
    client, _ = client_for(tmp_path)
    calls = []
    with client:
        assert ui(client, "/manage/stop", {}).status_code == 409
        client.app.state.stop = lambda: calls.append(True)
        assert ui(client, "/manage/status").json()["canStop"] is True
        assert ui(client, "/manage/stop", {}).json() == {"stopping": True}
    assert calls == [True]


def test_served_api_and_stop(tmp_path, server):  # noqa: F811
    process = server("--fake-engine")
    info = await_connection(tmp_path, process)
    opener = build_opener(ProxyHandler({}))
    base = f'http://127.0.0.1:{info["port"]}'
    try:
        opener.open(Request(f"{base}/manage/stop", data=b"{}", headers={"Origin": "http://evil.example"}), timeout=3)
        raise AssertionError("the API answered another site")
    except HTTPError as error:
        assert error.code == 403
    key = {KEY_HEADER: info["manageKey"]}
    try:
        opener.open(f"{base}/manage/status", timeout=3)
        raise AssertionError("the API answered without the key")
    except HTTPError as error:
        assert error.code == 403
    with opener.open(Request(f"{base}/manage/status", headers=key), timeout=3) as response:
        assert json.load(response)["listen"] == f'127.0.0.1:{info["port"]}'
    with opener.open(Request(f"{base}/manage/stop", data=b"{}", headers=key), timeout=3) as response:
        assert json.load(response) == {"stopping": True}
    process.communicate(timeout=10)
    assert process.returncode == 0
    assert not connection_path(tmp_path).exists()


def test_settings_in_use_and_applied_when_the_window_saved_them(tmp_path):
    pairing = Pairing(tmp_path / "pairing.json")
    (tmp_path / "settings.json").write_text(json.dumps({"trusted_keys": {"10c26904c2f7bb19": "c3ludGhldGlj"}}))
    app = create_app(Config.load(tmp_path), pairing, EngineState(FakeVectorEngine()))
    with TestClient(app, client=LOCAL, base_url=BASE) as client:
        used = ui(client, "/manage/settings").json()["settings"]
        # Only what a user changes; the internal waits stay in settings.json.
        assert list(used) == list(EDITABLE) and used["trusted_keys"] == {"10c26904c2f7bb19": "c3ludGhldGlj"}
        # The window writes settings.json, then asks the host to use what it saved.
        for key, value in (("index_url", "/synthetic/index.json"), ("pair_code_digits", 8), ("log_lines", 3)):
            save_setting(tmp_path, key, value)
            assert ui(client, "/manage/settings/apply", {"key": key}).json() == {"applied": True}
        save_setting(tmp_path, "port", 38123)
        assert ui(client, "/manage/settings/apply", {"key": "port"}).json() == {"applied": False}  # until a restart
        for key in ("db", "poll_seconds", "unknown"):
            assert ui(client, "/manage/settings/apply", {"key": key}).status_code == 400, key
        used = ui(client, "/manage/settings").json()["settings"]
        assert used["index_url"] == "/synthetic/index.json" and used["port"] == DEFAULTS["port"]
        assert len(ui(client, "/manage/pair-code", {}).json()["code"]) == 8
        assert len(ui(client, "/manage/log").json()["lines"]) == 3
        assert "applied" in json.dumps(ui(client, "/manage/log").json())


def restart(manager, number):
    """Restart from the window and wait for that restart's result."""
    assert manager.host("/manage/restart", {}) == (200, {"restarting": True})
    deadline = time.monotonic() + 15
    while True:
        try:
            code, status = manager.host("/manage/status")
            if code == 200 and (status["restart"] or {}).get("number") == number:
                return status
        except Refused:
            pass  # the same port is let go of for a moment
        assert time.monotonic() < deadline, "the restart did not finish"
        time.sleep(0.05)


def test_restart_listens_again_in_the_same_process(tmp_path, server):  # noqa: F811
    process = server("--fake-engine")
    info = await_connection(tmp_path, process)
    manager = Manager(Places.at(tmp_path))
    pairing = Pairing(tmp_path / "pairing.json")
    token = pairing.pair(pairing.issue_code(digits=6, seconds=60, attempts=1), "synthetic")
    # A plain restart: the same process, port and pairing; OctantPath's token keeps working.
    status = restart(manager, 1)
    assert status["restart"] == {"number": 1, "ok": True, "listen": f'127.0.0.1:{info["port"]}', "problem": None}
    assert status["pid"] == process.pid and process.poll() is None
    assert json.loads(connection_path(tmp_path).read_text()) | {"startedAt": None} == info | {"startedAt": None}
    health = Request(f'http://127.0.0.1:{info["port"]}/v1/health', headers={"Authorization": f"Bearer {token}"})
    with build_opener(ProxyHandler({})).open(health, timeout=3) as response:
        assert json.load(response)["paired"] is True
    # An address this machine does not have: back on the old one, with the reason in the window.
    manager.save_setting("host", "192.0.2.1")
    status = restart(manager, 2)
    assert status["restart"]["ok"] is False and "192.0.2.1" in status["restart"]["problem"]
    assert status["listen"] == f'127.0.0.1:{info["port"]}' and process.poll() is None
    manager.save_setting("host", "")
    # A port another program holds: the old one never stops answering.
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        held = taken.getsockname()[1]
        manager.save_setting("port", str(held))
        status = restart(manager, 3)
    assert status["restart"]["ok"] is False and str(held) in status["restart"]["problem"]
    assert status["listen"] == f'127.0.0.1:{info["port"]}'
    port_row = next(row for row in manager.settings()["settings"] if row["key"] == "port")
    assert port_row["value"] == 0 and port_row["saved"] == held  # still offered for the next restart
    # A free port: the new one answers and the old one closes; the window finds the host there (connection.json).
    with socket.socket() as free:
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
    manager.save_setting("port", str(port))
    status = restart(manager, 4)
    assert status["restart"]["ok"] is True and status["listen"] == f"127.0.0.1:{port}"
    assert json.loads(connection_path(tmp_path).read_text())["port"] == port
    with pytest.raises(OSError):
        build_opener(ProxyHandler({})).open(f'http://127.0.0.1:{info["port"]}/manage/status', timeout=3)
    assert restart(manager, 5)["listen"] == f"127.0.0.1:{port}"
    manager.host("/manage/stop", {})
    assert process.wait(timeout=15) == 0
    assert not connection_path(tmp_path).exists()


def test_build_commit_outside_a_checkout_and_from_the_bundled_file(tmp_path, monkeypatch):
    from octantcompass import build
    assert build.describe(tmp_path) is None
    stamp = tmp_path / "_commit.txt"
    stamp.write_text("abc1234\n", encoding="utf-8")
    monkeypatch.setattr(build, "COMMIT_FILE", stamp)
    build.commit.cache_clear()
    try:
        assert build.commit() == "abc1234"
    finally:
        build.commit.cache_clear()
