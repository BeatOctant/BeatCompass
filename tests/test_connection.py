import ast
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from octantcompass.api import create_app
from octantcompass.config import Config
from octantcompass.engine import DBError, EngineState, VectorEngine, VersionError
from octantcompass.engine.fake import FakeVectorEngine
from octantcompass.pairing import Pairing, digest

from test_presets import Relay
from conftest import standing_secret

HEADERS = {"OctantPath-Api-Version": "2.0"}


def wait_ready(client):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        response = client.get("/v1/health")
        if response.json()["status"] != "loading":
            return response.json()
        time.sleep(0.01)
    pytest.fail("engine did not finish loading")


def test_health_pair_examples_and_revocation(tmp_path, example, engine_example):
    pairing = Pairing(tmp_path / "pairing.json")
    request = example("pair.request.json")
    standing_secret(pairing, request["secret"])
    # Contract 1 (OctantCompass to OctantVector) info() keys views by id; /v1/health lists those ids in order.
    db = example("health.ready.json")["db"]
    info = {**engine_example("info.json"), "dbVersion": db["dbVersion"], "genres": db["genres"],
            "views": {view: {"role": next(r for r, vs in db["roles"].items() if view in vs), "rowKind": "maps",
                             "rows": 1, "dim": 1} for view in db["views"]}}
    engine = EngineState(Relay(example("presets.response.json"), info=info))
    with TestClient(create_app(Config(tmp_path, idle_minutes=0), pairing, engine)) as client:
        wait_ready(client)
        response = client.post("/v1/pair", json={**request, "future": True}, headers=HEADERS)
        assert response.status_code == 200
        assert response.json().keys() == example("pair.response.json").keys()
        token = response.json()["token"]
        environment = pairing.authenticate(token)
        paired = client.get("/v1/health", headers={"Authorization": f"Bearer {token}"}).json()
        expected = example("health.ready.json")
        expected["signals"] = 0  # nothing was sent from this environment yet
        expected["instanceId"] = paired["instanceId"]  # this run of the host
        expected["hostVersion"] = paired["hostVersion"]  # the example carries whatever version OctantSpecs wrote
        # presetsRevision is the revision of the engine's presets() (the examples share one), relayed.
        assert paired == expected
        assert "paired" not in client.get("/v1/health").json()
        assert "paired" not in client.get("/v1/health", headers={"Authorization": "Bearer invalid"}).json()
        second = client.post("/v1/pair", json=request, headers=HEADERS).json()["token"]
        assert pairing.authenticate(second) != environment
        assert Pairing(pairing.path).authenticate(token) == environment
        for route in ("/v1/recommendations", "/v1/signals", "/v1/profile"):
            response = client.post(route, json={}, headers=HEADERS)
            assert response.status_code == 401
            assert response.json() == example("error.unauthorized.json")
        assert pairing.revoke(environment)
        assert pairing.authenticate(token) is None
        assert pairing.authenticate(second) is not None
        assert "signals" not in client.get("/v1/health", headers={"Authorization": f"Bearer {token}"}).json()
        for route in ("/v1/recommendations", "/v1/signals", "/v1/profile"):
            assert client.post(route, json={}, headers={**HEADERS, "Authorization": f"Bearer {token}"}).status_code == 401
        assert client.post("/v1/pair", json={"secret": "wrong", "name": "test"}, headers=HEADERS).status_code == 401
    assert token.encode() not in pairing.path.read_bytes() and second.encode() not in pairing.path.read_bytes()
    assert request["secret"].encode() not in pairing.path.read_bytes()
    # Only the hash of a live token is kept; the revoked one is deleted with its environment.
    assert digest(second).encode() in pairing.path.read_bytes()


