"""Presets (OctantSpecs plans/presets.md): the list, and turning a preset into contract 1 (OctantCompass to OctantVector) conditions."""

import re
from copy import deepcopy

from octantcompass.engine import RequestError, UnknownPresetError
from octantcompass.engine.fake import FakeVectorEngine

from test_recommendations import connected


class Relay(FakeVectorEngine):
    """An engine with given presets() and expand_preset() results, to see what OctantCompass does with them."""

    def __init__(self, listing, expansion=None, failure=None, **kwargs):
        super().__init__(**kwargs)
        self.listing, self.expansion, self.failure = listing, expansion, failure
        self.expanded = []

    def presets(self, db):
        return deepcopy(self.listing)

    def expand_preset(self, db, request):
        self.expanded.append(deepcopy(request))
        if self.failure is not None:
            raise self.failure
        return deepcopy(self.expansion) if "preset" in request else request


def presets_of(client, headers):
    response = client.get("/v1/presets", headers=headers)
    assert response.status_code == 200
    return response.json()


def assert_text(value):
    # Display text is a string or {language: string} (contract 3 (OctantPath to OctantCompass) LocalizedText).
    assert isinstance(value, str) or (isinstance(value, dict) and value and all(isinstance(v, str) for v in value.values()))


def assert_presets_shape(listing):
    """Contract 3 PresetsResponse, which contract 1 presets(db) returns as is."""
    assert isinstance(listing["revision"], str) and isinstance(listing["presets"], list)
    for preset in listing["presets"]:
        assert {"id", "label", "inputs"} <= preset.keys() <= {"id", "label", "group", "hint", "inputs"}
        assert re.fullmatch(r"[a-z0-9_-]+", preset["id"])
        for key in ("label", "group", "hint"):
            if key in preset:
                assert_text(preset[key])
        for item in preset["inputs"]:
            assert isinstance(item["kind"], str) and isinstance(item.get("optional", False), bool)
            if item["kind"] == "choice":
                assert isinstance(item["name"], str) and item["options"]
                if "label" in item:
                    assert_text(item["label"])
                for option in item["options"]:
                    assert isinstance(option["value"], str)
                    assert_text(option["label"])


def test_presets_and_revision_are_relayed_unchanged(tmp_path, example):
    listing = example("presets.response.json")
    assert_presets_shape(listing)
    # Display text may be {language: string}; OctantPath picks the language (contract 3), OctantCompass does not look.
    listing["presets"][0].update(label={"en": "All", "ja": "すべて"}, group={"en": "Songs and charts", "ja": "曲と譜面"})
    with connected(tmp_path, Relay(listing)) as (client, headers):
        assert presets_of(client, headers) == listing
        assert client.get("/v1/health", headers=headers).json()["presetsRevision"] == listing["revision"]


def test_preset_request_is_expanded_by_the_engine_then_relayed(tmp_path, example, engine_example):
    request = example("recommendations.request.preset.json")
    expansion = {**{key: value for key, value in request.items() if key not in ("preset", "choices")},
                 "genre": "jpop", "narrow": {"clap_htsat_unfused": 1}, "weights": {"chart_patterns": 1}, "difficulty": "auto",
                 "params": {**request["params"], "narrowTopMaps": 50}}
    engine = Relay(example("presets.response.json"), expansion, recommendations=engine_example("recommend.output.json"))
    with connected(tmp_path, engine) as (client, headers):
        response = client.post("/v1/recommendations", json=request, headers=headers)
        assert response.status_code == 200
        assert engine.expanded == [request]
        # OctantCompass adds only the aim for `auto`; the expanded conditions and params reach recommend unchanged.
        conditions = {key: value for key, value in expansion.items() if key != "params"}
        assert engine.calls[-1] == ("recommend", [], {**conditions, "difficulty": "warmup"}, expansion["params"])
        expected = example("recommendations.response.json")
        expected["requestId"] = response.json()["requestId"]
        assert response.json() == expected

        engine.expansion = {"count": 5, "params": {"includeKnown": True}}
        assert client.post("/v1/recommendations", json={"count": 5, "preset": "again"}, headers=headers).status_code == 200
        assert engine.calls[-1] == ("recommend", [], {"count": 5}, {"includeKnown": True})

        assert client.post("/v1/recommendations", json={"count": 2}, headers=headers).status_code == 200
        assert engine.expanded[-1] == {"count": 2} and engine.calls[-1] == ("recommend", [], {"count": 2}, {})


def test_unknown_preset_and_missing_inputs_stop_before_recommend(tmp_path, example):
    engine = Relay(example("presets.response.json"), failure=UnknownPresetError("pumpup"))
    with connected(tmp_path, engine) as (client, headers):
        response = client.post("/v1/recommendations", json={"count": 5, "preset": "pumpup"}, headers=headers)
        assert response.status_code == 400 and response.json() == example("error.unknown_preset.json")
        engine.failure = RequestError("preset song needs a seed")
        response = client.post("/v1/recommendations", json={"count": 5, "preset": "song"}, headers=headers)
        assert response.status_code == 400 and response.json()["error"]["code"] == "invalid_request"
        assert "needs a seed" not in response.text
        # A null seed is no chart (contract 3): rejected before the engine sees the request.
        expanded = len(engine.expanded)
        response = client.post("/v1/recommendations", json={"count": 5, "preset": "challenge", "seed": None}, headers=headers)
        assert response.status_code == 400 and response.json()["error"]["code"] == "invalid_request"
        assert len(engine.expanded) == expanded
        assert not [call for call in engine.calls if call[0] == "recommend"]


def test_seed_without_vectors_is_reported(tmp_path):
    from octantcompass.engine import SeedNotEmbeddedError

    class Bare(FakeVectorEngine):
        def recommend(self, db, history, request, params):
            raise SeedNotEmbeddedError("seed chart x has no vector in DB fake")

    seed = {"hash": "a" * 40, "characteristic": "Standard", "difficulty": "Expert"}
    with connected(tmp_path, Bare()) as (client, headers):
        response = client.post("/v1/recommendations", json={"count": 5, "preset": "all", "seed": seed}, headers=headers)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "seed_not_embedded"
