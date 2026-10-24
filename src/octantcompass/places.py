"""Where OctantCompass keeps its files.

With --data-dir everything is in that one folder, as before. Without it, Linux follows the XDG base directories
(2026-10-14, through platformdirs): what a person edits, what must not be lost, the DBs, and what exists only while OctantCompass runs
each go to their own place. Windows and macOS keep one folder. Nothing is cached on disk: the signals OctantPath
sends live in memory while the host runs (2026-10-22).

| place   | files                                                                | Linux default                            |
|---------|----------------------------------------------------------------------|------------------------------------------|
| config  | settings.json                                                        | $XDG_CONFIG_HOME/OctantCompass             |
| state   | pairing.json, .pairing.json.lock, db-selection.json, db-request.json | $XDG_STATE_HOME/OctantCompass              |
| data    | db/, dbs/ (DBs taken from the index), downloads/ (during an update)  | $XDG_DATA_HOME/OctantCompass               |
| runtime | connection.json, instance.lock                                       | $XDG_RUNTIME_DIR/OctantCompass, else state |
"""

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from platformdirs.macos import MacOS
from platformdirs.unix import Unix
from platformdirs.windows import Windows

NAME = "OctantCompass"


def dirs():
    """platformdirs for this system, chosen by sys.platform each time (the tests switch it)."""
    kind = {"win32": Windows, "darwin": MacOS}.get(sys.platform, Unix)
    return kind(NAME, appauthor=False, roaming=False)


def single_folder():
    """The one folder of the systems other than Linux: Windows, macOS, and others under the XDG data folder."""
    return dirs().user_data_path


@dataclass(frozen=True)
class Places:
    config: Path
    state: Path
    data: Path
    runtime: Path
    folder: Path | None = None  # the --data-dir this was made from; None for the platform default

    @classmethod
    def at(cls, folder):
        """Everything in one folder (--data-dir)."""
        folder = Path(folder)
        return cls(folder, folder, folder, folder, folder)

    @classmethod
    def default(cls):
        if sys.platform != "linux":
            folder = single_folder()
            return cls(folder, folder, folder, folder)
        d = dirs()
        state = d.user_state_path
        # Without XDG_RUNTIME_DIR the runtime files stay with the state (platformdirs would guess /run/user/<uid>).
        runtime = d.user_runtime_path if os.environ.get("XDG_RUNTIME_DIR", "").strip() else state
        return cls(config=d.user_config_path, state=state, data=d.user_data_path, runtime=runtime)

    @classmethod
    def of(cls, value):
        """A Places as it is, or a folder made into one."""
        return value if isinstance(value, cls) else cls.at(value)

    @property
    def settings(self):
        return self.config / "settings.json"

    @property
    def pairing(self):
        return self.state / "pairing.json"

    def make(self):
        for folder in dict.fromkeys((self.config, self.state, self.data, self.runtime)):
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)

    def shown(self):
        """For the window: the one folder, or each place on its own line."""
        if len({self.config, self.state, self.data, self.runtime}) == 1:
            return str(self.data)
        return "\n".join(f"{key}: {getattr(self, key)}" for key in ("config", "state", "data", "runtime"))
