import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from octantcompass.api import create_app
from octantcompass.config import Config
from octantcompass.engine import DBError, EngineState, EngineUnavailableError, RequestError, SeedNotInDBError
from octantcompass.engine.fake import FakeVectorEngine
from octantcompass.pairing import Pairing
from octantcompass.service import RecommendationService
from conftest import standing_secret


# Shortly after the plays in examples/contract3/signals.request.json, within one session.
EXAMPLE_NOW = datetime(2026, 10, 4, 12, 10, tzinfo=timezone.utc)


@contextmanager
def connected(tmp_path, engine, clock=lambda: EXAMPLE_NOW):
    pairing = Pairing(tmp_path / "pairing.json")
    standing_secret(pairing, "synthetic")
    token = pairing.pair("synthetic", "synthetic environment")
    headers = {"OctantPath-Api-Version": "2.0", "Authorization": f"Bearer {token}"}
    with TestClient(create_app(Config(tmp_path, idle_minutes=0), pairing, EngineState(engine), clock=clock)) as client:
        deadline = time.monotonic() + 3
        while client.get("/v1/health").json()["status"] == "loading":
            assert time.monotonic() < deadline
            time.sleep(0.01)
        yield client, headers


@pytest.mark.parametrize("name", ["recommendations.request.json", "recommendations.request.explicit.json", "recommendations.request.clearchance.json"])
def test_request_examples_and_response_passthrough(tmp_path, example, engine_example, name):
    response_example = example("recommendations.response.json")
    engine = FakeVectorEngine(recommendations=engine_example("recommend.output.json"))
    request = example(name)
    with connected(tmp_path, engine) as (client, headers):
        response = client.post("/v1/recommendations", json=request, headers=headers)
        assert response.status_code == 200
        result = response.json()
        UUID(result["requestId"])
        response_example["requestId"] = result["requestId"]
        assert result == response_example
        call = engine.calls[-1]
        assert call[0] == "recommend" and call[1] == []
        expected_request = {key: value for key, value in request.items() if key != "params"}
        if expected_request["difficulty"] == "auto":
            expected_request["difficulty"] = "warmup"
        assert call[2] == expected_request
        assert call[3] == request.get("params", {})
        next_response = client.post("/v1/recommendations", json=request, headers=headers).json()
        assert next_response["requestId"] != result["requestId"]


def test_candidates_not_sorted_scored_trimmed_or_filled(tmp_path, engine_example):
    expected = engine_example("recommend.output.json")
    expected["items"].reverse()
    expected["items"][0]["rank"] = 8
    expected["items"][0]["future"] = {"untouched": True}
    engine = FakeVectorEngine(recommendations=expected)
    with connected(tmp_path, engine) as (client, headers):
        for count in (1, 100):
            response = client.post("/v1/recommendations", json={"count": count}, headers=headers)
            assert response.status_code == 200
            assert response.json()["candidates"] == expected["items"]
            assert engine.calls[-1][2] == {"count": count}
    expected["items"] = []
    engine = FakeVectorEngine(recommendations=expected)
    with connected(tmp_path, engine) as (client, headers):
        assert client.post("/v1/recommendations", json={"count": 20}, headers=headers).json()["candidates"] == []


def test_params_request_omissions_and_unknown_fields(tmp_path):
    engine = FakeVectorEngine()
    params = {"minViewWeight": 0.5, "future": {"nested": [1, "two"]}, "k": 8}
    with connected(tmp_path, engine) as (client, headers):
        request = {"count": 4, "params": params, "futureTopLevel": "ignored", "weights": {"music": 1}}
        assert client.post("/v1/recommendations", json=request, headers=headers).status_code == 200
        assert engine.calls[-1][2] == {"count": 4, "weights": {"music": 1}}
        assert type(engine.calls[-1][2]["weights"]["music"]) is int
        assert engine.calls[-1][3] == params
        assert client.post("/v1/recommendations", json={"count": 1}, headers=headers).status_code == 200
        assert engine.calls[-1][2] == {"count": 1}
        assert engine.calls[-1][3] == {}


@pytest.mark.parametrize("body", [
    {}, {"count": "2"}, {"count": True}, {"count": -1}, {"count": 2, "difficulty": True},
    {"count": 2, "difficulty": 1.1}, {"count": 2, "difficulty": -0.1},
    {"count": 2, "weights": {"music": "1"}}, {"count": 2, "weights": {"music": True}},
    {"count": 2, "seed": {"hash": "synthetic"}}, {"count": 2, "params": []},
    {"count": 2, "exclude": [{"hash": "synthetic", "difficulty": "Expert"}]},
    {"count": 2, "chartsPerMap": 0}, {"count": 2, "seed": None},
    {"count": 2, "difficulty": "auto", "params": {"warmupPlays": -1}},
])
def test_invalid_requests_do_not_reach_engine(tmp_path, body):
    engine = FakeVectorEngine()
    with connected(tmp_path, engine) as (client, headers):
        response = client.post("/v1/recommendations", json=body, headers=headers)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_request"
        assert not any(call[0] == "recommend" for call in engine.calls)


