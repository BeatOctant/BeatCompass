"""Local DB selection, DB archives handed over, and updates from the contract 5 (OctantHarvest to OctantCompass) index
(OctantSpecs §6).

The DB a host uses is files in its data folder: the folder it adopted last (db-selection.json, written by the host)
and the folder it was asked to use (db-request.json). The window and the `db` commands switch, import and update
without the host (LocalDatabase) and hand the new folder over as a request; a running host adopts it within
poll_seconds (DatabaseUpdater.watch), a stopped one at its next start. Updates from the index run only when an index
URL is configured and someone asks; there is no automatic schedule yet. Signatures are checked by apply_update with
the trust from the settings (OctantSpecs contracts/distribution.md, 署名). DB contents are opened only through
VectorEngine.
"""

import asyncio
import json
import shutil
import tempfile
from pathlib import Path, PureWindowsPath
from threading import Lock
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4

from . import __version__
from .activity import log
from .engine import EngineState, SignatureWarning, VersionError, recorded
from .permissions import write_private

# Sent with every request for the index and its parts: which program asks, and where to reach the project (as
# OctantHarvest names itself to the services it uses).
USER_AGENT = f"OctantCompass/{__version__} ( https://bsky.app/profile/beatoctant.bsky.social )"


class DatabaseSelection:
    def __init__(self, state):
        """state: Places.state."""
        self.path = Path(state) / "db-selection.json"

    def read(self):
        try:
            value = json.loads(self.path.read_text())
        except FileNotFoundError:
            return None
        if not isinstance(value, dict) or not isinstance(value.get("path"), str) or not value["path"]:
            raise ValueError("invalid DB selection")
        if not isinstance(value.get("revision"), str) or not value["revision"]:
            raise ValueError("invalid DB selection revision")
        return value

    def publish(self, path, revision=None):
        write_private(self.path, json.dumps({"path": str(Path(path).resolve()), "revision": revision or str(uuid4())}))


class DatabaseRequests(DatabaseSelection):
    def __init__(self, state):
        self.path = Path(state) / "db-request.json"


def db_in_use(config, selection, given=None):
    """The DB folder a host uses when it starts: the one given on the command line (--db), else the one it adopted
    last, else the setting (config.db). A request it has not adopted yet comes after the start."""
    if given is not None:
        return Path(given)
    selected = selection.read()
    return Path(selected["path"]) if selected is not None else config.db


class NotHttps(ValueError):
    pass


def https_url(url):
    """url when it is https; the index and its parts are fetched over https only (OctantSpecs contracts/distribution.md,
    取得の決まり. 2026-10-16): the index is not signed, and over anything else it and the parts it names could be
    swapped on the way."""
    if not isinstance(url, str) or urlsplit(url).scheme.lower() != "https":
        raise NotHttps(f"not an https URL: {url!r}")
    return url


def part_url(index_url, part):
    # Contract 5 (OctantHarvest to OctantCompass) index: a part without a URL lies next to the index under its name.
    return part.get("url") or urljoin(index_url, part["name"])


def part_size(part):
    """The size the index gives for a part: the most fetched for it. The index is not signed, and OctantVector checks
    the parts only once they are fetched, so a part that sends more is refused while it arrives."""
    size = part.get("size")
    if type(size) is not int or size < 0:
        raise ValueError(f"the index gives no size for the part {part.get('name')!r}")
    return size


# What an update needs besides the fetched parts: each entry is joined into one tar and extracted (both about the
# parts' size; the tar is plain, OctantVector update.py), and applying a diff writes the new full next to them.
ROOM_FACTOR = 3


def check_room(folder, size):
    """Refuse an update whose work (ROOM_FACTOR x size, the total the index gives for the parts) does not fit in the
    free space of folder. The index is not signed, so one that gives more than the disk holds is refused before
    anything is fetched, instead of filling the disk while it arrives; the factor covers the joined tar and the
    extracted files beside the parts (security-audit-2026-10-21.md, 6)."""
    free = shutil.disk_usage(folder).free
    if size * ROOM_FACTOR > free:
        raise ValueError(f"the index gives {size} bytes of parts, which need about {size * ROOM_FACTOR} bytes "
                         f"to fetch and apply, more than the {free} bytes free for them")


def fetch(url, out, size, progress=None, *, timeout=60):
    out.parent.mkdir(parents=True, exist_ok=True)
    received = 0
    request = Request(https_url(url), headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=timeout) as response, open(out, "wb") as file:
        https_url(response.geturl())  # a redirect may not leave https
        for chunk in iter(lambda: response.read(1 << 20), b""):
            received += len(chunk)
            if received > size:
                raise ValueError(f"the part is larger than the {size} bytes the index gives")
            file.write(chunk)
            if progress is not None:
                progress(len(chunk))


