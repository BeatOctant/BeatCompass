import importlib.util
import os
import sys
import warnings
from pathlib import Path
from types import ModuleType

import pytest

from octantcompass.engine import (DBError, EngineState, RequestError, SeedNotEmbeddedError, SeedNotInDBError,
                               SignatureWarning, UnknownPresetError, VectorEngine, VersionError)
from octantcompass.engine.octantvector import SUPPORTED_API, OctantVectorEngine
from octantcompass.engine.fake import FakeVectorEngine
from test_presets import assert_presets_shape
from test_recommendations import connected


@pytest.fixture
def runtime(monkeypatch, engine_example, example, tmp_path):
    """Contract-only module substitute; no OctantVector implementation is read."""
    (tmp_path / "db").mkdir()
    package = ModuleType("octantvector")
    module = ModuleType("octantvector.runtime")
    module.API_VERSION = "1.0"
    module.DBError = type("DBError", (Exception,), {})
    module.VersionError = type("VersionError", (module.DBError,), {})
    module.RequestError = type("RequestError", (Exception,), {})
    module.SeedNotInDBError = type("SeedNotInDBError", (module.RequestError,), {})
    module.SeedNotEmbeddedError = type("SeedNotEmbeddedError", (module.SeedNotInDBError,), {})
    module.UnknownPresetError = type("UnknownPresetError", (module.RequestError,), {})
    module.SignatureWarning = type("SignatureWarning", (UserWarning,), {})
    module.calls = []
    info = engine_example("info.json")
    info["dbVersion"] = "synthetic"

    class Handle:
        def info(self):
            module.calls.append(("info",))
            return info

    module.handle = Handle()
    module.recommendation = engine_example("recommend.output.json")
    module.recommendation["dbVersion"] = "synthetic"
    module.profile = engine_example("describe.output.json")
    module.profile["dbVersion"] = "synthetic"
    # presets(db) has the shape of contract 3 (OctantPath to OctantCompass) PresetsResponse (contract 1 (OctantCompass to OctantVector)).
    module.listing = example("presets.response.json")
    module.expansion = {"count": 2, "genre": "jpop", "narrow": {"clap_htsat_unfused": 1}, "weights": {"chart_patterns": 1},
                        "difficulty": "normal"}

    def open_db(path):
        module.calls.append(("open_db", path))
        return module.handle

    def recommend(db, history, request, params):
        module.calls.append(("recommend", db, history, request, params))
        return module.recommendation

    def describe(db, history, params):
        module.calls.append(("describe", db, history, params))
        return module.profile

    def presets(db):
        module.calls.append(("presets", db))
        return module.listing

    def expand_preset(db, request):
        module.calls.append(("expand_preset", db, request))
        if "preset" not in request:
            return request
        if request["preset"] not in {preset["id"] for preset in module.listing["presets"]}:
            raise module.UnknownPresetError("synthetic unknown preset")
        return module.expansion

    def plan_update(index, local_version):
        module.calls.append(("plan_update", index, local_version))
        return index["files"]

    def apply_update(files, base, out, trust=None):
        module.calls.append(("apply_update", files, base, out, trust))
        return out

    def import_archive(path, out, trust=None):
        module.calls.append(("import_archive", path, out, trust))
        return out

    module.open_db, module.recommend, module.describe = open_db, recommend, describe
    module.presets, module.expand_preset = presets, expand_preset
    module.plan_update, module.apply_update, module.import_archive = plan_update, apply_update, import_archive
    package.runtime = module
    monkeypatch.setitem(sys.modules, "octantvector", package)
    monkeypatch.setitem(sys.modules, "octantvector.runtime", module)
    return package, module


@pytest.fixture(params=["fake", "adapter", "installed"])
def contract_engine(request, tmp_path, engine_example):
    if request.param == "fake":
        info = engine_example("info.json")
        info["dbVersion"] = "synthetic"
        recommendations = engine_example("recommend.output.json")
        recommendations["dbVersion"] = "synthetic"
        profile = engine_example("describe.output.json")
        profile["dbVersion"] = "synthetic"
        return FakeVectorEngine(info=info, recommendations=recommendations, profile=profile), tmp_path
    if request.param == "adapter":
        request.getfixturevalue("runtime")
        return OctantVectorEngine(), tmp_path
    path = os.environ.get("OCTANTCOMPASS_TEST_DB")
    if path is None or importlib.util.find_spec("octantvector") is None:
        pytest.skip("requires installed OctantVector and OCTANTCOMPASS_TEST_DB")
    return OctantVectorEngine(), Path(path)


