import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from octantcompass import cli
from octantcompass.api import create_app
from octantcompass.config import Config
from octantcompass.dbupdate import DatabaseRequests, DatabaseSelection, DatabaseUpdater
from octantcompass.engine import DBError, EngineState, VersionError
from octantcompass.engine.fake import FakeVectorEngine
from octantcompass.pairing import Pairing
from test_connection import wait_ready
from conftest import standing_secret


class LocalEngine(FakeVectorEngine):
    def open_db(self, path):
        path = Path(path)
        if path.name == "invalid":
            raise DBError("private DB details")
        if path.name == "unsupported":
            raise VersionError("private schema details")
        if path.name == "missing":
            raise FileNotFoundError(path)
        return path

    def db_info(self, db):
        return {**super().db_info(db), "dbVersion": db.name}

    def read_info(self, path):
        """What the manifest says is what opening the folder would: its name is the version."""
        return {**super().read_info(path), "dbVersion": self.open_db(path).name}

    def recommend(self, db, *args):
        return {**super().recommend(db, *args), "dbVersion": db.name}


@pytest.mark.parametrize("folder", ["invalid", "unsupported", "missing"])
def test_failed_switch_keeps_previous_snapshot_and_selection(tmp_path, folder):
    state = EngineState(LocalEngine())
    state.load(tmp_path / "old")
    old = state.snapshot
    selection = DatabaseSelection(tmp_path)
    selection.publish(tmp_path / "old")
    saved = selection.read()
    with pytest.raises((DBError, FileNotFoundError)):
        DatabaseUpdater(state).select_local(tmp_path / folder, selection)
    assert state.snapshot is old
    assert selection.read() == saved
    assert state.update_reason == "db_update_failed"
    DatabaseUpdater(state).select_local(tmp_path / "new", selection)
    assert state.snapshot.info["dbVersion"] == "new"
    assert state.update_reason is None
    assert selection.read()["path"] == str(tmp_path / "new")


def test_switch_waits_for_open_and_info_and_preserves_inflight_request(tmp_path):
    opening, release_open = threading.Event(), threading.Event()
    recommending, release_recommend = threading.Event(), threading.Event()
    class Engine(LocalEngine):
        def db_info(self, db):
            if db.name == "new":
                opening.set()
                assert release_open.wait(5)
            return super().db_info(db)

        def recommend(self, *args):
            recommending.set()
            assert release_recommend.wait(5)
            return super().recommend(*args)
    state = EngineState(Engine())
    pairing = Pairing(tmp_path / "pairing.json")
    standing_secret(pairing, "synthetic")
    headers = {"OctantPath-Api-Version": "2.0", "Authorization": f'Bearer {pairing.pair("synthetic", "test")}' }
    with TestClient(create_app(Config(tmp_path, idle_minutes=0, db=tmp_path / "old"), pairing, state)) as client:
        wait_ready(client)
        old = state.snapshot
        responses = []
        request_thread = threading.Thread(target=lambda: responses.append(client.post("/v1/recommendations", json={"count": 1}, headers=headers)))
        update_thread = threading.Thread(target=lambda: DatabaseUpdater(state).select_local(tmp_path / "new"))
        request_thread.start()
        try:
            assert recommending.wait(2)
            update_thread.start()
            assert opening.wait(2)
            assert state.snapshot is old
            assert client.get("/v1/health").json()["db"]["dbVersion"] == "old"
            release_open.set()
            update_thread.join(3)
            assert client.get("/v1/health").json()["db"]["dbVersion"] == "new"
        finally:
            release_open.set()
            release_recommend.set()
            request_thread.join(3)
            if update_thread.ident:
                update_thread.join(3)
        assert responses[0].json()["dbVersion"] == "old"
        assert client.post("/v1/recommendations", json={"count": 1}, headers=headers).json()["dbVersion"] == "new"