def read_index(url, limit, *, timeout=60):
    """The index at url; limit: the most bytes read (Config.index_max_bytes)."""
    with urlopen(Request(https_url(url), headers={"User-Agent": USER_AGENT}), timeout=timeout) as response:
        https_url(response.geturl())  # a redirect may not leave https
        text = response.read(limit + 1)
    if len(text) > limit:
        raise ValueError(f"the index is larger than {limit} bytes (index_max_megabytes)")
    index = json.loads(text)
    if not isinstance(index, dict):
        raise ValueError("the index is not a JSON object")
    return index


def folder_name(version):
    """The index dbVersion as the name of the new DB folder. The index is not signed, so a name that could leave
    `dbs/` (a separator, "..", an absolute path or a drive) is refused before any file is touched. So is a name
    too long for a folder (with the suffix a taken name gets): resolve() does not fail on it, exists() does."""
    if (not isinstance(version, str) or version in ("", ".", "..") or "/" in version or "\\" in version
            or "\0" in version or len(version.encode()) > 100 or PureWindowsPath(version).drive
            or Path(version).is_absolute()):
        raise ValueError(f"the index names an unsafe DB folder: {version!r}")
    return version


class UpdateRunning(RuntimeError):
    """Another update from the index or import of an archive is still running."""


class UpdateCancelled(RuntimeError):
    """The update was called off while its parts were being fetched (DatabaseUpdater.cancel)."""


def archive_folder(path):
    """The DB folder name for an archive handed over as <dbVersion>.full.tar (OctantHarvest `handoff`): its file
    name without the suffixes, refused as folder_name refuses an index dbVersion. OctantVector checks what is inside."""
    name = Path(path).name
    for suffix in (".tar", ".full"):
        name = name.removesuffix(suffix)
    return folder_name(name)


