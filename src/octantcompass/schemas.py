"""HTTP request shapes from OctantSpecs contracts/examples/mod-api; unknown fields ignored."""

from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, FiniteFloat, StrictBool, StrictInt, StrictStr, model_validator


class WireModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


# Contract 3 sends hashes in lowercase, but games and mods (SongCore on Quest) use uppercase.
# Accept both and compare in lowercase (OctantSpecs octantpath/law.md 1.2).
Hash = Annotated[StrictStr, AfterValidator(str.lower)]


class Chart(WireModel):
    hash: Hash
    characteristic: StrictStr
    difficulty: StrictStr


class Exclusion(WireModel):
    hash: Hash
    characteristic: StrictStr = None
    difficulty: StrictStr = None
    reason: StrictStr = None

    @model_validator(mode="after")
    def chart_fields_together(self):
        if ("characteristic" in self.model_fields_set) != ("difficulty" in self.model_fields_set):
            raise ValueError("chart exclusions need both characteristic and difficulty")
        return self


Number = StrictInt | Annotated[FiniteFloat, Field(strict=True)]


class RecommendationRequest(WireModel):
    count: Annotated[StrictInt, Field(ge=0)]
    preset: StrictStr = None
    choices: dict[str, StrictStr] = None
    seed: Chart = None
    genre: StrictStr = None
    weights: dict[str, Number] = None
    difficulty: StrictStr | Annotated[Number, Field(ge=0, le=1)] = None
    chartsPerMap: Annotated[StrictInt, Field(ge=1)] = None
    exclude: list[Exclusion] = None
    params: dict[str, Any] = None


class SignalsRequest(WireModel):
    # Validate each entry separately: a malformed event must not discard its batch.
    events: list[Any]


class ProfileRequest(WireModel):
    params: dict[str, Any] = None


class SignalEvent(WireModel):
    hash: Hash
    type: StrictStr
    at: StrictStr

    @model_validator(mode="after")
    def utc_timestamp(self):
        from datetime import datetime, timedelta

        at = datetime.fromisoformat(self.at.replace("Z", "+00:00"))
        if "T" not in self.at or at.utcoffset() != timedelta(0):
            raise ValueError("at must be an ISO 8601 UTC timestamp")
        return self


class ChartSignal(SignalEvent):
    characteristic: StrictStr
    difficulty: StrictStr


class PlayModifiers(WireModel):
    speed: Annotated[Number, Field(gt=0)] = None
    noFail: StrictBool = None


class PlaySignal(ChartSignal):
    result: StrictStr
    modifiers: PlayModifiers
    accuracy: Annotated[Number, Field(ge=0, le=1)] = None

    @model_validator(mode="after")
    def known_result(self):
        if self.result not in {"clear", "fail", "quit", "restart"}:
            raise ValueError("unknown play result")
        return self


class ImportedPlaySignal(ChartSignal):
    source: StrictStr
    count: Annotated[StrictInt, Field(ge=0)]


class RecommendationSignal(ChartSignal):
    requestId: StrictStr
    stage: StrictStr


def validate_events(events):
    from pydantic import ValidationError

    models = {"play": PlaySignal, "imported_play": ImportedPlaySignal,
              "recommendation": RecommendationSignal,
              **{name: SignalEvent for name in ("upvote", "downvote", "like", "like_removed", "user_delete")}}
    validated = []
    for event in events:
        kind = event.get("type") if isinstance(event, dict) else None
        model = models.get(kind) if isinstance(kind, str) else None
        reason = None
        if model is None:
            # A type this contract 3 (OctantPath to OctantCompass) version lacks, or an event of another shape, is kept
            # as sent but out of the history (OctantSpecs contracts/mod-api.md, 版の食い違い). Neither
            # stops the rest: OctantPath sends the whole record again to a newer host anyway.
            reason = "unsupported_type" if isinstance(kind, str) else "invalid_event"
        else:
            try:
                model.model_validate(event)
            except (ValidationError, ValueError):
                reason = "invalid_event"
        # Keep the original JSON, including future fields and old hashes.
        validated.append((event, reason))
    return validated