def test_unsupported_index_health_example_and_continued_recommendations(tmp_path, example, engine_example):
    class Engine(FakeVectorEngine):
        def plan_update(self, index, version):
            assert version == "2026.10.04"
            raise VersionError("private index details")
    info = engine_example("info.json")
    db = example("health.update_unsupported.json")["db"]
    info["views"] = {key: {"role": next(r for r, vs in db["roles"].items() if key in vs)} for key in db["views"]}
    state = EngineState(Engine(info=info))
    pairing = Pairing(tmp_path / "pairing.json")
    standing_secret(pairing, "synthetic")
    headers = {"OctantPath-Api-Version": "2.0", "Authorization": f'Bearer {pairing.pair("synthetic", "test")}' }
    with TestClient(create_app(Config(tmp_path, idle_minutes=0), pairing, state)) as client:
        wait_ready(client)
        old = state.snapshot
        with pytest.raises(VersionError):
            DatabaseUpdater(state).plan({"indexVersion": "future"})
        assert state.snapshot is old
        health = client.get("/v1/health").json()
        assert health == example("health.update_unsupported.json") | {"hostVersion": health["hostVersion"]}
        assert client.post("/v1/recommendations", json={"count": 1}, headers=headers).status_code == 200


def test_plan_uses_only_engine_and_resets_failure(tmp_path):
    engine = FakeVectorEngine()
    state = EngineState(engine)
    updater = DatabaseUpdater(state)
    state.update_reason = "index_unsupported"
    files = [{"opaque": "do not interpret"}]
    assert updater.plan({"files": files}) == files
    assert engine.calls[-1] == ("plan_update", {"files": files}, None)
    assert state.update_reason is None


def test_selection_atomic_and_revision_changes(tmp_path):
    selection = DatabaseSelection(tmp_path)
    assert selection.read() is None
    selection.publish(tmp_path / "new")
    first = selection.read()
    selection.publish(tmp_path / "new")
    assert selection.read()["revision"] != first["revision"]
    assert not list(tmp_path.glob(".db-selection.json-*"))
    assert selection.path.stat().st_mode & 0o077 == 0
    selection.path.write_text('{"path": false}')
    with pytest.raises(ValueError):
        selection.read()


def test_selection_publish_failure_preserves_live_db(tmp_path, monkeypatch):
    state = EngineState(LocalEngine())
    state.load(tmp_path / "old")
    old = state.snapshot
    selection = DatabaseSelection(tmp_path)
    selection.publish(tmp_path / "old")
    saved = selection.read()
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr("octantcompass.permissions.os.replace", fail)
    with pytest.raises(OSError):
        DatabaseUpdater(state).select_local(tmp_path / "new", selection)
    assert state.snapshot is old
    assert selection.read() == saved
    assert not list(tmp_path.glob(".db-selection.json-*"))


def test_cli_switch_and_running_host_observe_selection(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "OctantVectorEngine", LocalEngine)
    state = EngineState(LocalEngine())
    pairing = Pairing(tmp_path / "pairing.json")
    selection = DatabaseSelection(tmp_path)
    requests = DatabaseRequests(tmp_path)
    updater = DatabaseUpdater(state)
    config = Config(tmp_path, idle_minutes=0, db=tmp_path / "old", poll_seconds=0.01)
    with TestClient(create_app(config, pairing, state,
                    watch_db=lambda: updater.watch(requests, None, config.poll_seconds, selection))) as client:
        wait_ready(client)
        assert cli.main(["db", "switch", str(tmp_path / "new"), "--data-dir", str(tmp_path)]) == 0
        assert json.loads(capsys.readouterr().out)["dbVersion"] == "new"
        deadline = time.monotonic() + 3
        while client.get("/v1/health").json()["db"]["dbVersion"] != "new":
            assert time.monotonic() < deadline
            time.sleep(0.01)
        # A folder that becomes invalid after selection must not kill the host.
        requests.publish(tmp_path / "invalid")
        while "update" not in client.get("/v1/health").json():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert state.snapshot.info["dbVersion"] == "new"
        assert selection.read()["path"] == str(tmp_path / "new")
        requests.publish(tmp_path / "recovered")
        while state.snapshot.info["dbVersion"] != "recovered":
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert state.update_reason is None