def test_common_contract1_shapes_and_determinism(contract_engine, example):
    engine, path = contract_engine
    assert isinstance(engine, VectorEngine)
    db = engine.open_db(path)
    info = engine.db_info(db)
    assert {"dbVersion", "schemaVersion", "builtAt", "calibrationId", "counts", "views", "skippedViews", "genres"} <= info.keys()
    assert isinstance(info["views"], dict) and isinstance(info["skippedViews"], list)
    for view in info["views"].values():
        assert {"role", "rowKind", "rows", "dim"} <= view.keys()
    history = example("signals.request.json")["events"]
    params = {"minViewWeight": 0.5, "warmupPlays": 3, "sessionGapMinutes": 30}
    request = {"count": 2, "difficulty": "normal"}
    result = engine.recommend(db, history, request, params)
    assert result == engine.recommend(db, history, request, params)
    assert result["dbVersion"] == info["dbVersion"]
    assert isinstance(result["items"], list)
    assert isinstance(result["aims"], dict) and isinstance(result["replaced"], list)
    for candidate in result["items"]:
        assert {"rank", "hash", "characteristic", "difficulty", "reason"} <= candidate.keys()
        assert "score" not in candidate
    profile = engine.describe(db, history, params)
    assert profile["dbVersion"] == info["dbVersion"]
    assert {"likedMaps", "plays", "playStyles"} <= profile.keys()
    listing = engine.presets(db)
    assert_presets_shape(listing)
    assert listing == engine.presets(db)
    assert engine.expand_preset(db, request) == request


def test_adapter_passes_handles_arguments_and_results_unchanged(runtime, tmp_path, example):
    _, module = runtime
    engine = OctantVectorEngine()
    db = engine.open_db(tmp_path)
    assert db is module.handle
    history = example("signals.request.json")["events"]
    params = {"future": {"keep": [1]}, "minViewWeight": 0.5}
    request = example("recommendations.request.explicit.json")
    module.recommendation["items"].reverse()
    assert engine.recommend(db, history, request, params) is module.recommendation
    assert module.calls[-1] == ("recommend", db, history, request, params)
    assert module.calls[-1][2] is history and module.calls[-1][4] is params
    assert engine.describe(db, history, params) is module.profile
    assert module.calls[-1] == ("describe", db, history, params)
    assert engine.presets(db) is module.listing
    assert module.calls[-1] == ("presets", db)
    preset_request = {**example("recommendations.request.preset.json"), "future": {"keep": True}}
    assert engine.expand_preset(db, preset_request) is module.expansion
    assert module.calls[-1] == ("expand_preset", db, preset_request) and module.calls[-1][2] is preset_request
    assert engine.expand_preset(db, request) is request
    files = [{"opaque": "file"}]
    index = {"files": files}
    assert engine.plan_update(index, "old") is files
    assert module.calls[-1] == ("plan_update", index, "old")
    out = tmp_path / "new"
    trust = {"keys": {}, "allowUnsigned": True}
    assert engine.apply_update(files, tmp_path, out, trust) is out
    assert module.calls[-1] == ("apply_update", files, tmp_path, out, trust)
    assert engine.import_archive(tmp_path / "v.full.tar", out, trust) is out
    assert module.calls[-1] == ("import_archive", tmp_path / "v.full.tar", out, trust)


def test_signature_warnings_are_translated_and_others_pass(runtime, tmp_path):
    _, module = runtime
    other = type("OtherWarning", (UserWarning,), {})

    def apply_update(files, base, out, trust=None):
        warnings.warn(module.SignatureWarning("v2: synthetic: no manifest.sig"))
        warnings.warn(other("synthetic other warning"))
        return out
    module.apply_update = apply_update
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert OctantVectorEngine().apply_update([], None, tmp_path / "new") == tmp_path / "new"
    assert sorted((w.category.__name__, str(w.message)) for w in caught) == [
        ("OtherWarning", "synthetic other warning"), ("SignatureWarning", "v2: synthetic: no manifest.sig")]
    assert next(w for w in caught if w.category.__name__ == "SignatureWarning").category is SignatureWarning

    def import_archive(path, out, trust=None):
        warnings.warn(module.SignatureWarning("v3: synthetic: no manifest.sig"))
        return out
    module.import_archive = import_archive
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert OctantVectorEngine().import_archive(tmp_path / "v3.full.tar", tmp_path / "v3") == tmp_path / "v3"
    assert [(w.category, str(w.message)) for w in caught] == [(SignatureWarning, "v3: synthetic: no manifest.sig")]