class DatabaseUpdater:
    def __init__(self, state):
        self.state = state
        self.progress = None  # {"done": bytes, "total": bytes} while parts are being fetched
        self.running = Lock()
        self.cancelled = False

    def cancel(self):
        """Call off the update while its parts are being fetched; it stops before the next MB and keeps the
        current DB. Applying the fetched parts is not called off. It stays called off: an updater runs one update
        (LocalDatabase makes one for each)."""
        self.cancelled = True

    async def watch(self, requests, initial, poll_seconds, accepted=None):
        """poll_seconds: a number, or a function returning it (the setting can change while the host runs)."""
        previous = initial
        while True:
            await asyncio.sleep(poll_seconds() if callable(poll_seconds) else poll_seconds)
            try:
                selected = await asyncio.to_thread(requests.read)
                if selected is not None and selected != previous:
                    previous = selected
                    operation = asyncio.create_task(asyncio.to_thread(
                        self.select_local, Path(selected["path"]), accepted, revision=selected["revision"]))
                    try:
                        await asyncio.shield(operation)
                    except asyncio.CancelledError:
                        # Finish validation/publication before host shutdown.
                        try:
                            await operation
                        except Exception:
                            self.state.update_reason = "db_update_failed"
                        raise
            except Exception:
                self.state.update_reason = "db_update_failed"

    def select_local(self, path, selection=None, *, revision=None):
        # Serialize updates; readers retain their immutable snapshot throughout
        # validation and finish requests against the DB they started with.
        with self.state.update_lock:
            try:
                prepared = self.state.prepare(Path(path))
                if selection is not None:
                    selection.publish(path, revision)
            except Exception:
                self.state.update_reason = "db_update_failed"
                log.warning("DB switch failed: %s", path)
                raise
            self.state.snapshot = prepared
            self.state.update_reason = None
        log.info("DB switched to %s (%s)", prepared.info["dbVersion"], path)
        return prepared.info

    def apply(self, files, base, out, trust):
        """Contract-1 apply_update from the DB folder `base` (None for a full) into the new folder `out`; `trust`
        from Config.update_trust. The current DB is not switched here: select_local does that after it opens.
        -> what OctantVector warned about the versions' signatures; they are used all the same, so each warning is
        also logged for the window."""
        try:
            with recorded(SignatureWarning) as signatures:
                self.state.engine.apply_update(files, base, Path(out), trust)
        except Exception:
            self.state.update_reason = "db_update_failed"
            raise
        for text in signatures:
            log.warning("DB update: signature warning, the version is used anyway: %s", text)
        return signatures

    def check(self, index_url, *, index_limit):
        """What an update from the index would fetch: {"dbVersion": newest, "entries": [...], "size": bytes}, or
        an empty entries list when the local DB is already the newest this OctantVector reads. index_limit: the most
        bytes the index may have (Config.index_max_bytes)."""
        url = index_url
        try:
            index = read_index(url, index_limit)
        except Exception:
            self.state.update_reason = "db_update_failed"
            raise
        entries = self.plan(index)
        try:
            for entry in entries:
                folder_name(entry["dbVersion"])
                for part in entry["parts"]:
                    part_size(part)
                    https_url(part_url(url, part))
        except ValueError:
            self.state.update_reason = "db_update_failed"
            raise
        snapshot = self.state.snapshot
        return {"current": snapshot.info["dbVersion"] if snapshot.info else None,
                "dbVersion": entries[-1]["dbVersion"] if entries else None,
                "entries": [{"dbVersion": e["dbVersion"], "kind": e["kind"], "size": e.get("size")} for e in entries],
                "size": sum(part.get("size") or 0 for e in entries for part in e["parts"]),
                "url": url, "plan": entries}

    def update(self, index_url, data, trust, selection=None, *, index_limit):
        """Fetch what the index plans, apply it into a new folder under `data/dbs` (data: Places.data), and switch to it once it
        opens. The current DB keeps serving until then and stays when anything fails. -> (the new info, or None
        when already up to date; the signature warnings from apply)."""
        if not self.running.acquire(blocking=False):
            raise UpdateRunning("an update from the index is already running")
        try:
            return self._update(index_url, data, trust, selection, index_limit)
        finally:
            self.running.release()

    def import_file(self, path, data, trust, selection=None):
        """Check and extract the DB archive at `path` (one full version as a tar, handed over without an index) into
        a new folder under `data/dbs`, and switch to it once it opens. The current DB keeps serving until then and
        stays when anything fails. -> (the new info, the signature warnings)."""
        if not self.running.acquire(blocking=False):
            raise UpdateRunning("an update or import is already running")
        try:
            try:
                path = Path(path)
                size, free = path.stat().st_size, shutil.disk_usage(self._folder(data, "dbs")).free
                if size * 2 > free:  # the tar holds the files uncompressed, and they are extracted next to dbs/
                    raise ValueError(f"the archive is {size} bytes and extracts beside dbs/, "
                                     f"more than the {free} bytes free for it")
                out = self._new_folder(data, archive_folder(path))
                with recorded(SignatureWarning) as signatures:
                    self.state.engine.import_archive(path, out, trust)
            except Exception as exc:
                self.state.update_reason = "db_update_failed"
                log.warning("DB import from an archive failed (%s)", exc)
                raise
            for text in signatures:
                log.warning("DB import: signature warning, the version is used anyway: %s", text)
            return self.select_local(out, selection), signatures
        finally:
            self.running.release()

    def _new_folder(self, data, name):
        """data/dbs/<name>, with a suffix when that is taken (or a write into it was left unfinished: OctantVector
        writes <name>.partial first and refuses to start when one is there); refused when it would resolve outside
        dbs/ (a link)."""
        dbs = self._folder(data, "dbs")
        out = dbs / name
        if out.resolve().parent != dbs.resolve():
            raise ValueError(f"unsafe DB folder: {name!r}")
        if out.exists() or out.with_name(f"{out.name}.partial").exists():
            out = out.with_name(f"{out.name}-{uuid4().hex[:8]}")
        return out

    def _update(self, index_url, data, trust, selection, index_limit):
        checked = self.check(index_url, index_limit=index_limit)
        entries = checked["plan"]
        if not entries:
            return None, []
        snapshot = self.state.snapshot
        base = None if entries[0]["kind"] == "full" else snapshot.path
        try:
            # check() refused bad names; this also catches a link in dbs/
            out = self._new_folder(data, folder_name(entries[-1]["dbVersion"]))
        except Exception:
            self.state.update_reason = "db_update_failed"
            raise
        downloads = Path(tempfile.mkdtemp(prefix="download-", dir=self._folder(data, "downloads")))
        self.progress = {"done": 0, "total": checked["size"]}
        try:
            check_room(downloads, checked["size"])
            files = []
            for n, entry in enumerate(entries):
                parts = []
                for m, part in enumerate(entry["parts"]):
                    path = downloads / f"{n}-{m}"
                    fetch(part_url(checked["url"], part), path, part["size"], self._advance)
                    parts.append(path)
                files.append((entry, parts))
            self.progress = None  # applying is not called off
            signatures = self.apply(files, base, out, trust)
        except Exception as exc:
            self.state.update_reason = "db_update_failed"
            log.warning("DB update from the index failed (%s)", exc)
            raise
        finally:
            self.progress = None
            shutil.rmtree(downloads, ignore_errors=True)
        return self.select_local(out, selection), signatures

    def _advance(self, size):
        if self.cancelled:
            raise UpdateCancelled("the update was called off")
        if self.progress is not None:
            self.progress = {**self.progress, "done": self.progress["done"] + size}

    @staticmethod
    def _folder(data, name):
        folder = Path(data) / name
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        return folder

    def plan(self, index):
        with self.state.update_lock:
            snapshot = self.state.snapshot
            version = snapshot.info["dbVersion"] if snapshot.info else None
            try:
                files = self.state.engine.plan_update(index, version)
            except VersionError:
                self.state.update_reason = "index_unsupported"
                raise
            except Exception:
                self.state.update_reason = "db_update_failed"
                raise
            self.state.update_reason = None
            return files