def test_cli_failed_validation_does_not_publish(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "OctantVectorEngine", LocalEngine)
    with pytest.raises(SystemExit) as exc:
        cli.main(["db", "switch", str(tmp_path / "invalid"), "--data-dir", str(tmp_path)])
    assert exc.value.code == 1
    assert DatabaseSelection(tmp_path).read() is None


def test_cli_selected_db_restart_and_explicit_override(tmp_path, monkeypatch):
    selection = DatabaseSelection(tmp_path)
    selection.publish(tmp_path / "selected")
    observed = []
    async def serve(config, make_app, **_):
        observed.append(config.current.db)
        return 0
    monkeypatch.setattr(cli, "serve", serve)
    assert cli.main(["serve", "--data-dir", str(tmp_path)]) == 0
    assert cli.main(["serve", "--data-dir", str(tmp_path), "--db", str(tmp_path / "override")]) == 0
    assert observed == [tmp_path / "selected", tmp_path / "override"]


def test_failed_pending_request_after_restart_retains_accepted_db(tmp_path):
    selection = DatabaseSelection(tmp_path)
    requests = DatabaseRequests(tmp_path)
    selection.publish(tmp_path / "old")
    requests.publish(tmp_path / "invalid")
    state = EngineState(LocalEngine())
    pairing = Pairing(tmp_path / "pairing.json")
    updater = DatabaseUpdater(state)
    config = Config(tmp_path, idle_minutes=0, db=Path(selection.read()["path"]), poll_seconds=0.01)
    with TestClient(create_app(config, pairing, state,
                    watch_db=lambda: updater.watch(requests, selection.read(), config.poll_seconds, selection))) as client:
        deadline = time.monotonic() + 3
        while "update" not in client.get("/v1/health").json():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert state.snapshot.status == "ready"
        assert state.snapshot.info["dbVersion"] == "old"
        assert selection.read()["path"] == str(tmp_path / "old")


def test_pending_request_before_first_start_is_accepted(tmp_path):
    selection = DatabaseSelection(tmp_path)
    requests = DatabaseRequests(tmp_path)
    requests.publish(tmp_path / "new")
    state = EngineState(LocalEngine())
    updater = DatabaseUpdater(state)
    with TestClient(create_app(Config(tmp_path, idle_minutes=0), Pairing(tmp_path / "pairing.json"), state,
                    watch_db=lambda: updater.watch(requests, None, 0.01, selection))) as client:
        deadline = time.monotonic() + 3
        while state.snapshot.info is None or state.snapshot.info["dbVersion"] != "new":
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert selection.read() == requests.read()


def test_update_trust_comes_from_the_settings(tmp_path):
    """OctantSpecs contracts/distribution.md, 署名: trusted keys besides OctantVector's and the development-only unsigned switch."""
    assert Config(tmp_path).update_trust() == {"keys": {}, "allowUnsigned": False}
    (tmp_path / "settings.json").write_text(json.dumps({"trusted_keys": {"abcd": "AAAA"}, "allow_unsigned_db": True}))
    config = Config.load(tmp_path)
    assert config.update_trust() == {"keys": {"abcd": "AAAA"}, "allowUnsigned": True}
    for bad in ({"allow_unsigned_db": "yes"}, {"trusted_keys": {"abcd": 1}}):
        with pytest.raises(ValueError):
            Config(tmp_path, **bad)
    engine = FakeVectorEngine()
    assert DatabaseUpdater(EngineState(engine)).apply([], tmp_path / "base", tmp_path / "new", config.update_trust()) == []
    assert engine.calls[-1] == ("apply_update", [], tmp_path / "base", tmp_path / "new", config.update_trust())
