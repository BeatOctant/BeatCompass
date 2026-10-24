"""The management API at /manage/: what the OctantCompass window (window.py) asks the running host for. State, hands,
paired environments and pairing codes, the recent log, the settings in use, restart and stop. The window changes
settings.json itself and switches, imports and updates the DB itself, handing the new DB to the host as a request
(manager.py, dbupdate.LocalDatabase), so they work while the host is stopped.

It is not part of contract 3 (OctantPath to OctantCompass). It answers loopback clients only, even when the host listens
beyond 127.0.0.1. Its client is a program on this PC (the window's Python), never a web page, so a page is kept out
by the Host header (it must name this PC, which stops DNS rebinding) and the Origin header (a browser sends one from
a page; nothing here is called from one). Loopback is not the same as "this PC's user", though: `adb reverse` (the
Quest's USB connection) makes any app on the Quest reach the host from 127.0.0.1. So each run also makes a key that
only the files of this PC's user hold (connection.json, readable by its owner only), and every call must send it
(2026-10-23; the key of 2026-10-13's "no key" decision is not needed against pages, but is against other devices).
"""

import asyncio
import hmac
import ipaddress
import os
import secrets

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StrictStr

from . import __version__, build
from .activity import log
from .config import EDITABLE, RESTART_KEYS
from .engine import summary


class ApplyRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    key: StrictStr  # a setting the window just saved in settings.json


def loopback(request):
    try:
        return request.client is not None and ipaddress.ip_address(request.client.host).is_loopback
    except ValueError:
        return False


LOCAL_NAMES = {"127.0.0.1", "localhost", "::1"}


def split_host(value):
    """("127.0.0.1", "23287") from "127.0.0.1:23287", ("::1", ...) from "[::1]:...", or None."""
    value = value.strip().lower()
    if value.startswith("["):
        name, bracket, rest = value[1:].partition("]")
        if not bracket or (rest and not rest.startswith(":")):
            return None
        return name, rest[1:]
    name, _, port = value.partition(":")
    return name, port


def from_this_pc(request):
    """The request names this PC in Host and comes from no web page (no Origin)."""
    host = split_host(request.headers.get("host", ""))
    return host is not None and host[0] in LOCAL_NAMES and "origin" not in request.headers


KEY_HEADER = "X-OctantCompass-Manage-Key"


def new_key():
    """A key for one run of the host, published in connection.json."""
    return secrets.token_urlsafe(32)


def has_key(request):
    key = getattr(request.app.state, "manage_key", None)
    given = request.headers.get(KEY_HEADER, "")
    return bool(key) and hmac.compare_digest(given.encode(), key.encode())


def refuse(status, code):
    return JSONResponse(status_code=status, content={"error": {"code": code}})


def create_router(live, pairing, state, cache, recent, api_version, use=lambda: None):
    """live: the LiveConfig; each request reads the settings as they are now. use: call when the window needs the DB
    loaded (a resting DB loads again)."""
    router = APIRouter(prefix="/manage")

    def allowed(request):
        if not loopback(request):
            return refuse(403, "loopback_only")
        if not from_this_pc(request):
            return refuse(403, "other_site")
        if not has_key(request):
            return refuse(403, "no_key")
        return None

    @router.get("/status")
    def status(request: Request):
        if refused := allowed(request):
            return refused
        config = live.current
        snapshot = state.snapshot
        info = snapshot.info
        server = getattr(request.app.state, "server", None) or {}
        # The window says "resting" for a DB let go to save memory; contract 3 (OctantPath to OctantCompass) says "loading".
        return {"hostVersion": __version__, "hostCommit": build.commit(), "apiVersion": api_version,
                "status": "resting" if snapshot.resting else snapshot.status,
                "reason": snapshot.reason, "update": state.update_reason,
                # The DB as it was loaded (None before it first loads); the window shows the folder's own info
                "db": None if info is None else summary(info),
                "dbPath": None if snapshot.path is None else str(snapshot.path),
                "dataDir": config.places.shown(), "listen": server.get("listen"), "startedAt": server.get("startedAt"),
                "canStop": callable(getattr(request.app.state, "stop", None)),
                "canRestart": callable(getattr(request.app.state, "restart", None)), "pid": os.getpid(),
                # The last restart from the window: its number, whether the new listener came up, and why not.
                "restart": getattr(request.app.state, "restart_result", None)}

    @router.get("/settings")
    def settings(request: Request):
        """The settings the window changes, as this run uses them (settings.json and command-line values). They
        differ from settings.json for RESTART_KEYS until a restart, and for values given on the command line."""
        if refused := allowed(request):
            return refused
        config = live.current
        return {"settings": {key: getattr(config, key) for key in EDITABLE}}

    @router.post("/settings/apply")
    def apply_setting(body: ApplyRequest, request: Request):
        """Use a setting the window just saved in settings.json; RESTART_KEYS wait for a restart."""
        if refused := allowed(request):
            return refused
        if body.key not in EDITABLE:
            return refuse(400, "invalid_setting")
        if body.key in RESTART_KEYS:
            log.info("setting %s changed in the window; a restart applies it", body.key)
            return {"applied": False}
        try:
            recent.resize(live.reload([body.key]).log_lines)
        except (OSError, ValueError, TypeError):
            return refuse(400, "invalid_setting")
        log.info("setting %s changed in the window and applied", body.key)
        return {"applied": True}

    @router.get("/presets")
    async def presets(request: Request):
        if refused := allowed(request):
            return refused
        use()
        snapshot = state.snapshot
        if snapshot.status != "ready":
            return refuse(503, "not_ready")
        # Relayed as OctantVector returns them; the window only shows the list.
        return await asyncio.to_thread(state.engine.presets, snapshot.db)

    @router.get("/environments")
    async def environments(request: Request):
        if refused := allowed(request):
            return refused
        rows = await asyncio.to_thread(pairing.environments)
        for row in rows:
            row["signals"] = cache.count(row["id"])
        return {"environments": rows}

    @router.post("/environments/{environment}/revoke")
    async def revoke(environment: str, request: Request):
        if refused := allowed(request):
            return refused
        if not await asyncio.to_thread(pairing.revoke, environment):
            return refuse(404, "not_found")
        cache.delete(environment)
        log.info("revoked environment %s", environment)
        return {"revoked": environment}

    @router.post("/pair-code")
    async def pair_code(request: Request):
        if refused := allowed(request):
            return refused
        config = live.current
        code = await asyncio.to_thread(pairing.issue_code, digits=config.pair_code_digits,
                                       seconds=config.pair_code_seconds, attempts=config.pair_code_attempts)
        log.info("issued a pairing code")
        return {"code": code, "seconds": config.pair_code_seconds}

    @router.get("/log")
    def recent_log(request: Request):
        return allowed(request) or {"lines": recent.read()}

    @router.post("/stop")
    def stop(request: Request):
        if refused := allowed(request):
            return refused
        stop = getattr(request.app.state, "stop", None)
        if not callable(stop):
            return refuse(409, "not_stoppable")
        log.info("stop requested from the window")
        stop()
        return {"stopping": True}

    @router.post("/restart")
    def restart(request: Request):
        if refused := allowed(request):
            return refused
        restart = getattr(request.app.state, "restart", None)
        if not callable(restart):
            return refuse(409, "not_restartable")
        log.info("restart requested from the window")
        restart()
        return {"restarting": True}

    return router