class LocalDatabase:
    """The DB of a data folder as the window and the `db` commands see and change it, whether the host runs or not:
    the folder the host uses and the folder it was asked to use next, from the files; what a DB folder says, from its
    manifest (read_info: nothing is loaded); and switching, importing and updating, each checked as the host will
    open it and handed over as a request. One at a time; each runs on a DatabaseUpdater of its own, so nothing it
    opened stays in memory after it."""

    def __init__(self, places, engine, given=None):
        """engine: the VectorEngine the host uses. given: the DB folder given on the command line (--db)."""
        self.places = places
        self.engine = engine
        self.given = given
        self.selection = DatabaseSelection(places.state)
        self.requests = DatabaseRequests(places.state)
        self.busy = Lock()
        self.work = None  # (what, its DatabaseUpdater) while a switch, import, check or update runs

    def in_use(self, config):
        return db_in_use(config, self.selection, self.given)

    def pending(self):
        """The DB folder asked for that the host has not adopted (yet, or because it could not open it), or None."""
        request = self.requests.read()
        return Path(request["path"]) if request is not None and request != self.selection.read() else None

    def info(self, path):
        """What the DB folder's manifest says (VectorEngine.read_info)."""
        return self.engine.read_info(Path(path))

    def running(self):
        """{"what": "switch", "import", "check" or "update", "progress": {"done", "total"} while an update fetches
        its parts}, or None."""
        work = self.work
        return None if work is None else {"what": work[0], "progress": work[1].progress}

    def cancel(self):
        """Call off an update while its parts are fetched. -> whether one was running."""
        work = self.work
        if work is None or work[0] != "update":
            return False
        work[1].cancel()
        return True

    def switch(self, path):
        """Open the DB folder as the host will, and ask the host to use it. -> {"dbVersion", "path"}."""
        path = Path(path).resolve()
        info = self._run("switch", lambda updater: updater.select_local(path, self.requests))
        return {"dbVersion": info["dbVersion"], "path": str(path)}

    def import_file(self, path, config):
        """Check and extract a DB archive (<dbVersion>.full.tar) into the data folder and ask the host to use it.
        -> {"dbVersion", "signatureWarnings"}."""
        info, signatures = self._run("import", lambda updater: updater.import_file(
            Path(path).resolve(), config.places.data, config.update_trust(), self.requests))
        return {"dbVersion": info["dbVersion"], "signatureWarnings": signatures}

    def check(self, index, config, folder):
        """What an update from the index would fetch for the DB folder in use (DatabaseUpdater.check, without its
        plan)."""
        def check(updater):
            updater.state.read_manifest(folder)
            checked = updater.check(index, index_limit=config.index_max_bytes)
            return {key: value for key, value in checked.items() if key != "plan"}
        return self._run("check", check)

    def update(self, index, config, folder):
        """Fetch what the index plans for the DB folder in use, apply it into a new folder and ask the host to use it.
        -> {"updated": False when already up to date, "dbVersion": the new one or the one in use,
        "signatureWarnings"}."""
        def update(updater):
            updater.state.read_manifest(folder)
            current = updater.state.snapshot.info
            info, signatures = updater.update(index, config.places.data, config.update_trust(), self.requests,
                                              index_limit=config.index_max_bytes)
            return {"updated": info is not None, "dbVersion": (info or current or {}).get("dbVersion"),
                    "signatureWarnings": signatures}
        return self._run("update", update)

    def _run(self, what, work):
        if not self.busy.acquire(blocking=False):
            raise UpdateRunning("another switch, import or update is running")
        updater = DatabaseUpdater(EngineState(self.engine))
        self.work = (what, updater)
        try:
            return work(updater)
        finally:
            self.work = None
            self.busy.release()
