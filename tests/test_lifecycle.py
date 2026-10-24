import json
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

import pytest

from octantcompass.lifecycle import beyond_this_pc, connection_path, process_alive, publish_connection, running_connection
from octantcompass.places import Places


def command(data_dir, *arguments):
    return [sys.executable, "-m", "octantcompass", *arguments, "--data-dir", str(data_dir)]


def cli(data_dir, *arguments):
    return subprocess.run(command(data_dir, *arguments), capture_output=True, text=True, timeout=10)


def await_connection(data_dir, process):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail(f"server exited: {process.communicate()}")
        if running_connection(data_dir, 0.2):
            return json.loads(connection_path(data_dir).read_text())
        time.sleep(0.02)
    pytest.fail("server did not publish a working connection")


@pytest.fixture
def server(tmp_path):
    processes = []

    def start(*args):
        process = subprocess.Popen(command(tmp_path, "serve", *args), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        processes.append(process)
        return process

    yield start
    for process in processes:
        if process.poll() is None:
            process.terminate()
        try:
            process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()


def request(info, path, body=None, token=None):
    from urllib.request import Request
    headers = {"OctantPath-Api-Version": "2.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None if body is None else json.dumps(body).encode()
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = Request(f'http://127.0.0.1:{info["port"]}{path}', data=data, headers=headers)
    with build_opener(ProxyHandler({})).open(req, timeout=3) as response:
        return json.load(response)


def test_real_server_duplicate_pairing_cli_and_shutdown(tmp_path, server):
    process = server("--fake-engine")
    info = await_connection(tmp_path, process)
    assert info["pid"] == process.pid
    assert info["host"] == "127.0.0.1" and info["port"] > 0
    # No pairing through files (contract 6 2.0), and no format version: the file is OctantCompass's own. It holds the
    # management API's key for this run (manage.py), which is why only its owner reads it.
    assert info.keys() == {"host", "port", "pid", "hostVersion", "startedAt", "manageKey"}
    assert len(info["manageKey"]) >= 32
    assert info["startedAt"].endswith("Z")
    if os.name != "nt":
        assert stat.S_IMODE(connection_path(tmp_path).stat().st_mode) == 0o600
    assert cli(tmp_path, "serve", "--fake-engine").returncode == 0
    assert json.loads(connection_path(tmp_path).read_text()) == info
    # The same PC pairs with a code as any other device does (contract 6 (client to OctantCompass) 2.0).
    first_code = cli(tmp_path, "pair", "code").stdout.strip()
    token = request(info, "/v1/pair", {"secret": first_code, "name": "synthetic local"})["token"]
    assert request(info, "/v1/health", token=token)["signals"] == 0
    code = cli(tmp_path, "pair", "code")
    assert code.returncode == 0 and len(code.stdout.strip()) == 6
    remote_token = request(info, "/v1/pair", {"secret": code.stdout.strip(), "name": "synthetic remote"})["token"]
    environments = cli(tmp_path, "env", "list")
    assert environments.returncode == 0
    entries = json.loads(environments.stdout)
    assert len(entries) == 2
    assert token not in environments.stdout and remote_token not in environments.stdout
    assert cli(tmp_path, "env", "revoke", entries[0]["id"]).returncode == 0
    assert "paired" not in request(info, "/v1/health", token=token)
    process.terminate()
    stdout, stderr = process.communicate(timeout=10)
    assert not connection_path(tmp_path).exists()
    assert token not in stdout + stderr and first_code not in stdout + stderr
    assert code.stdout.strip() not in stdout + stderr
    replacement = server("--fake-engine")
    second_info = await_connection(tmp_path, replacement)
    assert request(second_info, "/v1/health", token=remote_token)["paired"] is True


@pytest.mark.parametrize("stale", ["invalid JSON", json.dumps({"pid": -1}), json.dumps({"pid": os.getpid(), "host": "127.0.0.1", "port": 0})])
def test_stale_connection_is_replaced(tmp_path, server, stale):
    connection_path(tmp_path).write_text(stale)
    process = server()
    info = await_connection(tmp_path, process)
    assert info["pid"] == process.pid
    health = request(info, "/v1/health")
    deadline = time.monotonic() + 30
    while health["status"] == "loading" and time.monotonic() < deadline:  # an installed OctantVector loads first
        time.sleep(0.1)
        health = request(info, "/v1/health")
    # The default engine is OctantVector; without it installed the reason is engine_unavailable.
    assert health["status"] == "degraded" and health["reason"] in {"db_missing", "engine_unavailable"}


@pytest.mark.skipif(os.name == "nt", reason="POSIX signal integration")
def test_sigint_removes_connection(tmp_path, server):
    process = server()
    await_connection(tmp_path, process)
    process.send_signal(signal.SIGINT)
    process.wait(timeout=10)
    assert not connection_path(tmp_path).exists()


def test_simultaneous_startup_only_one_server(tmp_path, server):
    first = server("--fake-engine")
    second = server("--fake-engine")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if first.poll() is not None or second.poll() is not None:
            break
        time.sleep(0.02)
    survivor, duplicate = (first, second) if second.poll() is not None else (second, first)
    assert duplicate.wait(timeout=3) == 0
    info = await_connection(tmp_path, survivor)
    assert info["pid"] == survivor.pid


def test_connection_atomic_and_platform_paths(tmp_path, monkeypatch):
    for variable in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR"):
        monkeypatch.setenv(variable, str(tmp_path / variable))
    monkeypatch.setattr("octantcompass.places.sys.platform", "linux")
    places = Places.default()
    assert (places.config, places.state, places.data, places.runtime, places.folder) == (
        tmp_path / "XDG_CONFIG_HOME/OctantCompass", tmp_path / "XDG_STATE_HOME/OctantCompass",
        tmp_path / "XDG_DATA_HOME/OctantCompass",
        tmp_path / "XDG_RUNTIME_DIR/OctantCompass", None)
    for variable in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR"):
        monkeypatch.setenv(variable, "")  # set but empty counts as unset (XDG Base Directory)
    places = Places.default()
    assert (places.config, places.state, places.data, places.runtime) == (
        Path.home() / ".config/OctantCompass", Path.home() / ".local/state/OctantCompass",
        Path.home() / ".local/share/OctantCompass",
        Path.home() / ".local/state/OctantCompass")
    assert Places.at(tmp_path) == Places(*[tmp_path] * 5)
    # The Windows folder comes from the system there; here stand in for it with the local AppData folder.
    monkeypatch.setattr("platformdirs.windows.get_win_folder",
                        lambda csidl: str(tmp_path / "local") if csidl == "CSIDL_LOCAL_APPDATA" else None)
    monkeypatch.setattr("octantcompass.places.sys.platform", "win32")
    assert Places.default() == Places(*[tmp_path / "local/OctantCompass"] * 4)
    monkeypatch.setattr("octantcompass.places.sys.platform", "darwin")
    assert Places.default().data == Path.home() / "Library/Application Support/OctantCompass"
    publish_connection(tmp_path, "127.0.0.1", 12345)
    assert json.loads(connection_path(tmp_path).read_text())["port"] == 12345
    assert not list(tmp_path.glob(".connection.json-*"))
    assert not process_alive(-1)


def test_cli_settings_code_and_bad_inputs(tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps({"pair_code_digits": 9}))
    code = cli(tmp_path, "pair", "code")
    assert code.returncode == 0 and len(code.stdout.strip()) == 9
    assert cli(tmp_path, "env", "revoke").returncode != 0
    assert cli(tmp_path, "serve", "--port", "-1").returncode != 0


def test_python_environment_is_isolated():
    assert sys.prefix != sys.base_prefix
    assert "include-system-site-packages = false" in (Path(sys.prefix) / "pyvenv.cfg").read_text().lower()


@pytest.mark.parametrize("host, beyond", [("127.0.0.1", False), ("::1", False), ("localhost", False),
                                          ("0.0.0.0", True), ("::", True), ("192.168.1.10", True)])
def test_an_address_beyond_this_pc_is_told_apart(host, beyond):
    # Listening there sends the pairing code and tokens over plain HTTP, so the start logs a warning.
    assert beyond_this_pc(host) is beyond
