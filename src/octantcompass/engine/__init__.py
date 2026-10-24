"""Opaque contract-1 port and DB state; no distribution files are inspected."""

import warnings
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from threading import Lock

from ..activity import log

DbHandle = Any


class DBError(Exception):
    pass


class EngineIncompatibleError(Exception):
    pass


class EngineUnavailableError(Exception):
    pass


class VersionError(DBError):
    pass


class RequestError(Exception):
    pass


class SeedNotInDBError(RequestError):
    pass


class SeedNotEmbeddedError(SeedNotInDBError):
    """The seed is listed in the DB but has no vector to compare (contract 1 (OctantCompass to OctantVector), OctantVector SeedNotEmbeddedError)."""


class UnknownPresetError(RequestError):
    """The request names a preset that is not in the current list (contract 1 presets, OctantSpecs plans/presets.md §8)."""


class SignatureWarning(UserWarning):
    """apply_update or import_archive used a DB version that is not signed by a known key: no manifest.sig, a malformed one, an unknown
    key or a signature that does not match (OctantSpecs contracts/distribution.md, 署名; OctantVector SignatureWarning).
    The signature names the author and does not stop the update."""


@contextmanager
def recorded(category):
    """Collect the text of every `category` warning raised in the block into the list it gives, instead of showing
    it; other warnings are shown as before when the block ends."""
    texts = []
    caught = []
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", category)
            yield texts
    finally:
        for warning in caught:
            if issubclass(warning.category, category):
                texts.append(str(warning.message))
            else:
                warnings.showwarning(warning.message, warning.category, warning.filename, warning.lineno)


@runtime_checkable
class VectorEngine(Protocol):
    def open_db(self, path: Path) -> DbHandle: ...
    def db_info(self, db: DbHandle) -> dict: ...
    def read_info(self, path: Path) -> dict: ...
    def recommend(self, db: DbHandle, history: list, request: dict, params: dict) -> dict: ...
    def describe(self, db: DbHandle, history: list, params: dict) -> dict: ...
    def presets(self, db: DbHandle) -> dict: ...
    def expand_preset(self, db: DbHandle, request: dict) -> dict: ...
    def plan_update(self, index: dict, local_version: str | None) -> list: ...
    def apply_update(self, files: list, base: Path | None, out: Path, trust: dict | None = None) -> Path: ...
    def import_archive(self, path: Path, out: Path, trust: dict | None = None) -> Path: ...


@dataclass(frozen=True)
class Snapshot:
    status: str = "loading"
    reason: str | None = None
    db: DbHandle = None
    info: dict | None = None
    path: Path | None = None  # the DB folder, for the window
    # The DB is let go to save memory (contract 6 (client to OctantCompass) 2.0): status stays "loading" and
    # info keeps what the DB said when it was last loaded, until a request from a paired environment loads it again.
    resting: bool = False


def summary(info):
    """What the window shows of a DB's info (db_info or read_info): its versions, build time, genre words and notice,
    the views' names, and each view's credits (as text: a DB from someone else is read with a warning only)."""
    views = info.get("views", {})
    return {key: info.get(key) for key in ("dbVersion", "schemaVersion", "builtAt", "genres", "notice")} | {
        "views": list(views), "credits": {view: v["credits"] for view, v in views.items() if v.get("credits")}}


class EngineState:
    def __init__(self, engine: VectorEngine):
        self.engine = engine
        self.snapshot = Snapshot()
        self.update_reason = None
        self.update_lock = Lock()
        self.waking = Lock()

    def rest(self, path: Path):
        """Hold no DB: at the start, and when no paired environment used it for a while. A DB switch or update that
        holds update_lock goes first; the caller tries again later. Returns whether the DB was let go."""
        if not self.update_lock.acquire(blocking=False):
            return False
        try:
            snapshot = self.snapshot
            if snapshot.resting or (snapshot.status == "loading" and snapshot.path is not None):
                return False
            # Requests already running keep their own snapshot; the DB is freed when the last of them ends.
            self.snapshot = Snapshot("loading", info=snapshot.info, path=snapshot.path or path, resting=True)
            return True
        finally:
            self.update_lock.release()

    def wake(self):
        """Load the resting DB again (blocking; run it in a thread). Only one load runs; other callers return."""
        if not self.snapshot.resting or not self.waking.acquire(blocking=False):
            return
        try:
            with self.update_lock:
                if self.snapshot.resting:
                    self.load(self.snapshot.path)
        finally:
            self.waking.release()

    def read_manifest(self, path: Path):
        """Know the DB at path by what its manifest says, without loading it: for a program that hands DBs to the
        host (dbupdate.LocalDatabase), which needs its version and folder only. info is None when it cannot be read."""
        try:
            info = self.engine.read_info(path)
        except (OSError, DBError):
            info = None
        self.snapshot = Snapshot("loading", info=info, path=Path(path), resting=True)

    def prepare(self, path: Path):
        db = self.engine.open_db(path)
        return Snapshot("ready", db=db, info=self.engine.db_info(db), path=path)

    def load(self, path: Path):
        try:
            db = self.engine.open_db(path)
            info = self.engine.db_info(db)
        except EngineIncompatibleError:
            self.snapshot = Snapshot("degraded", "engine_incompatible", path=path)
        except EngineUnavailableError:
            self.snapshot = Snapshot("degraded", "engine_unavailable", path=path)
        except VersionError:
            self.snapshot = Snapshot("degraded", "schema_unsupported", path=path)
        except FileNotFoundError:
            self.snapshot = Snapshot("degraded", "db_missing", path=path)
        except DBError:
            self.snapshot = Snapshot("degraded", "db_invalid", path=path)
        except Exception as exc:
            # Anything else (out of memory, an error the engine did not map) must not leave the host "loading"
            # for good: say degraded, and keep the cause in the log for the window.
            log.error("DB could not be loaded (%s: %s)", type(exc).__name__, exc)
            self.snapshot = Snapshot("degraded", "db_load_failed", path=path)
        else:
            self.snapshot = Snapshot("ready", db=db, info=info, path=path)