def test_revoke_deletes_the_environment(tmp_path):
    pairing = Pairing(tmp_path / "pairing.json")
    pairing.write({"environments": [{"id": i, "name": i, "tokenHash": h} for i, h in (("kept", "y"), ("gone", "z"))],
                   "code": None})
    assert [e["id"] for e in Pairing(pairing.path).environments()] == ["kept", "gone"]
    assert pairing.revoke("gone") and not pairing.revoke("gone")
    assert pairing.environments() == [{"id": "kept", "name": "kept"}]
    assert json.loads(pairing.path.read_text())["environments"] == [{"id": "kept", "name": "kept", "tokenHash": "y"}]


@pytest.mark.parametrize("version", [None, "3.0"])
@pytest.mark.parametrize("route", ["/v1/pair", "/v1/recommendations", "/v1/signals", "/v1/profile"])
def test_version_before_auth_or_shape(tmp_path, version, route):
    with TestClient(create_app(Config(tmp_path, idle_minutes=0), Pairing(tmp_path / "pairing.json"), EngineState(FakeVectorEngine()))) as client:
        response = client.post(route, content="broken json", headers={} if version is None else {"OctantPath-Api-Version": version})
        assert response.status_code == 426
        body = response.json()["error"]
        assert body["code"] == "version_mismatch"
        assert body["apiVersion"] == "2.0"
        assert body["requestApiVersion"] == version
        assert client.get("/v1/health", headers={"OctantPath-Api-Version": "99.0"}).status_code == 200


def test_minor_version_and_validation(tmp_path):
    pairing = Pairing(tmp_path / "pairing.json")
    standing_secret(pairing, "local-secret")
    with TestClient(create_app(Config(tmp_path, idle_minutes=0), pairing, EngineState(FakeVectorEngine()))) as client:
        assert client.post("/v1/pair", json={"secret": "local-secret", "name": "synthetic"}, headers={"OctantPath-Api-Version": "2.99"}).status_code == 200
        for body in ({}, {"secret": 123, "name": "test"}, {"secret": "local-secret", "name": None}):
            response = client.post("/v1/pair", json=body, headers=HEADERS)
            assert response.status_code == 400
            assert response.json()["error"]["code"] == "invalid_request"


def test_code_expiry_attempt_limit_and_cross_process_store(tmp_path):
    now = [1000.0]
    first = Pairing(tmp_path / "pairing.json", clock=lambda: now[0])
    second = Pairing(first.path, clock=lambda: now[0])
    standing_secret(first, "local")
    standing_secret(second, "local")
    code = first.issue_code(digits=8, seconds=12, attempts=2)
    assert len(code) == 8 and code.isdigit()
    assert second.pair(code, "synthetic")
    assert first.pair(code, "synthetic") is None
    code = first.issue_code(digits=6, seconds=12, attempts=2)
    now[0] += 12
    assert second.pair(code, "synthetic") is None
    code = first.issue_code(digits=6, seconds=12, attempts=2)
    assert second.pair("wrong", "synthetic") is None
    assert second.pair("wrong", "synthetic") is None
    assert first.pair(code, "synthetic") is None
    assert first.pair("local", "same-name")
    assert second.pair("local", "same-name")
    standing_secret(first, "replacement")
    assert first.pair("local", "same-name") is None
    assert first.pair("replacement", "same-name")


def test_code_concurrent_single_use(tmp_path):
    pairing = Pairing(tmp_path / "pairing.json")
    code = pairing.issue_code(digits=6, seconds=60, attempts=5)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: Pairing(pairing.path).pair(code, "synthetic"), range(4)))
    assert sum(token is not None for token in results) == 1


@pytest.mark.parametrize("exc,reason", [(DBError("bad"), "db_invalid"), (VersionError("bad"), "schema_unsupported"), (FileNotFoundError(), "db_missing"),
                                        (MemoryError(), "db_load_failed"), (ValueError("bad"), "db_load_failed")])
def test_db_failures(tmp_path, exc, reason):
    state = EngineState(FakeVectorEngine(error=exc))
    with TestClient(create_app(Config(tmp_path, idle_minutes=0), Pairing(tmp_path / "pairing.json"), state)) as client:
        result = wait_ready(client)
        assert result["status"] == "degraded"
        assert result["reason"] == reason
        assert result["db"] is None


