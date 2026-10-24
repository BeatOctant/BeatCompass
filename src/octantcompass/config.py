"""Host settings (OctantSpecs octantcompass/law.md 1.9; the keys are in README.md).

The defaults are in defaults.json next to this file, bundled with the package and the executable; Python does not
repeat them. settings.json holds only what the user changed and is read over them; loading never writes it.
"""

import ipaddress
import json
import os
import tempfile
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .permissions import private_file
from .places import Places

# Every key settings.json may hold, with its default.
DEFAULTS = json.loads(Path(__file__).with_name("defaults.json").read_text(encoding="utf-8"))
# Settings the window changes: the ones a user changes, in the groups and the order it shows them (the
# window's text settings.<group> names a group). The others, internal waits, are changed in settings.json by hand; the
# DB folder is chosen with the DB switch.
SETTING_GROUPS = (("listen", ("host", "port")), ("import", ("index_url", "allow_unsigned_db", "trusted_keys")),
               ("pairing", ("pair_code_digits", "pair_code_seconds", "pair_code_attempts")),
               ("running", ("start_at_login", "idle_minutes", "log_lines")))
EDITABLE = tuple(key for _, keys in SETTING_GROUPS for key in keys)
# Settings that need the listener made again (a restart from the window); the others apply as soon as they are saved.
RESTART_KEYS = ("host", "port")
# What a restart from the window reads again: every setting, those written by hand too, but the DB folder.
RELOADED = tuple(key for key in DEFAULTS if key != "db")
# Settings the window edits as on or off.
BOOLEAN_KEYS = ("allow_unsigned_db", "start_at_login")


NonEmpty = Annotated[str, Field(min_length=1)]
Positive = Annotated[float, Field(gt=0, allow_inf_nan=False)]


class _Checks(BaseModel):
    """What each setting may be. Strict: "16" is not 16 and true is not 1. Floats take whole numbers, not infinity."""
    model_config = ConfigDict(strict=True)

    host: NonEmpty
    port: Annotated[int, Field(ge=0, le=65535)]
    pair_code_digits: Annotated[int, Field(gt=0)]
    pair_code_attempts: Annotated[int, Field(gt=0)]
    log_lines: Annotated[int, Field(gt=0)]
    pair_code_seconds: Positive
    health_timeout: Positive
    poll_seconds: Positive
    startup_timeout: Positive
    shutdown_timeout: Positive
    index_max_megabytes: Positive
    idle_minutes: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    index_url: NonEmpty | None
    trusted_keys: dict[NonEmpty, NonEmpty]  # keyId -> public key
    allow_unsigned_db: bool
    start_at_login: bool


@dataclass(frozen=True)
class Config:
    places: Places  # a folder (--data-dir) is made into one
    db: Path | None = DEFAULTS["db"]  # none: db/ in the data folder
    host: str = DEFAULTS["host"]
    # Contract 6 (client to OctantCompass) 2.0: OctantPath finds a OctantCompass on the same PC at 127.0.0.1 on the
    # default port. 0 still asks the OS for a free port.
    port: int = DEFAULTS["port"]
    pair_code_digits: int = DEFAULTS["pair_code_digits"]
    pair_code_seconds: float = DEFAULTS["pair_code_seconds"]
    pair_code_attempts: int = DEFAULTS["pair_code_attempts"]
    health_timeout: float = DEFAULTS["health_timeout"]
    poll_seconds: float = DEFAULTS["poll_seconds"]
    startup_timeout: float = DEFAULTS["startup_timeout"]
    shutdown_timeout: float = DEFAULTS["shutdown_timeout"]
    # Contract 5 (OctantHarvest to OctantCompass) index: URL or local path; none = local folders only
    index_url: str | None = DEFAULTS["index_url"]
    # The largest index read; the parts it lists are read up to the sizes it gives
    index_max_megabytes: float = DEFAULTS["index_max_megabytes"]
    log_lines: int = DEFAULTS["log_lines"]  # recent events kept in memory for the window
    # The DB (about 2 GB once loaded) is let go after this many minutes without a request from a paired environment
    # and loaded again on the next one; the host starts without it. 0 loads it at the start and keeps it.
    idle_minutes: float = DEFAULTS["idle_minutes"]
    # {keyId: Ed25519 public key base64} trusted besides OctantVector's bundled keys
    trusted_keys: dict | None = field(default_factory=lambda: dict(DEFAULTS["trusted_keys"]))
    # Development only: no warning for DB updates without manifest.sig
    allow_unsigned_db: bool = DEFAULTS["allow_unsigned_db"]
    # Start OctantCompass (`serve`, no window) when the user logs in. Off by default (OctantSpecs octantcompass/law.md 3.1);
    # the window writes or removes the OS entry when it saves this (shortcut.set_start_at_login).
    start_at_login: bool = DEFAULTS["start_at_login"]

    @property
    def index_max_bytes(self):
        return int(self.index_max_megabytes * 1024 * 1024)

    def update_trust(self):
        """The `trust` argument of contract 1 (OctantCompass to OctantVector) apply_update (OctantSpecs contracts/distribution.md, 署名)."""
        return {"keys": dict(self.trusted_keys), "allowUnsigned": self.allow_unsigned_db}

    def __post_init__(self):
        object.__setattr__(self, "places", Places.of(self.places))
        object.__setattr__(self, "db", Path(self.db) if self.db else self.places.data / "db")
        object.__setattr__(self, "trusted_keys", dict(self.trusted_keys or {}))
        try:
            _Checks.model_validate({name: getattr(self, name) for name in _Checks.model_fields})
        except ValidationError as error:
            first = error.errors()[0]
            raise ValueError(f"{first['loc'][0]}: {first['msg']}") from None

    @classmethod
    def load(cls, places, **overrides):
        """places: a Places, or a folder (--data-dir)."""
        places = Places.of(places)
        values = read_settings(places)
        values = {key: value for key, value in values.items() if key in DEFAULTS}
        values.update({key: value for key, value in overrides.items() if value is not None})
        return cls(places=places, **values)