@pytest.mark.parametrize("exception,status,code", [
    (RequestError("private details"), 400, "invalid_request"), (SeedNotInDBError("private details"), 404, "seed_not_found"),
    # A DB the engine cannot use is not ready, as for the profile (contract 3).
    (DBError("private details"), 503, "not_ready"), (RuntimeError("private details"), 500, "internal_error")])
def test_engine_error_mapping_and_next_request(tmp_path, exception, status, code):
    class Engine(FakeVectorEngine):
        def recommend(self, *args):
            if self.failure:
                raise self.failure
            return super().recommend(*args)

    engine = Engine()
    engine.failure = exception
    with connected(tmp_path, engine) as (client, headers):
        response = client.post("/v1/recommendations", json={"count": 3, "genre": "future-genre"}, headers=headers)
        assert response.status_code == status
        assert response.json()["error"]["code"] == code
        assert "private details" not in response.text
        engine.failure = None
        assert client.post("/v1/recommendations", json={"count": 3}, headers=headers).status_code == 200


def test_degraded_recommendation_returns_503(tmp_path):
    with connected(tmp_path, FakeVectorEngine(error=EngineUnavailableError())) as (client, headers):
        response = client.post("/v1/recommendations", json={"count": 3}, headers=headers)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "not_ready"


def test_loading_recommendation_returns_503_and_engine_does_not_block_health(tmp_path):
    load_release = threading.Event()
    load_started = threading.Event()
    recommend_release = threading.Event()
    recommend_started = threading.Event()

    class Engine(FakeVectorEngine):
        def open_db(self, path):
            load_started.set()
            load_release.wait(timeout=5)
            return super().open_db(path)

        def recommend(self, *args):
            recommend_started.set()
            recommend_release.wait(timeout=5)
            return super().recommend(*args)

    pairing = Pairing(tmp_path / "pairing.json")
    standing_secret(pairing, "synthetic")
    token = pairing.pair("synthetic", "synthetic")
    headers = {"OctantPath-Api-Version": "2.0", "Authorization": f"Bearer {token}"}
    with TestClient(create_app(Config(tmp_path, idle_minutes=0), pairing, EngineState(Engine()))) as client:
        try:
            assert load_started.wait(timeout=2)
            response = client.post("/v1/recommendations", json={"count": 3}, headers=headers)
            assert response.status_code == 503
            assert client.post("/v1/profile", json={}, headers=headers).status_code == 503
        finally:
            load_release.set()
        deadline = time.monotonic() + 3
        while client.get("/v1/health").json()["status"] == "loading":
            assert time.monotonic() < deadline
            time.sleep(0.01)
        result = []
        thread = threading.Thread(target=lambda: result.append(client.post("/v1/recommendations", json={"count": 3}, headers=headers)))
        thread.start()
        try:
            assert recommend_started.wait(timeout=2)
            assert client.get("/v1/health").json()["status"] == "ready"
        finally:
            recommend_release.set()
            thread.join(timeout=3)
        assert result[0].status_code == 200


def test_service_aims_and_history_are_passed_without_filtering(example):
    engine = FakeVectorEngine()
    history = example("signals.request.json")["events"]  # two plays in the session at EXAMPLE_NOW
    later = EXAMPLE_NOW + timedelta(hours=1)  # a new session, no plays yet
    for now, warmup, expected in ((later, 3, "warmup"), (EXAMPLE_NOW, 3, "warmup"), (EXAMPLE_NOW, 2, "normal"),
                                  (later, 0, "normal"), (EXAMPLE_NOW, 8, "warmup")):
        params = {"warmupPlays": warmup, "sessionGapMinutes": 47, "minViewWeight": 0.5}
        RecommendationService(engine, lambda: now).recommend(object(), history, {"count": 4, "difficulty": "auto", "params": params})
        assert engine.calls[-1][1] == history
        assert engine.calls[-1][2]["difficulty"] == expected
        assert engine.calls[-1][3] == params
    service = RecommendationService(engine, lambda: later)
    for difficulty in ("challenge", "normal", "warmup", "future-aim", 0, 0.6, 1):
        service.recommend(object(), history, {"count": 4, "difficulty": difficulty})
        assert engine.calls[-1][2]["difficulty"] == difficulty


def test_hashes_are_compared_in_lowercase(tmp_path):
    # SongCore on Quest gives uppercase hashes; contract 3 sends lowercase. Accept both.
    engine = FakeVectorEngine()
    upper = "8DFC3BAEFA47765DDA2918619E772FB01ACDA858"
    with connected(tmp_path, engine) as (client, headers):
        request = {"count": 2, "seed": {"hash": upper, "characteristic": "Standard", "difficulty": "Hard"},
                   "exclude": [{"hash": upper}]}
        assert client.post("/v1/recommendations", json=request, headers=headers).status_code == 200
        assert engine.calls[-1][2]["seed"]["hash"] == upper.lower()
        assert engine.calls[-1][2]["exclude"][0]["hash"] == upper.lower()
