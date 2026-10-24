"""What the OctantCompass window does besides showing the running host (window.py shows it, without a GUI here).

The window owns three things, so that they work while the host is stopped: the settings, which are settings.json
(config.py); starting, stopping and restarting the host; and the DB, which is files in the data folder: it shows
what the DB in use says and switches, imports and updates it as the `db` commands do (dbupdate.LocalDatabase),
handing the new DB to the host, which adopts it at once when it runs and at its next start when not. A setting that
keeps the host from starting (a port in use, an address this PC does not have) is changed here and the host started
again, with the reason it stopped shown. Everything else the window shows (state, hands, paired environments,
pairing codes, the log) needs the running host and comes from its management API (manage.py) as it is.
"""

import json
import subprocess
import time
from pathlib import Path
from threading import Lock
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from . import __version__, build, shortcut
from .config import DEFAULTS, RESTART_KEYS, SETTING_GROUPS, Config, parse_setting, read_settings, save_setting
from .dbupdate import LocalDatabase, NotHttps, UpdateCancelled, UpdateRunning
from .engine import DBError, EngineIncompatibleError, EngineUnavailableError, VersionError, summary
from .engine.octantvector import OctantVectorEngine
from .frozen import system_environment
from .lifecycle import running_address, running_manage_key
from .manage import KEY_HEADER

# How long a call to the running host may take: the management API answers from memory.
CALL_SECONDS = 5
# The lines of the start's output shown when the host stopped before it answered.
PROBLEM_LINES = 5


class Refused(Exception):
    """The window's request is refused: code is the error code the window shows a text for."""

    def __init__(self, code, status=400):
        super().__init__(code)
        self.code = code
        self.status = status


