"""HTTP endpoints; see OctantSpecs contract 3 (OctantPath to OctantCompass) for the wire contract."""

import asyncio
import gc
import time
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StrictStr

from . import __version__
from .activity import RecentLog, log
from .cache import SignalCache
from .config import LiveConfig
from .engine import DBError, RequestError, SeedNotEmbeddedError, SeedNotInDBError, UnknownPresetError
from .schemas import ProfileRequest, RecommendationRequest, SignalsRequest, validate_events
from .service import RecommendationService, utc_now
from .manage import create_router, new_key

API_VERSION = "2.0"


class PairRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    secret: StrictStr
    name: StrictStr


def error(status, code, message, **details):
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message, **details}})


# Contract 1 (OctantCompass to OctantVector) errors as contract 3 (OctantPath to OctantCompass) errors (its table of failures),
# for every route that asks the engine. The first row that matches is used: a SeedNotEmbeddedError is also a
# SeedNotInDBError, and those and UnknownPresetError are RequestErrors. The engine's own text stays out of the
# response, except which seed has no vector.
ENGINE_ERRORS = (
    (UnknownPresetError, 400, "unknown_preset", "preset '{preset}' is not in the current list"),
    # Listed in the DB but no vector to compare (the encoders have not reached it): say so instead of an empty list
    (SeedNotEmbeddedError, 404, "seed_not_embedded", "the seed chart has no vector in the database yet: {exc}"),
    (SeedNotInDBError, 404, "seed_not_found", "the seed chart was not found"),
    (RequestError, 400, "invalid_request", "the request does not match the database"),
    (DBError, 503, "not_ready", "the recommendation database is not ready"),
)


def engine_error(exc, preset=None):
    for kind, status, code, message in ENGINE_ERRORS:
        if isinstance(exc, kind):
            return error(status, code, message.format(preset=preset, exc=exc))
    raise exc


def bearer(request):
    parts = request.headers.get("authorization", "").split()
    return parts[1] if len(parts) == 2 and parts[0].lower() == "bearer" else None


def roles(views):
    """Contract 3 1.2 `db.roles`: role name -> ids of its views, from contract 1 (OctantCompass to OctantVector) info() views."""
    out = {}
    for view, meta in views.items():
        if meta.get("role"):
            out.setdefault(meta["role"], []).append(view)
    return out


