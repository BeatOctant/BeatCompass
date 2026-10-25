from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from octantcompass.cache import SignalCache
from octantcompass.engine import EngineUnavailableError
from octantcompass.engine.fake import FakeVectorEngine
from octantcompass.schemas import validate_events
from octantcompass.service import RecommendationService
from test_recommendations import connected


def play(at, result="clear"):
    return {"type": "play", "hash": "a" * 40,
            "characteristic": "OneSaber", "difficulty": "Expert", "at": at,
            "result": result, "modifiers": {"speed": 1.5, "noFail": True}}


def send(client, headers, events, *, replace=False):
    method = client.put if replace else client.post
    return method("/v1/signals", json={"events": events}, headers=headers)


def history(client):
    cache = client.app.state.cache
    return cache.history(next(iter(cache._events)))


def test_examples_history_granularity_and_new_signals(tmp_path, example):
    engine = FakeVectorEngine()
    events = example("signals.request.json")["events"]
    events[0]["future"] = {"keep": [1, 2]}
    with connected(tmp_path, engine) as (client, headers):
        response = send(client, headers, events)
        assert response.status_code == 200
        assert response.json() == example("signals.response.json")
        assert client.get("/v1/health", headers=headers).json()["signals"] == 9
        assert client.post("/v1/recommendations", json={"count": 2, "difficulty": "auto"}, headers=headers).status_code == 200
        assert engine.calls[-1][1] == events
        assert engine.calls[-1][2]["difficulty"] == "warmup"
        event = play("2026-10-04T12:07:00Z", "quit")
        assert send(client, headers, [event]).json()["count"] == 10
        params = {"minViewWeight": 0.5, "sessionGapMinutes": 30, "warmupPlays": 3, "future": True}
        client.post("/v1/recommendations", json={"count": 2, "difficulty": "auto", "params": params}, headers=headers)
        assert engine.calls[-1][1] == events + [event]
        assert engine.calls[-1][2]["difficulty"] == "normal"
        assert engine.calls[-1][3] == params


def test_environment_isolation_and_instance_id(tmp_path, example):
    engine = FakeVectorEngine()
    events = example("signals.request.json")["events"]
    with connected(tmp_path, engine) as (client, first):
        token = client.post("/v1/pair", json={"secret": "synthetic", "name": "other"}, headers=first).json()["token"]
        second = {**first, "Authorization": f"Bearer {token}"}
        send(client, first, events)
        other = [play("2026-10-04T13:00:00Z")]
        send(client, second, other)
        instance = client.get("/v1/health", headers=first).json()["instanceId"]
        for headers, history_ in ((first, events), (second, other)):
            health = client.get("/v1/health", headers=headers).json()
            assert health["signals"] == len(history_) and health["instanceId"] == instance
            client.post("/v1/recommendations", json={"count": 1}, headers=headers)
            assert engine.calls[-1][1] == history_
    # Another run of the host holds nothing and tells OctantPath so with another instanceId.
    with connected(tmp_path, engine) as (client, first):
        health = client.get("/v1/health", headers=first).json()
        assert health["paired"] and health["signals"] == 0 and health["instanceId"] != instance


def test_replace_then_append(tmp_path, example):
    # OctantPath sends its whole record when it meets a run of the host, then only what it adds.
    events = example("signals.request.json")["events"]
    with connected(tmp_path, FakeVectorEngine()) as (client, headers):
        assert send(client, headers, events[:5]).json() == {"count": 5, "rejected": []}
        assert send(client, headers, events, replace=True).json() == {"count": 9, "rejected": []}
        assert history(client) == events
        later = play("2026-10-04T13:00:00Z")
        assert send(client, headers, [later]).json() == {"count": 10, "rejected": []}
        assert history(client) == events + [later]
        # A copied game folder under the same token replaces the history with its own record.
        copy = events[:3] + [play("2026-10-05T10:00:00Z")]
        assert send(client, headers, copy, replace=True).json()["count"] == 4
        assert history(client) == copy
        assert client.get("/v1/health", headers=headers).json()["signals"] == 4


def test_unsupported_type_is_counted_but_kept_out_of_the_history(tmp_path):
    first, later = play("2026-10-04T13:00:00Z"), play("2026-10-04T13:02:00Z")
    future = {"type": "future_signal", "hash": "c" * 40, "at": "2026-10-04T13:01:00Z"}
    with connected(tmp_path, FakeVectorEngine()) as (client, headers):
        result = send(client, headers, [first, future, later]).json()
        assert result == {"count": 3, "rejected": [{"index": 1, "reason": "unsupported_type"}]}
        assert client.get("/v1/health", headers=headers).json()["signals"] == 3
        assert history(client) == [first, later]


@pytest.mark.parametrize("event", [None, [], {}, {"type": []},
    play("2026-10-04T13:00:00"), play("2026-10-04T13:00:00+01:00"),
    play("invalid"), play("2026-10-04T13:00:00Z", "invalid"),
    {"type": "imported_play", "source": "PlayerData", "hash": "c" * 40, "characteristic": "Standard",
     "difficulty": "Expert", "count": 2, "cleared": "yes", "at": "2026-10-04T13:00:00Z"}])
def test_malformed_events_are_isolated(tmp_path, event):
    with connected(tmp_path, FakeVectorEngine()) as (client, headers):
        response = send(client, headers, [event])
        assert response.status_code == 200
        assert response.json() == {"count": 1, "rejected": [{"index": 0, "reason": "invalid_event"}]}
        assert history(client) == []