class Manager:
    def __init__(self, places, serve_arguments=(), engine=None, db=None):
        """places: the Places of this OctantCompass. serve_arguments: added to `serve` when the window starts the host
        (the host options given to `gui`). engine: the VectorEngine the host uses (OctantVector unless --fake-engine),
        for the DB section. db: the DB folder given to `gui` (--db), which a start from here uses."""
        self.places = places
        self.serve_arguments = list(serve_arguments)
        self.opener = build_opener(ProxyHandler({}))  # loopback calls never go through a configured proxy
        self.starting = Lock()
        self.process = None  # the host this window started, while it may still be running
        self.last_start = None  # {"ok": bool, "problem": text or None}: the last start from this window
        self.local = LocalDatabase(places, engine if engine is not None else OctantVectorEngine(), db)
        self.shown = None  # (folder, manifest's mtime, what the DB section shows of it): read again when it changes

    # ---- the running host ----

    def host(self, path, body=None):
        """Call the running host's management API (path under /manage/). -> (HTTP status, JSON body); raises
        Refused("not_running", 503) when no host answers."""
        if not path.startswith("/manage/"):
            raise Refused("not_found", 404)
        address = running_address(self.places.runtime)
        if address is None:
            raise Refused("not_running", 503)
        host, port = address
        name = f"[{host}]" if ":" in host else host
        headers = {KEY_HEADER: running_manage_key(self.places.runtime) or ""}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(f"http://{name}:{port}{path}", data=None if body is None else json.dumps(body).encode(),
                          headers=headers)
        try:
            with self.opener.open(request, timeout=CALL_SECONDS) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            with error:
                try:
                    return error.code, json.load(error)
                except ValueError:
                    return error.code, {"error": {"code": "host_error"}}
        except (OSError, ValueError):
            raise Refused("not_running", 503)

    def status(self):
        """{"running", "starting", "start": the last start from this window, "host": the host's status or None,
        "db": the DB section (database), "window": this OctantCompass's version and data folder}."""
        if not self.starting.locked() and self.process is not None and self.process.poll() is not None:
            self.process = None  # it ended (stopped, or failed after the start): reaped by poll()
        host = self._host_status()
        return {"running": host is not None, "starting": self.starting.locked(), "start": self.last_start,
                "host": host, "db": self.database(host),
                "window": {"version": __version__, "commit": build.commit(), "dataDir": self.places.shown()}}

    def _host_status(self):
        """The running host's status (/manage/status), or None."""
        try:
            code, host = self.host("/manage/status")
        except Refused:
            return None
        return host if code == 200 else None

    # ---- the DB ----

    def database(self, host=None):
        """The DB section, while the host runs or not: the folder in use (the running host's, else the one a start
        uses), what its manifest says ("info"; "problem" when it cannot be read: a reason code as the host's), the
        folder asked for that the host has not adopted yet ("pending"), the DB work running here, and the index URL
        an update uses when none is written."""
        try:
            config = Config.load(self.places)
        except (OSError, ValueError, TypeError):
            config = None
        try:
            path = self._in_use(host, config)
            pending = self.local.pending()
        except (OSError, ValueError):
            path = pending = None  # db-selection.json or db-request.json is unreadable; the host says why it stops
        info, problem = self._read(path) if path is not None else (None, None)
        return {"path": None if path is None else str(path), "info": info, "problem": problem,
                "pending": None if pending is None else str(pending), "work": self.local.running(),
                "indexUrl": None if config is None else config.index_url}

    def _in_use(self, host, config):
        """The DB folder in use, which the DB section shows and an update plans from: the running host's (host: its
        status, or None), else the one a start from here uses (None when the settings cannot be read)."""
        if host is not None and host.get("dbPath"):
            return Path(host["dbPath"])
        return None if config is None else self.local.in_use(config)

    def _read(self, path):
        """(summary of the DB folder's info, None) or (None, reason code); kept until its manifest changes."""
        try:
            changed = (path / "manifest.json").stat().st_mtime_ns
        except OSError:
            changed = None
        if self.shown is not None and self.shown[:2] == (path, changed) and changed is not None:
            return self.shown[2]
        try:
            shown = summary(self.local.info(path)), None
        except FileNotFoundError:
            shown = None, "db_missing"
        except VersionError:
            shown = None, "schema_unsupported"
        except EngineUnavailableError:
            shown = None, "engine_unavailable"
        except EngineIncompatibleError:
            shown = None, "engine_incompatible"
        except (OSError, DBError):
            shown = None, "db_invalid"
        self.shown = path, changed, shown
        return shown

    def busy(self):
        """A switch, import or update runs here (the window stays open until it ends; a check stops nothing)."""
        work = self.local.running()
        return work is not None and work["what"] != "check"

    def switch_db(self, path):
        """Check the DB folder by opening it, as the host will, and hand it to the host -> {"dbVersion", "path"}."""
        try:
            return self.local.switch(absolute(path))
        except UpdateRunning:
            raise Refused("update_running", 409)
        except Refused:
            raise
        except Exception:
            raise Refused("db_switch_failed")

    def import_db(self, path):
        """Check and extract a DB archive handed over without an index (OctantHarvest `handoff`) into the data folder,
        and hand it to the host -> {"dbVersion", "signatureWarnings"}."""
        config = self._config()
        try:
            return self.local.import_file(absolute(path), config)
        except UpdateRunning:
            raise Refused("update_running", 409)
        except Refused:
            raise
        except Exception:
            raise Refused("db_import_failed")

    def check_update(self, index=None):
        """What an update from the index (the setting's index_url when none is given) would fetch."""
        config = self._config()
        return self._from_index(lambda url, folder: self.local.check(url, config, folder), index, config,
                                "index_unreadable")

    def update_db(self, index=None):
        """Fetch what the index plans, apply it and hand the new DB to the host -> {"updated", "dbVersion",
        "signatureWarnings"}."""
        config = self._config()
        return self._from_index(lambda url, folder: self.local.update(url, config, folder), index, config,
                                "db_update_failed")

    def cancel_update(self):
        """Call off the update while its parts are fetched -> {"cancelled": whether one was running}."""
        return {"cancelled": self.local.cancel()}

    def _from_index(self, work, index, config, failed):
        """work(index URL, the DB folder in use: the one the DB section shows)."""
        url = (index or "").strip() or config.index_url
        if not url:
            raise Refused("no_index")
        try:
            return work(url, self._in_use(self._host_status(), config))
        except UpdateRunning:
            raise Refused("update_running", 409)
        except UpdateCancelled:
            raise Refused("update_cancelled")
        except VersionError:
            raise Refused("index_unsupported")
        except NotHttps:
            raise Refused("index_not_https")
        except Exception:
            raise Refused(failed)

    def _config(self):
        try:
            return Config.load(self.places)
        except (OSError, ValueError, TypeError):
            raise Refused("settings_unreadable")

    # ---- starting the host ----

    def start(self):
        """Start the host (`serve`) on its own, so it keeps running when the window closes, and wait until it
        answers or stops. -> {"ok": True} or {"ok": False, "problem": what it said when it stopped}."""
        if not self.starting.acquire(blocking=False):
            raise Refused("starting", 409)
        try:
            self.last_start = self._start()
            return self.last_start
        finally:
            self.starting.release()

    def _start(self):
        if self._answers():
            return {"ok": True, "problem": None}
        try:
            config = Config.load(self.places)
        except (OSError, ValueError, TypeError) as exc:
            return {"ok": False, "problem": f"settings.json: {exc}"}
        self.places.make()
        output = self.places.runtime / "start.log"
        with open(output, "wb") as out:
            self.process = subprocess.Popen(shortcut.command(self.places, "serve") + self.serve_arguments,
                                            stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                            env=system_environment(), **shortcut.detached())
        # The host answers once it listens; the DB loads after that (or when a paired environment asks).
        deadline = time.monotonic() + config.startup_timeout + 5
        while time.monotonic() < deadline:
            if self._answers():
                return {"ok": True, "problem": None}
            if self.process.poll() is not None:
                self.process = None
                return {"ok": False, "problem": last_lines(output) or "OctantCompass stopped without a reason"}
            time.sleep(config.poll_seconds)
        return {"ok": False, "problem": f"OctantCompass did not answer within {config.startup_timeout + 5:g} seconds"}

    def _answers(self):
        try:
            return self.host("/manage/status")[0] == 200
        except Refused:
            return False

    # ---- settings ----

    def settings(self):
        """Each setting the window changes, in its group and order: as settings.json says, as the running host uses
        it (None while stopped), and its default."""
        try:
            saved = Config.load(self.places)
        except (OSError, ValueError, TypeError):
            saved = None
        try:
            code, body = self.host("/manage/settings")
            running = body["settings"] if code == 200 else None
        except Refused:
            running = None
        rows = [{"key": key, "group": group,
                 "value": None if running is None else shown(key, running[key]),
                 "saved": None if saved is None else shown(key, getattr(saved, key)),
                 "edit": "" if saved is None else editable(key, getattr(saved, key)),
                 "default": shown(key, DEFAULTS[key])} for group, keys in SETTING_GROUPS for key in keys]
        return {"path": str(self.places.settings), "readable": saved is not None, "running": running is not None,
                "settings": rows}

    def save_setting(self, key, text):
        """Write one setting into settings.json; the running host uses it at once, except RESTART_KEYS (a restart)
        -> {"saved": key, "applied": whether the running host uses it now}."""
        try:
            value = parse_setting(key, text)
            before = read_settings(self.places).get(key)
            save_setting(self.places, key, value)
        except (OSError, ValueError, TypeError):
            raise Refused("invalid_setting")
        if key == "start_at_login":
            # The OS entry follows the saved value; if it cannot be written, the setting goes back to what it was.
            try:
                shortcut.set_start_at_login(self.places, bool(value))
            except (OSError, RuntimeError, KeyError):
                save_setting(self.places, key, before)
                raise Refused("start_at_login_failed", 500)
        applied = False
        if key not in RESTART_KEYS:
            try:
                code, body = self.host("/manage/settings/apply", {"key": key})
                applied = code == 200 and body.get("applied") is True
            except Refused:
                pass  # stopped: the next start reads it
        return {"saved": key, "applied": applied}


def absolute(path):
    """A path the user gave (~ for the home folder); refused unless absolute."""
    path = Path(path).expanduser()
    if not path.is_absolute():
        raise Refused("path_not_absolute")
    return path


def shown(key, value):
    """A setting as the window shows it: key ids only for trusted_keys (the public keys stay in settings.json)."""
    return sorted(value) if key == "trusted_keys" else str(value) if isinstance(value, Path) else value


def editable(key, value):
    """The text the window's editor starts from (what parse_setting reads back)."""
    if key == "trusted_keys":
        return "\n".join(f"{key_id} {public}" for key_id, public in sorted(value.items()))
    if isinstance(value, bool):
        return "true" if value else "false"
    return "" if value is None else str(value)


def last_lines(path):
    """The last nonempty lines a start wrote (its error, when it stopped)."""
    try:
        lines = Path(path).read_text(errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join([line for line in lines if line.strip()][-PROBLEM_LINES:])
