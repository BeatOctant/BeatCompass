"""Opt-in smoke test of the actual frozen artifact, with a synthetic engine."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import Request, build_opener, ProxyHandler

import pytest


def stop(process, data_dir):
    # Stop through the management API: terminate() is a hard kill on Windows.
    if process.poll() is not None:
        return
    try:
        info = json.loads((data_dir / "connection.json").read_text())
        base = f'http://127.0.0.1:{info["port"]}'
        request = Request(f"{base}/manage/stop", data=b"{}", headers={"X-OctantCompass-Manage-Key": info["manageKey"]})
        build_opener(ProxyHandler({})).open(request, timeout=3).close()
        process.wait(timeout=10)
    except Exception:
        process.kill()
        process.wait(timeout=5)
        raise


@pytest.fixture
def executable():
    configured = os.environ.get("OCTANTCOMPASS_TEST_EXECUTABLE")
    if not configured:
        pytest.skip("set OCTANTCOMPASS_TEST_EXECUTABLE to test the built artifact")
    path = Path(configured).resolve()
    assert path.is_file()
    return str(path)


def test_standalone_lifecycle_pair_history_profile_and_recommendations(executable, tmp_path, example):
    # The executable must work from a directory without the checkout or venv.
    environment = {key: value for key, value in os.environ.items() if key not in ("PYTHONPATH", "VIRTUAL_ENV")}
    log_path = tmp_path / "host.log"
    connection = tmp_path / "connection.json"
    with log_path.open("w+") as log:
        process = subprocess.Popen([executable, "serve", "--fake-engine", "--data-dir", str(tmp_path)],
                                   cwd=tmp_path, env=environment, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 15
            while not connection.exists():
                assert process.poll() is None, log_path.read_text()
                assert time.monotonic() < deadline, log_path.read_text()
                time.sleep(0.02)
            info = json.loads(connection.read_text())
            opener = build_opener(ProxyHandler({}))
            base = f'http://{info["host"]}:{info["port"]}'
            headers = {"OctantPath-Api-Version": "2.0", "Content-Type": "application/json"}
            def call(path, body=None):
                request = Request(base + path, headers=headers,
                                  data=None if body is None else json.dumps(body).encode())
                with opener.open(request, timeout=3) as response:
                    assert response.status == 200
                    return json.load(response)
            while call("/v1/health")["status"] == "loading":
                assert time.monotonic() < deadline
                time.sleep(0.02)
            second = subprocess.run([executable, "serve", "--fake-engine", "--data-dir", str(tmp_path)],
                                    cwd=tmp_path, env=environment, capture_output=True, timeout=10)
            assert second.returncode == 0, second.stderr.decode()
            # PyInstaller onefile launches the Python host as a child process.
            assert json.loads(connection.read_text())["pid"] == info["pid"]
            code = subprocess.run([executable, "pair", "code", "--data-dir", str(tmp_path)], cwd=tmp_path, env=environment,
                                  capture_output=True, text=True, timeout=10).stdout.strip()
            token = call("/v1/pair", {"secret": code, "name": "synthetic frozen test"})["token"]
            headers["Authorization"] = f"Bearer {token}"
            assert call("/v1/signals", example("signals.request.json"))["count"] == 9
            assert call("/v1/health")["signals"] == 9
            assert call("/v1/profile", example("profile.request.json")) == {}
            assert call("/v1/recommendations", {"count": 3})["candidates"] == []
        finally:
            stop(process, tmp_path)
        assert process.returncode == 0, log_path.read_text()
        assert not connection.exists()
        output = log_path.read_text()
        assert token not in output
        assert code not in output


def test_standalone_default_engine_can_load_runtime(executable, tmp_path):
    log_path = tmp_path / "default.log"
    connection = tmp_path / "connection.json"
    with log_path.open("w") as log:
        process = subprocess.Popen([executable, "serve", "--data-dir", str(tmp_path)],
                                   cwd=tmp_path, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 15
            while not connection.exists():
                assert process.poll() is None, log_path.read_text()
                assert time.monotonic() < deadline, log_path.read_text()
                time.sleep(0.02)
            info = json.loads(connection.read_text())
            opener = build_opener(ProxyHandler({}))
            while True:
                with opener.open(f'http://127.0.0.1:{info["port"]}/v1/health', timeout=3) as response:
                    health = json.load(response)
                if health["status"] != "loading":
                    break
                assert time.monotonic() < deadline, log_path.read_text()
                time.sleep(0.02)
            assert health["status"] == "degraded"
            # db_missing means the runtime loaded and its API version was accepted.
            assert health["reason"] == "db_missing"
        finally:
            stop(process, tmp_path)
        assert process.returncode == 0, log_path.read_text()
        assert not connection.exists()


def test_standalone_carries_what_checking_a_db_signature_loads(executable):
    # OctantVector checks a DB's signature with cryptography, which loads cffi's top-level _cffi_backend. 0.1.3 for
    # Linux left it out with the libraries taken from the system, and every DB import failed.
    from PyInstaller.archive.readers import CArchiveReader
    assert any(name.startswith("_cffi_backend.") for name in CArchiveReader(executable).toc)


def test_standalone_window_starts_the_host_and_stays_one(executable, tmp_path):
    if sys.platform == "linux" and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        pytest.skip("the window needs a display (xvfb-run)")
    log_path = tmp_path / "window.log"
    connection = tmp_path / "connection.json"
    with log_path.open("w") as log:
        window = subprocess.Popen([executable, "gui", "--fake-engine", "--data-dir", str(tmp_path)],
                                  cwd=tmp_path, stdout=log, stderr=log)
        try:
            # The window starts the host, which writes where it listens.
            deadline = time.monotonic() + 60
            while not connection.exists():
                assert window.poll() is None, log_path.read_text()
                assert time.monotonic() < deadline, log_path.read_text()
                time.sleep(0.1)
            info = json.loads(connection.read_text())
            assert info["pid"] != window.pid
            # The window stays open (the system's web view loaded), and a second one only brings it forward.
            second = subprocess.run([executable, "gui", "--data-dir", str(tmp_path)], cwd=tmp_path,
                                    capture_output=True, timeout=60)
            assert second.returncode == 0, second.stderr.decode()
            assert window.poll() is None, log_path.read_text()
            close(executable, tmp_path)
            window.wait(timeout=30)
            # The host keeps running without the window; the next double-click opens the window on it.
            again = subprocess.Popen([executable, "gui", "--data-dir", str(tmp_path)], cwd=tmp_path, stdout=log,
                                     stderr=log)
            deadline = time.monotonic() + 60
            while not windows(executable, tmp_path):
                assert again.poll() is None, log_path.read_text()
                assert time.monotonic() < deadline, "the window did not open again"
                time.sleep(0.1)
            time.sleep(5)
            assert again.poll() is None, log_path.read_text()
            status = Request(f'http://127.0.0.1:{info["port"]}/manage/status',
                             headers={"X-OctantCompass-Manage-Key": info["manageKey"]})
            with build_opener(ProxyHandler({})).open(status, timeout=3) as response:
                assert json.load(response)["pid"] == info["pid"]
            close(executable, tmp_path)
            again.wait(timeout=30)
        finally:
            for pid in windows(executable, tmp_path):
                kill(pid)
            # The host runs until it is stopped.
            stop_host(tmp_path)


def windows(executable, data_dir):
    """The processes of the window for data_dir: `gui` processes of this executable (two for the one-file executable,
    the one that unpacked it and the window)."""
    if sys.platform == "win32":
        script = (f"Get-CimInstance Win32_Process -Filter \"Name='{Path(executable).name}'\" | "
                  "ForEach-Object { \"$($_.ProcessId) $($_.CommandLine)\" }")
        found = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                               capture_output=True, text=True, timeout=30).stdout.splitlines()
        rows = [line.split(" ", 1) for line in found if " " in line]
    else:
        rows = []
        for process in Path("/proc").iterdir():
            try:
                rows.append((process.name, (process / "cmdline").read_bytes().replace(b"\0", b" ").decode()))
            except OSError:
                continue
    return [int(pid) for pid, command in rows
            if pid.isdigit() and executable in command and " gui " in command and str(data_dir) in command]


def close(executable, data_dir):
    """Close the window and wait until it ended. On Windows taskkill without /F asks the window to close, as its
    close button does; on Linux the window ends on SIGTERM. Either way the executable removes its unpacked files."""
    pids = windows(executable, data_dir)
    if sys.platform == "win32":
        subprocess.run(["taskkill", *[part for pid in pids for part in ("/PID", str(pid))]], capture_output=True)
    else:
        for pid in pids:
            kill(pid)
    deadline = time.monotonic() + 30
    while windows(executable, data_dir):
        assert time.monotonic() < deadline, "the window did not close"
        time.sleep(0.1)


def kill(pid):
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def stop_host(data_dir):
    connection = data_dir / "connection.json"
    if not connection.exists():
        return
    info = json.loads(connection.read_text())
    base = f'http://127.0.0.1:{info["port"]}'
    request = Request(f"{base}/manage/stop", data=b"{}", headers={"X-OctantCompass-Manage-Key": info["manageKey"]})
    build_opener(ProxyHandler({})).open(request, timeout=3).close()
    deadline = time.monotonic() + 15
    while connection.exists():
        assert time.monotonic() < deadline
        time.sleep(0.1)