@pytest.mark.parametrize("engine", [FakeVectorEngine(error=EngineUnavailableError()), FakeVectorEngine()])
def test_signals_do_not_require_ready_db(tmp_path, engine):
    with connected(tmp_path, engine) as (client, headers):
        # Explicitly hold the state in loading as well as checking degraded.
        if engine.error is not None:
            assert client.get("/v1/health").json()["status"] == "degraded"
        else:
            from octantcompass.engine import Snapshot
            client.app.state.engine.snapshot = Snapshot()
        assert send(client, headers, [play("2026-10-04T13:00:00Z")]).json()["count"] == 1


def test_concurrent_sends_and_deletion(example):
    events = example("signals.request.json")["events"]
    cache = SignalCache()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: cache.receive("env", validate_events(events)), range(4)))
    assert sorted(result["count"] for result in results) == [9, 18, 27, 36]
    assert cache.history("env") == events * 4
    assert cache.receive("env", validate_events(events), replace=True)["count"] == 9
    cache.delete("env")
    assert cache.count("env") == 0 and cache.history("env") == []


@pytest.mark.parametrize("gap,warmup,expected", [(30, 3, "warmup"), (31, 3, "normal"), (30, 1, "normal"), (30, 0, "normal")])
def test_session_gap_params_and_explicit_aims(gap, warmup, expected):
    history = [play("2026-10-04T13:00:00Z", "fail"), play("2026-10-04T13:01:00Z", "restart"),
               {"type": "imported_play", "count": 1000},
               play("2026-10-04T13:31:00Z", "quit")]
    engine = FakeVectorEngine()
    service = RecommendationService(engine, lambda: datetime(2026, 10, 4, 13, 35, tzinfo=timezone.utc))
    params = {"sessionGapMinutes": gap, "warmupPlays": warmup}
    service.recommend(object(), history, {"count": 1, "difficulty": "auto", "params": params})
    assert engine.calls[-1][1] == history
    assert engine.calls[-1][2]["difficulty"] == expected
    assert engine.calls[-1][3] == params
    for aim in ("normal", "warmup", "challenge", 0.7):
        service.recommend(object(), history, {"count": 1, "difficulty": aim, "params": params})
        assert engine.calls[-1][2]["difficulty"] == aim


@pytest.mark.parametrize("minutes,expected", [(29, "normal"), (30, "warmup"), (60 * 24, "warmup")])
def test_request_after_session_gap_starts_new_session(minutes, expected):
    # difficulty-fit.md: a gap since the last play starts a new session, even before the next play.
    history = [play(f"2026-10-04T13:0{minute}:00Z") for minute in (1, 2, 3)]
    last = datetime(2026, 10, 4, 13, 3, tzinfo=timezone.utc)
    engine = FakeVectorEngine()
    service = RecommendationService(engine, lambda: last + timedelta(minutes=minutes))
    service.recommend(object(), history, {"count": 1, "difficulty": "auto", "params": {"sessionGapMinutes": 30, "warmupPlays": 3}})
    assert engine.calls[-1][2]["difficulty"] == expected


def test_new_session_over_http_uses_request_time(tmp_path, example):
    events = example("signals.request.json")["events"] + [play("2026-10-04T12:07:00Z")]
    later = datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)
    for clock, expected in ((None, "normal"), (lambda: later, "warmup")):
        engine = FakeVectorEngine()
        options = {} if clock is None else {"clock": clock}
        with connected(tmp_path / expected, engine, **options) as (client, headers):
            send(client, headers, events)
            client.post("/v1/recommendations", json={"count": 1, "difficulty": "auto"}, headers=headers)
            assert engine.calls[-1][2]["difficulty"] == expected


@pytest.mark.parametrize("gap", [0, -1, True, "30", None])
def test_invalid_session_params_return_400(tmp_path, gap):
    with connected(tmp_path, FakeVectorEngine()) as (client, headers):
        response = client.post("/v1/recommendations", json={"count": 1, "difficulty": "auto", "params": {"sessionGapMinutes": gap}}, headers=headers)
        assert response.status_code == 400


def test_all_map_signals_preserve_original_fields(tmp_path):
    events = [{"type": kind, "hash": str(index) * 40,
               "at": "2026-10-04T13:00:00Z", "future": ["kept"], "characteristic": {"future": True}}
              for index, kind in enumerate(("upvote", "downvote", "like", "like_removed", "user_delete"), start=1)]
    with connected(tmp_path, FakeVectorEngine()) as (client, headers):
        assert send(client, headers, events).json() == {"count": 5, "rejected": []}
        assert history(client) == events


def test_invalid_shape_does_not_block_later_valid_signal(tmp_path):
    first = play("2026-10-04T13:00:00Z")
    first["modifiers"]["noFail"] = "false"
    second = play("2026-10-04T13:01:00Z")
    with connected(tmp_path, FakeVectorEngine()) as (client, headers):
        assert send(client, headers, [first, second]).json() == {"count": 2, "rejected": [{"index": 0, "reason": "invalid_event"}]}
        assert history(client) == [second]
        for body in ({}, {"events": None}, {"events": {}}, {"events": "bad"}):
            assert client.post("/v1/signals", json=body, headers=headers).status_code == 400
            assert client.put("/v1/signals", json=body, headers=headers).status_code == 400
        assert send(client, headers, []).json() == {"count": 2, "rejected": []}
        assert send(client, headers, [], replace=True).json() == {"count": 0, "rejected": []}


def test_uppercase_hashes_reach_the_history_in_lowercase(tmp_path):
    engine = FakeVectorEngine()
    event = play("2026-10-04T12:00:00Z")
    event["hash"] = "A" * 40
    with connected(tmp_path, engine) as (client, headers):
        assert send(client, headers, [event]).json()["count"] == 1
        client.post("/v1/recommendations", json={"count": 2}, headers=headers)
        assert engine.calls[-1][1] == [dict(event, hash="a" * 40)]