def test_loading_does_not_block_http(tmp_path):
    release = threading.Event()
    started = threading.Event()

    class SlowEngine(FakeVectorEngine):
        def open_db(self, path):
            started.set()
            release.wait(timeout=5)
            return super().open_db(path)

    state = EngineState(SlowEngine())
    with TestClient(create_app(Config(tmp_path, idle_minutes=0), Pairing(tmp_path / "pairing.json"), state)) as client:
        try:
            assert started.wait(timeout=2)
            assert client.get("/v1/health").json()["status"] == "loading"
        finally:
            release.set()
        assert wait_ready(client)["status"] == "ready"


def test_fake_contract_port_and_opaque_handle(tmp_path, example, engine_example):
    fake = FakeVectorEngine(recommendations=engine_example("recommend.output.json"), profile=engine_example("describe.output.json"))
    assert isinstance(fake, VectorEngine)
    handle = fake.open_db(tmp_path / "not-an-actual-db")
    history = example("signals.request.json")["events"]
    result = fake.recommend(handle, history, {"count": 2}, {"minViewWeight": 0.5})
    assert result == engine_example("recommend.output.json")
    result["items"].clear()
    assert fake.recommend(handle, [], {}, {}) == engine_example("recommend.output.json")
    assert fake.describe(handle, history, {}) == engine_example("describe.output.json")
    assert fake.plan_update({"files": ["synthetic"]}, None) == ["synthetic"]
    assert fake.apply_update([], None, tmp_path / "out") == tmp_path / "out"


