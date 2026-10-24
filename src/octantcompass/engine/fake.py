"""Deterministic contract-1 substitute, with caller-supplied synthetic data."""

from copy import deepcopy
from pathlib import Path

from . import UnknownPresetError

# The hands and what they expand to are OctantVector's (contract 1 (OctantCompass to OctantVector) presets / expand_preset);
# OctantCompass only relays them. The fake lists one fixed hand, as OctantVector does for a DB without hands of its own,
# so the page and `serve --fake-engine` have one to show, and expanding it only drops `preset` and `choices`.
PRESETS = {"revision": "fake", "presets": [{"id": "all", "label": {"en": "All", "ja": "すべて"}, "inputs": [{"kind": "seed"}]}]}


class FakeVectorEngine:
    def __init__(self, *, info=None, recommendations=None, profile=None, error=None):
        self.info = deepcopy(info if info is not None else {
            "dbVersion": "fake", "schemaVersion": "1.1", "builtAt": None, "calibrationId": "fake",
            "counts": {}, "views": {}, "skippedViews": [], "genres": [],
        })
        self.recommendations = deepcopy(recommendations if recommendations is not None else {
            "dbVersion": self.info["dbVersion"], "items": [], "aims": {}, "replaced": [], "unknownEvents": 0, "ignoredWeights": [],
        })
        self.profile = deepcopy(profile if profile is not None else {})
        self.error = error
        self.calls = []

    def open_db(self, path):
        self.calls.append(("open_db", Path(path)))
        if self.error:
            raise self.error
        return object()

    def db_info(self, db):
        return deepcopy(self.info)

    def read_info(self, path):
        self.calls.append(("read_info", Path(path)))
        if self.error:
            raise self.error
        return deepcopy(self.info)

    def recommend(self, db, history, request, params):
        self.calls.append(("recommend", deepcopy(history), deepcopy(request), deepcopy(params)))
        return deepcopy(self.recommendations)

    def presets(self, db):
        return deepcopy(PRESETS)

    def expand_preset(self, db, request):
        if request.get("preset") is None:
            return deepcopy(request)
        if request["preset"] not in {preset["id"] for preset in PRESETS["presets"]}:
            raise UnknownPresetError(f"preset {request['preset']} is not in the current list")
        return deepcopy({key: value for key, value in request.items() if key not in ("preset", "choices")})

    def describe(self, db, history, params):
        self.calls.append(("describe", deepcopy(history), deepcopy(params)))
        return deepcopy(self.profile)

    def plan_update(self, index, local_version):
        self.calls.append(("plan_update", deepcopy(index), local_version))
        return deepcopy(index.get("files", []))

    def apply_update(self, files, base, out, trust=None):
        self.calls.append(("apply_update", deepcopy(files), base, out, deepcopy(trust)))
        if self.error:
            raise self.error
        return Path(out)

    def import_archive(self, path, out, trust=None):
        self.calls.append(("import_archive", path, out, deepcopy(trust)))
        if self.error:
            raise self.error
        return Path(out)
