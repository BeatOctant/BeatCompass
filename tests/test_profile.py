import threading

import pytest

from octantcompass.engine import DBError, EngineUnavailableError, RequestError
from octantcompass.engine.fake import FakeVectorEngine
from test_recommendations import connected


def test_profile_examples_and_complete_passthrough(tmp_path, example, engine_example):
    expected = engine_example("describe.output.json")
    expected["future"] = {"untouched": [1, 2]}
    engine = FakeVectorEngine(profile=expected)
    with connected(tmp_path, engine) as (client, headers):
        body = example("profile.request.json")
        response = client.post("/v1/profile", json={**body, "future": True}, headers=headers)
        assert response.status_code == 200
        assert response.json() == expected
        # Contract 3 (OctantPath to OctantCompass)'s example gives the display fields; §4.2 also requires
        # retaining the full describe result, including additional fields.
        for key, value in example("profile.response.json").items():
            assert response.json()[key] == value
        assert engine.calls[-1] == ("describe", [], body["params"])
        client.post("/v1/profile", json={}, headers=headers)
        assert engine.calls[-1] == ("describe", [], {})


def test_profile_environment_history_and_params(tmp_path, example):
    engine = FakeVectorEngine()
    with connected(tmp_path, engine) as (client, first):
        token = client.post("/v1/pair", json={"secret": "synthetic", "name": "second"},
                            headers={"OctantPath-Api-Version": "2.0"}).json()["token"]
        second = {**first, "Authorization": f"Bearer {token}"}
        history = example("signals.request.json")["events"]
        assert client.post("/v1/signals", json={"events": history}, headers=first).status_code == 200
        params = {"future": {"x": [True]}, "sessionGapMinutes": -1, "minViewWeight": 0.5}
        for headers, expected in ((first, history), (second, [])):
            assert client.post("/v1/profile", json={"params": params}, headers=headers).status_code == 200
            assert engine.calls[-1] == ("describe", expected, params)


@pytest.mark.parametrize("body", [{"params": None}, {"params": []}, {"params": 1}])
def test_profile_invalid_shape(tmp_path, body):
    engine = FakeVectorEngine()
    with connected(tmp_path, engine) as (client, headers):
        response = client.post("/v1/profile", json=body, headers=headers)
        assert response.status_code == 400
        assert not any(call[0] == "describe" for call in engine.calls)


@pytest.mark.parametrize("failure,status,code", [
    (RequestError("private"), 400, "invalid_request"),
    (DBError("private"), 503, "not_ready"),
    (RuntimeError("private"), 500, "internal_error"),
])
def test_profile_failures_and_recovery(tmp_path, failure, status, code):
    class Engine(FakeVectorEngine):
        def describe(self, *args):
            if self.failure:
                raise self.failure
            return super().describe(*args)
    engine = Engine()
    engine.failure = failure
    with connected(tmp_path, engine) as (client, headers):
        response = client.post("/v1/profile", json={}, headers=headers)
        assert response.status_code == status
        assert response.json()["error"]["code"] == code
        assert "private" not in response.text
        engine.failure = None
        assert client.post("/v1/profile", json={}, headers=headers).status_code == 200


def test_profile_degraded(tmp_path):
    with connected(tmp_path, FakeVectorEngine(error=EngineUnavailableError())) as (client, headers):
        assert client.post("/v1/profile", json={}, headers=headers).status_code == 503


def test_describe_does_not_block_health(tmp_path):
    started, release = threading.Event(), threading.Event()
    class Engine(FakeVectorEngine):
        def describe(self, *args):
            started.set()
            assert release.wait(5)
            return super().describe(*args)
    with connected(tmp_path, Engine()) as (client, headers):
        responses = []
        thread = threading.Thread(target=lambda: responses.append(client.post("/v1/profile", json={}, headers=headers)))
        thread.start()
        try:
            assert started.wait(2)
            assert client.get("/v1/health").json()["status"] == "ready"
        finally:
            release.set()
            thread.join(3)
        assert responses[0].status_code == 200
