"""Combine caller conditions and session position without evaluating candidates."""

import math
from datetime import datetime, timezone

from .engine import RequestError, VectorEngine


def utc_now():
    return datetime.now(timezone.utc)


def session_play_count(history, params, now):
    # Defaults from OctantSpecs plans/difficulty-fit.md#設定.
    # A request a full gap after the last play starts a new session (OctantSpecs octantcompass/law.md 2.3).
    gap = params.get("sessionGapMinutes", 30)
    if type(gap) not in (int, float) or not math.isfinite(gap) or gap <= 0:
        raise RequestError("sessionGapMinutes must be a positive finite number")
    previous = None
    count = 0
    for event in history:
        if event.get("type") != "play":
            continue
        at = datetime.fromisoformat(event["at"].replace("Z", "+00:00"))
        if previous is not None and (at - previous).total_seconds() / 60 >= gap:
            count = 0
        count += 1
        previous = at
    if previous is not None and (now - previous).total_seconds() / 60 >= gap:
        count = 0
    return count


class RecommendationService:
    def __init__(self, engine: VectorEngine, clock=utc_now):
        self.engine = engine
        self.clock = clock

    def describe(self, db, history, body):
        return self.engine.describe(db, history, body.get("params", {}))

    def recommend(self, db, history, body):
        request = {key: value for key, value in body.items() if key != "params"}
        params = body.get("params", {})
        if request.get("difficulty") == "auto":
            # Default from OctantSpecs plans/difficulty-fit.md#設定.
            warmup = params.get("warmupPlays", 3)
            if type(warmup) is not int or warmup < 0:
                raise RequestError("warmupPlays must be a nonnegative integer")
            session_plays = session_play_count(history, params, self.clock())
            request["difficulty"] = "warmup" if session_plays < warmup else "normal"
        return self.engine.recommend(db, history, request, params)