def create_app(config, pairing, state, *, clock=utc_now, watch_db=None):
    """config: a Config, or the LiveConfig whose settings the window may change while the host runs."""
    live = config if isinstance(config, LiveConfig) else LiveConfig(config)
    config = live.current  # the places and the first DB do not change while the host runs
    cache = SignalCache()
    recent = RecentLog(config.log_lines)

    def loaded():
        snapshot = state.snapshot
        if snapshot.status == "ready":
            log.info("DB loaded: %s (%s)", snapshot.info["dbVersion"], snapshot.path)
        else:
            log.warning("DB not ready: %s (%s)", snapshot.reason, snapshot.path)

    async def load():
        if live.current.idle_minutes:
            # Contract 6 (client to OctantCompass) 2.0: a host that stays running holds no DB until it is used.
            state.rest(config.db)
            log.info("started without the DB; it loads when a paired environment asks (%s)", config.db)
            return
        await asyncio.to_thread(state.load, config.db)
        loaded()

    last_use = [time.monotonic()]
    waking = []  # the running load of a resting DB, so it is not dropped while it runs

    def use():
        """A paired environment (or the window) needs the DB: keep it, or load it again if it rests."""
        last_use[0] = time.monotonic()
        if state.snapshot.resting and not waking:
            log.info("loading the DB again for a request")
            task = asyncio.get_running_loop().create_task(asyncio.to_thread(state.wake))
            waking.append(task)
            task.add_done_callback(lambda done: (waking.remove(done), loaded()))

    async def rest_when_idle():
        while True:
            minutes = live.current.idle_minutes
            await asyncio.sleep(min(60, minutes * 60 / 4) if minutes else 60)
            if (minutes and state.snapshot.status == "ready" and not waking
                    and time.monotonic() - last_use[0] >= minutes * 60 and state.rest(config.db)):
                await asyncio.to_thread(gc.collect)
                log.info("let go of the DB after %g minutes without requests", minutes)

    @asynccontextmanager
    async def lifespan(app):
        log.addHandler(recent)
        task = asyncio.create_task(load())
        resting = asyncio.create_task(rest_when_idle())
        async def watch_after_load():
            await task
            await watch_db()
        watcher = asyncio.create_task(watch_after_load()) if watch_db is not None else None
        try:
            yield
        finally:
            await task
            resting.cancel()
            for running in list(waking):
                await running
            if watcher is not None:
                watcher.cancel()
                try:
                    await watcher
                except asyncio.CancelledError:
                    pass
            log.removeHandler(recent)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.engine = state
    app.state.cache = cache
    app.state.config = live
    app.state.manage_key = new_key()  # the management API's key for this run (manage.py), in connection.json

    @app.middleware("http")
    async def contract_guard(request, call_next):
        # The management API (/manage/) is not contract 3; it has its own guard (manage.py).
        if request.url.path != "/v1/health" and request.url.path != "/manage" and not request.url.path.startswith("/manage/"):
            version = request.headers.get("OctantPath-Api-Version")
            if version is None or version.split(".")[0] != API_VERSION.split(".")[0]:
                return error(426, "version_mismatch", "OctantCompass and OctantPath API versions do not match",
                             apiVersion=API_VERSION, requestApiVersion=version)
            if request.url.path != "/v1/pair":
                environment = await asyncio.to_thread(pairing.authenticate, bearer(request))
                if environment is None:
                    return error(401, "unauthorized", "the token is missing or revoked; pair again")
                request.state.environment = environment
                use()
        try:
            return await call_next(request)
        except Exception as exc:
            log.error("%s %s failed: %s", request.method, request.url.path, type(exc).__name__)
            return error(500, "internal_error", "the request could not be completed")

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return error(400, "invalid_request", "the request shape is invalid")

    @app.get("/v1/health")
    async def health(request: Request):
        environment = await asyncio.to_thread(pairing.authenticate, bearer(request))
        if environment:
            use()  # only a paired environment loads a resting DB, not anyone who can reach the port
        snapshot = state.snapshot
        info = None if snapshot.resting else snapshot.info
        result = {"apiVersion": API_VERSION, "hostVersion": __version__, "status": snapshot.status,
                  "db": None if info is None else {"dbVersion": info["dbVersion"], "schemaVersion": info["schemaVersion"],
                                                   # Contract 1 keys views by id@version; contract 3 lists the ids.
                                                   "views": list(info["views"]), "roles": roles(info["views"]),
                                                   "genres": info["genres"]}}
        if snapshot.reason:
            result["reason"] = snapshot.reason
        if state.update_reason:
            result["update"] = {"reason": state.update_reason}
        if environment:
            # instanceId tells this run of the host; OctantPath sends its whole record again when it changes.
            result.update(paired=True, instanceId=cache.instance, signals=cache.count(environment))
            if snapshot.status == "ready":
                # The hands come from OctantVector (OctantSpecs plans/presets.md §8); OctantCompass only relays them
                result["presetsRevision"] = (await asyncio.to_thread(state.engine.presets, snapshot.db))["revision"]
        return result

    @app.post("/v1/pair")
    def pair(body: PairRequest):
        token = pairing.pair(body.secret, body.name)
        if token is None:
            log.warning("pairing refused: the secret or code did not match")
            return error(401, "unauthorized", "the pairing secret is invalid")
        log.info("paired a new environment: %s", body.name)
        return {"token": token}

    @app.post("/v1/recommendations")
    async def recommendations(body: RecommendationRequest, request: Request):
        snapshot = state.snapshot
        if snapshot.status != "ready":
            return error(503, "not_ready", "the recommendation database is not ready")
        service = RecommendationService(state.engine, clock)
        try:
            conditions = await asyncio.to_thread(state.engine.expand_preset, snapshot.db, body.model_dump(exclude_unset=True))
            history = await asyncio.to_thread(cache.history, request.state.environment)
            result = await asyncio.to_thread(service.recommend, snapshot.db, history, conditions)
        except (RequestError, DBError) as exc:
            return engine_error(exc, body.preset)
        # Contract 1 shape (OctantSpecs contracts/examples/engine-runtime) to contract 3; order and items unchanged.
        return {"requestId": str(uuid4()), "dbVersion": result["dbVersion"], "candidates": result["items"],
                "aims": result["aims"], "replaced": result["replaced"],
                "ignoredWeights": result["ignoredWeights"]}

    @app.get("/v1/presets")
    def preset_list():
        snapshot = state.snapshot
        if snapshot.status != "ready":
            return error(503, "not_ready", "the recommendation database is not ready")
        return state.engine.presets(snapshot.db)

    @app.put("/v1/signals")
    def replace_signals(body: SignalsRequest, request: Request):
        # OctantPath's whole record, when it first meets this run of the host (or lost track of what it had sent).
        return cache.receive(request.state.environment, validate_events(body.events), replace=True)

    @app.post("/v1/signals")
    def signals(body: SignalsRequest, request: Request):
        return cache.receive(request.state.environment, validate_events(body.events))

    @app.post("/v1/profile")
    async def profile(body: ProfileRequest, request: Request):
        snapshot = state.snapshot
        if snapshot.status != "ready":
            return error(503, "not_ready", "the recommendation database is not ready")
        history = await asyncio.to_thread(cache.history, request.state.environment)
        service = RecommendationService(state.engine, clock)
        try:
            return await asyncio.to_thread(service.describe, snapshot.db, history, body.model_dump(exclude_unset=True))
        except (RequestError, DBError) as exc:
            return engine_error(exc)

    app.include_router(create_router(live, pairing, state, cache, recent, API_VERSION, use))
    return app