@pytest.mark.parametrize("version", ["0.9", "2.0", "1", "1.x", "garbage", None, "missing"])
def test_incompatible_api_version_never_opens_db(runtime, monkeypatch, tmp_path, version):
    package, module = runtime
    if version == "missing":
        del module.API_VERSION
    else:
        module.API_VERSION = version
    state = EngineState(OctantVectorEngine())
    state.load(tmp_path)
    assert state.snapshot.status == "degraded" and state.snapshot.reason == "engine_incompatible"
    assert module.calls == []


@pytest.mark.parametrize("version", ["1.0", "1.1", "1.10"])
def test_compatible_api_version_is_ready(runtime, tmp_path, version):
    _, module = runtime
    module.API_VERSION = version
    state = EngineState(OctantVectorEngine())
    state.load(tmp_path)
    assert state.snapshot.status == "ready"


@pytest.mark.parametrize("upstream,local,method", [("DBError", DBError, "open_db"), ("VersionError", VersionError, "open_db"),
    ("RequestError", RequestError, "recommend"), ("SeedNotInDBError", SeedNotInDBError, "recommend"),
    ("SeedNotEmbeddedError", SeedNotEmbeddedError, "recommend"), ("UnknownPresetError", UnknownPresetError, "expand_preset"),
    ("RequestError", RequestError, "expand_preset"),
    ("VersionError", VersionError, "plan_update"), ("DBError", DBError, "apply_update")])
def test_error_types_are_translated(runtime, tmp_path, upstream, local, method):
    _, module = runtime
    failure = getattr(module, upstream)("synthetic error")
    def fail(*args):
        raise failure
    setattr(module, method, fail)
    args = {"open_db": [tmp_path], "recommend": [module.handle, [], {}, {}], "expand_preset": [module.handle, {"preset": "all"}],
            "plan_update": [{}, None], "apply_update": [[], None, tmp_path]}[method]
    with pytest.raises(local) as caught:
        getattr(OctantVectorEngine(), method)(*args)
    assert type(caught.value) is local and caught.value.__cause__ is failure


@pytest.mark.parametrize("upstream,reason", [("VersionError", "schema_unsupported"), ("DBError", "db_invalid"), ("missing", "db_missing")])
def test_adapter_db_failures_reach_health_and_preserve_signals(runtime, tmp_path, upstream, reason):
    _, module = runtime
    def fail(path):
        raise FileNotFoundError() if upstream == "missing" else getattr(module, upstream)("private")
    module.open_db = fail
    with connected(tmp_path, OctantVectorEngine()) as (client, headers):
        health = client.get("/v1/health").json()
        assert health["status"] == "degraded" and health["reason"] == reason
        assert client.post("/v1/recommendations", json={"count": 2}, headers=headers).status_code == 503
        assert client.get("/v1/presets", headers=headers).json()["error"]["code"] == "not_ready"
        assert client.post("/v1/signals", json={"events": [{"type": "like", "hash": "a" * 40, "at": "2026-10-04T00:00:00Z"}]}, headers=headers).json()["count"] == 1


def test_missing_engine_is_degraded(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "octantvector", None)
    state = EngineState(OctantVectorEngine())
    state.load(tmp_path)
    assert state.snapshot.status == "degraded" and state.snapshot.reason == "engine_unavailable"


@pytest.mark.parametrize("upstream,status,code", [("RequestError", 400, "invalid_request"), ("SeedNotInDBError", 404, "seed_not_found"),
    ("SeedNotEmbeddedError", 404, "seed_not_embedded"), ("unexpected", 500, "internal_error")])
