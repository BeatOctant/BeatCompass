import json
from pathlib import Path

import pytest

from octantcompass.pairing import digest


def read_example(contract, name):
    return json.loads((Path(__file__).parent / "examples" / contract / name).read_text())


@pytest.fixture(autouse=True)
def host_settings(tmp_path):
    """Hosts started in tests use a free port and load the DB at once, unless a test says otherwise: the default
    port would clash between tests, and a resting DB waits for a paired request (contract 6 (client to
    OctantCompass) 2.0)."""
    (tmp_path / "settings.json").write_text(json.dumps({"port": 0, "idle_minutes": 0}))


@pytest.fixture(autouse=True)
def login_entries(tmp_path_factory, monkeypatch):
    """Where a test's login entry would go: never the real autostart folder or Startup of whoever runs the tests."""
    home = tmp_path_factory.mktemp("login")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "config"))
    monkeypatch.setenv("APPDATA", str(home / "appdata"))
    return home


@pytest.fixture(autouse=True)
def systemctl(monkeypatch):
    """A stand-in for `systemctl --user`, so a test never enables a unit in the user manager of whoever runs it. It
    records the calls and links an enabled unit as systemctl would; `available = False` acts as no user manager."""
    from octantcompass import shortcut

    class Systemctl:
        available = True
        calls = []

        def __call__(self, *args):
            if not self.available:
                raise RuntimeError("no user manager (test)")
            self.calls.append(args)
            if args[0] == "enable":
                wants = shortcut.login_entry_path().parent / "default.target.wants"
                wants.mkdir(parents=True, exist_ok=True)
                (wants / args[1]).unlink(missing_ok=True)
                (wants / args[1]).symlink_to(shortcut.login_entry_path())
            if args[0] == "disable":
                (shortcut.login_entry_path().parent / "default.target.wants" / args[1]).unlink(missing_ok=True)

    fake = Systemctl()
    fake.calls = []
    monkeypatch.setattr(shortcut, "systemctl", fake)
    return fake


@pytest.fixture
def example():
    return lambda name: read_example("contract3", name)


@pytest.fixture
def engine_example():
    """Contract 1 (OctantCompass to OctantVector) return values (OctantSpecs contracts/examples/engine-runtime)."""
    return lambda name: read_example("contract1", name)


def standing_secret(pairing, secret):
    """A reusable pairing secret for in-process tests, so they need not read a random code back. OctantCompass itself
    only issues single-use codes (contract 6 (client to OctantCompass) 2.0)."""
    # One standing secret per store: a new one replaces the old.
    pair = pairing.__dict__.get("unpatched_pair", pairing.pair)
    pairing.unpatched_pair = pair

    def pair_with_standing_secret(given, name):
        if given == secret:
            pairing.issue_code(digits=1, seconds=60, attempts=1)
            value = pairing.read()
            value["code"]["hash"] = digest(secret)
            pairing.write(value)
        return pair(given, name)
    pairing.pair = pair_with_standing_secret