class LiveConfig:
    """The settings this run uses now. Saving a setting replaces it here at once, except RESTART_KEYS, which a
    restart from the window reads again (with RELOADED). Command-line values (overrides) keep winning over
    settings.json."""

    def __init__(self, config, overrides=None):
        self.current = config
        self.overrides = {key: value for key, value in (overrides or {}).items() if value is not None}

    def reload(self, keys):
        """Read settings.json again and use its values for keys; raises ValueError or OSError if it does not load."""
        saved = Config.load(self.current.places, **self.overrides)
        self.current = replace(self.current, **{key: getattr(saved, key) for key in keys})
        return self.current


def read_settings(places):
    path = Places.of(places).settings
    values = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(values, dict):
        raise ValueError("settings must be a JSON object")
    return values


def parse_setting(key, text):
    """A value typed in the window -> the settings.json value; None removes the key (the default)."""
    if key not in EDITABLE:
        raise ValueError(f"{key} is not a setting the window changes")
    text = text.strip()
    if key == "trusted_keys":
        # One key per line: keyId, a space, the public key (base64).
        keys = {}
        for line in filter(None, (line.strip() for line in text.splitlines())):
            key_id, _, public = line.partition(" ")
            if not public.strip():
                raise ValueError("write each key as `keyId public-key`")
            keys[key_id] = public.strip()
        return keys or None
    if not text:
        return None
    if key == "host" and text != "localhost":
        ipaddress.ip_address(text)  # an address this machine can listen on; a typo would stop the next start
    if key in BOOLEAN_KEYS:
        if text not in ("true", "false"):
            raise ValueError(f"{key} must be true or false")
        return text == "true"
    kind = {f.name: f.type for f in fields(Config)}[key]
    return int(text) if kind is int else float(text) if kind is float else text


def same_value(a, b):
    """Whether two settings values are the same: 2 and 2.0 are (the window writes some numbers as floats), true and 1
    are not."""
    return a == b and (type(a) is bool) == (type(b) is bool)


def save_setting(places, key, value):
    """Write one setting into settings.json (other keys, known or not, stay) after checking the whole file still
    loads. None or the default removes the key, so the file keeps only what the user changed, and nothing is written
    when the file would not change. LiveConfig.reload makes the running host use it."""
    places = Places.of(places)
    before = read_settings(places)
    values = dict(before)
    if value is None or (key in DEFAULTS and same_value(value, DEFAULTS[key])):
        values.pop(key, None)
    else:
        values[key] = value
    Config(places=places, **{k: v for k, v in values.items() if k in DEFAULTS})
    if values != before:
        write_settings(places, values)


def write_settings(places, values):
    """Replace settings.json with `values` at once, readable by the owner only. It is written, and read_settings reads
    it, in the locale's encoding."""
    path = Places.of(places).settings
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".settings-")
    try:
        with os.fdopen(descriptor, "w") as file:
            private_file(Path(temporary))
            json.dump(values, file, indent=2, ensure_ascii=False)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