def test_import_boundaries_and_no_game_settings():
    root = Path(__file__).parents[1] / "src/octantcompass"
    for file in root.rglob("*.py"):
        for node in ast.walk(ast.parse(file.read_text())):
            modules = []
            if isinstance(node, ast.Import):
                modules = [item.name for item in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            for module in modules:
                if module.split(".")[0] in {"octantvector", "numpy", "pyarrow"}:
                    assert file.relative_to(root).as_posix() == "engine/octantvector.py"
    assert not any("game" in name.lower() or "saber" in name.lower() for name in Config.__dataclass_fields__)
    assert not {"warmupPlays", "minViewWeight", "sessionGapMinutes", "params"} & Config.__dataclass_fields__.keys()


@pytest.mark.parametrize("contract", ["contract1", "contract3"])
def test_examples_are_json_and_have_provenance(contract):
    # Copied examples come from the OctantSpecs ref this repository follows.
    pinned = (Path(__file__).parents[1] / "OCTANTSPECS_REF").read_text().strip()
    examples = Path(__file__).parent / "examples" / contract
    for file in examples.glob("*.json"):
        assert isinstance(json.loads(file.read_text()), dict)
    assert f"OctantSpecs ref: {pinned}" in (examples / "SOURCE").read_text()


def test_settings_defaults_override_unknown_and_validation(tmp_path):
    config = Config.load(tmp_path)
    assert config.host == "127.0.0.1" and config.port == 0
    (tmp_path / "settings.json").write_text(json.dumps({"port": 1234, "host": "0.0.0.0", "pair_code_seconds": 2, "warmupPlays": 99}))
    config = Config.load(tmp_path, port=0, host="127.0.0.1")
    assert config.port == 0 and config.host == "127.0.0.1" and config.pair_code_seconds == 2
    assert not hasattr(config, "warmupPlays")
    for override in ({"port": -1}, {"pair_code_seconds": 0}, {"pair_code_attempts": True}, {"poll_seconds": float("nan")}):
        with pytest.raises(ValueError):
            Config(tmp_path, **override)


def test_a_resting_db_that_fails_to_load_is_degraded_not_loading(tmp_path):
    # An error the engine does not map (here out of memory) used to leave the host "loading" for good.
    pairing = Pairing(tmp_path / "pairing.json")
    standing_secret(pairing, "synthetic")
    paired = {**HEADERS, "Authorization": f"Bearer {pairing.pair('synthetic', 'synthetic')}"}
    state = EngineState(FakeVectorEngine(error=MemoryError()))
    with TestClient(create_app(Config(tmp_path, idle_minutes=30), pairing, state),
                    client=("127.0.0.1", 50000), base_url="http://127.0.0.1:23287") as client:
        assert client.post("/v1/recommendations", json={"count": 1}, headers=paired).status_code == 503
        deadline = time.monotonic() + 3
        while (health := client.get("/v1/health", headers=paired).json())["status"] == "loading":
            assert time.monotonic() < deadline, "the host stayed loading"
            time.sleep(0.01)
        assert health["status"] == "degraded" and health["reason"] == "db_load_failed"
        assert not state.snapshot.resting


def test_resting_db_loads_for_a_paired_request_and_is_let_go_when_idle(tmp_path):
    # Contract 6 (client to OctantCompass) 2.0: a host that stays running holds no DB until a paired environment
    # asks, and lets it go again after idle_minutes without such requests.
    opened = []
    release = threading.Event()

    class CountingEngine(FakeVectorEngine):
        def open_db(self, path):
            opened.append(path)
            release.wait(timeout=5)
            return super().open_db(path)

    pairing = Pairing(tmp_path / "pairing.json")
    standing_secret(pairing, "synthetic")
    token = pairing.pair("synthetic", "synthetic")
    paired = {**HEADERS, "Authorization": f"Bearer {token}"}
    state = EngineState(CountingEngine())
    app = create_app(Config(tmp_path, idle_minutes=0.002), pairing, state)  # 0.12 s
    with TestClient(app, client=("127.0.0.1", 50000), base_url="http://127.0.0.1:23287") as client:
        page = {"X-OctantCompass-Manage-Key": app.state.manage_key}  # the window's call
        time.sleep(0.1)
        # Nobody asked: no DB, and neither an unpaired health check nor the window's status loads it.
        assert client.get("/v1/health").json() | {"hostVersion": None} == {
            "apiVersion": "2.0", "hostVersion": None, "status": "loading", "db": None}
        assert client.get("/manage/status", headers=page).json()["status"] == "resting"
        assert opened == [] and state.snapshot.resting
        # A paired request answers "not ready" at once and starts the load.
        assert client.post("/v1/recommendations", json={"count": 1}, headers=paired).status_code == 503
        release.set()
        deadline = time.monotonic() + 3
        while client.get("/v1/health", headers=paired).json()["status"] != "ready":
            assert time.monotonic() < deadline, "the DB did not load for the paired request"
            time.sleep(0.01)
        assert len(opened) == 1
        assert client.post("/v1/recommendations", json={"count": 1}, headers=paired).status_code == 200
        # Without paired requests the DB is let go; the window's status keeps the last DB's version to show.
        deadline = time.monotonic() + 3
        while not state.snapshot.resting:
            assert time.monotonic() < deadline, "the DB was not let go"
            time.sleep(0.01)
        assert client.get("/v1/health").json()["db"] is None
        assert client.get("/manage/status", headers=page).json()["db"]["dbVersion"] == state.snapshot.info["dbVersion"]
        deadline = time.monotonic() + 3
        while client.get("/v1/health", headers=paired).json()["status"] != "ready":
            assert time.monotonic() < deadline, "the DB did not load again"
            time.sleep(0.01)
        assert len(opened) == 2


def test_idle_zero_loads_at_start_and_keeps_the_db(tmp_path):
    state = EngineState(FakeVectorEngine())
    with TestClient(create_app(Config(tmp_path, idle_minutes=0), Pairing(tmp_path / "pairing.json"), state)) as client:
        assert wait_ready(client)["status"] == "ready"
        time.sleep(0.05)
        assert client.get("/v1/health").json()["status"] == "ready" and not state.snapshot.resting
    for bad in (-1, float("inf"), "30"):
        with pytest.raises(ValueError):
            Config(tmp_path, idle_minutes=bad)