def test_runtime_recommendation_errors_reach_http(runtime, tmp_path, upstream, status, code):
    _, module = runtime
    failure = RuntimeError("private") if upstream == "unexpected" else getattr(module, upstream)("private")
    def fail(*args):
        raise failure
    module.recommend = fail
    with connected(tmp_path, OctantVectorEngine()) as (client, headers):
        response = client.post("/v1/recommendations", json={"count": 1}, headers=headers)
        assert response.status_code == status and response.json()["error"]["code"] == code
        if code != "seed_not_embedded":  # that message names the seed on purpose (api)
            assert "private" not in response.text


def test_version_check_precedes_missing_db(runtime, tmp_path):
    package, module = runtime
    path = tmp_path / "missing"
    module.API_VERSION = "2.0"
    state = EngineState(OctantVectorEngine())
    state.load(path)
    assert state.snapshot.reason == "engine_incompatible"
    module.API_VERSION = "1.0"
    state = EngineState(OctantVectorEngine())
    state.load(path)
    assert state.snapshot.reason == "db_missing"
    assert module.calls == []


def test_adapter_http_roundtrip_preserves_history_params_and_candidates(runtime, tmp_path, example):
    _, module = runtime
    events = example("signals.request.json")["events"]
    params = {"minViewWeight": 0.5, "sessionGapMinutes": 40, "warmupPlays": 4, "future": [1, 2]}
    module.recommendation["items"].reverse()
    with connected(tmp_path, OctantVectorEngine()) as (client, headers):
        assert client.post("/v1/signals", json={"events": events}, headers=headers).status_code == 200
        response = client.post("/v1/recommendations", json={"count": 1, "difficulty": "auto", "params": params}, headers=headers)
        assert response.status_code == 200
        assert response.json()["candidates"] == module.recommendation["items"]
        assert module.calls[-1] == ("recommend", module.handle, events, {"count": 1, "difficulty": "warmup"}, params)


def test_adapter_http_relays_presets_and_expanded_requests(runtime, tmp_path, example):
    _, module = runtime
    request = example("recommendations.request.preset.json")
    module.expansion = {"count": 30, "genre": "jpop", "narrow": {"clap_htsat_unfused": 1}, "params": request["params"]}
    with connected(tmp_path, OctantVectorEngine()) as (client, headers):
        assert client.get("/v1/presets", headers=headers).json() == module.listing
        assert client.get("/v1/health", headers=headers).json()["presetsRevision"] == module.listing["revision"]
        response = client.post("/v1/recommendations", json=request, headers=headers)
        assert response.status_code == 200
        assert module.calls[-2:] == [("expand_preset", module.handle, request),
                                     ("recommend", module.handle, [], {"count": 30, "genre": "jpop", "narrow": {"clap_htsat_unfused": 1}}, request["params"])]
        response = client.post("/v1/recommendations", json={**request, "preset": "pumpup"}, headers=headers)
        assert response.status_code == 400 and response.json() == example("error.unknown_preset.json")


def test_supported_api_and_a_fixed_octantvector():
    import re
    import tomllib
    assert SUPPORTED_API == (1, 0)
    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    source = project["tool"]["uv"]["sources"]["octantvector"]
    assert source["git"] == "https://github.com/BeatOctant/OctantVector.git"
    # A release tag, or one commit between releases (2026-10-19: a release only once several changes gather);
    # never a branch, which would move under the lock.
    assert set(source) in ({"git", "tag"}, {"git", "rev"})
    assert re.fullmatch(r"v\d+\.\d+\.\d+", source["tag"]) if "tag" in source else re.fullmatch(r"[0-9a-f]{40}", source["rev"])


def test_cli_serves_octantvector_by_default_and_not_imported_for_pairing(tmp_path, monkeypatch):
    from octantcompass.cli import main
    import octantcompass.cli as cli
    monkeypatch.setitem(sys.modules, "octantvector", None)
    assert main(["env", "list", "--data-dir", str(tmp_path)]) == 0
    selected = []
    async def serve(config, factory, **_):
        app = factory()
        selected.append(app.state.engine.engine)
        return 0
    monkeypatch.setattr(cli, "serve", serve)
    assert main(["serve", "--data-dir", str(tmp_path)]) == 0
    assert isinstance(selected[-1], OctantVectorEngine)
    assert main(["serve", "--data-dir", str(tmp_path), "--fake-engine"]) == 0
    assert isinstance(selected[-1], FakeVectorEngine)
