"""Connection-file details are kept here; where files go is in places.py.

Since contract 6 (client to OctantCompass) 2.0 the connection file is OctantCompass's own: it stops a second
instance and lets the window find the host. OctantPath does not read it, and it holds no pairing secret."""

import asyncio
import ipaddress
import json
import os
import signal
import socket
import sys
from dataclasses import replace
from datetime import datetime, timezone
from contextlib import contextmanager
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

import uvicorn

from . import __version__
from .activity import log
from .permissions import write_private
from .config import RELOADED, LiveConfig


def connection_path(runtime):
    """runtime: Places.runtime, where the files that live only while OctantCompass runs are."""
    return runtime / "connection.json"


def process_alive(pid):
    if type(pid) is not int or pid <= 0:
        return False
    if sys.platform == "win32":
        # os.kill(pid, 0) is not a safe existence probe on Windows.
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.OpenProcess(0x00100000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5  # Access denied: process exists.
        try:
            return kernel.WaitForSingleObject(handle, 0) == 258
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def running_connection(runtime, timeout):
    try:
        info = json.loads(connection_path(runtime).read_text())
        if not process_alive(info["pid"]):
            return False
        host = info["host"]
        if host == "0.0.0.0":
            host = "127.0.0.1"
        elif host == "::":
            host = "::1"
        if ":" in host:
            host = f"[{host}]"
        # Loopback probing must not go through a configured HTTP proxy.
        with build_opener(ProxyHandler({})).open(f'http://{host}:{info["port"]}/v1/health', timeout=timeout) as response:
            body = json.load(response)
        return response.status == 200 and "apiVersion" in body and "hostVersion" in body
    except (OSError, URLError, ValueError, KeyError, TypeError):
        return False


def publish_connection(runtime, host, port, manage_key=None):
    """manage_key: the management API's key for this run (manage.py); the file is readable by its owner only."""
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = {"host": host, "port": port, "pid": os.getpid(), "hostVersion": __version__,
            "startedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
    if manage_key is not None:
        info["manageKey"] = manage_key
    write_private(connection_path(runtime), json.dumps(info))


def running_address(runtime):
    """(host, port) of the OctantCompass running with these places, as this PC reaches it, or None. The management
    API answers loopback clients only, so an address that listens everywhere is reached on loopback."""
    try:
        info = json.loads(connection_path(runtime).read_text())
        host = info["host"]
        port = info["port"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if type(port) is not int or not isinstance(host, str):
        return None
    return ("::1" if ":" in host else "127.0.0.1"), port


def running_manage_key(runtime):
    """The management API's key of the OctantCompass running with these places, or None."""
    try:
        key = json.loads(connection_path(runtime).read_text())["manageKey"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return key if isinstance(key, str) else None


class InstanceLock:
    """One process at a time per name in the runtime folder: `instance.lock` for the host, `window.lock` for the
    window."""

    def __init__(self, runtime, name="instance.lock"):
        runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.file = (runtime / name).open("a+b")

    def acquire(self):
        try:
            if sys.platform == "win32":
                import msvcrt
                self.file.seek(0)
                self.file.write(b"0")
                self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            return False
        return True

    def close(self):
        self.file.close()


class ManagedServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        # Lifecycle owns signals so cleanup runs before signal handlers restore.
        yield


def beyond_this_pc(host):
    """True when the address is reachable from other devices: pairing codes and tokens then cross the network as plain HTTP."""
    if host == "localhost":
        return False
    try:
        return not ipaddress.ip_address(host).is_loopback
    except ValueError:
        return True


def bind(host, port):
    sock = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET)
    try:
        if sys.platform != "win32":
            # A restart binds the same port again right away (Windows' SO_REUSEADDR means something else).
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.setblocking(False)
    except OSError:
        sock.close()
        raise
    return sock


class Listener:
    """One uvicorn server on one socket. The app and its lifespan outlive it, so a restart from the window only
    makes the listener again: the engine, the DB, pairing and the log stay as they are."""

    def __init__(self, app, sock, host, shutdown_timeout):
        self.host = host
        self.port = sock.getsockname()[1]
        self.sock = sock
        self.server = ManagedServer(uvicorn.Config(app, lifespan="off", access_log=False, log_level="warning",
                                                   timeout_graceful_shutdown=shutdown_timeout))
        self.task = asyncio.create_task(self.server.serve(sockets=[sock]))

    @property
    def listen(self):
        return f"{self.host}:{self.port}"

    async def started(self, timeout, poll_seconds):
        deadline = asyncio.get_running_loop().time() + timeout
        while not self.server.started and not self.task.done() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(poll_seconds)
        return self.server.started and not self.task.done()

    async def close(self):
        self.server.should_exit = True
        try:
            await self.task
        except Exception:
            pass
        self.sock.close()


async def serve(config, make_app, on_ready=None):
    """config: a Config, or the LiveConfig the window changes; a restart reads the settings again."""
    live = config if isinstance(config, LiveConfig) else LiveConfig(config)
    config = live.current
    places = config.places
    places.make()
    if await asyncio.to_thread(running_connection, places.runtime, config.health_timeout):
        return 0
    lock = InstanceLock(places.runtime)
    if not lock.acquire():
        try:
            deadline = asyncio.get_running_loop().time() + config.startup_timeout
            while asyncio.get_running_loop().time() < deadline:
                if await asyncio.to_thread(running_connection, places.runtime, config.health_timeout):
                    return 0
                await asyncio.sleep(config.poll_seconds)
            raise RuntimeError("another instance holds the startup lock but is not responding")
        finally:
            lock.close()
    requested = {"stop": False, "restart": False}
    listener = None
    published = False
    old_handlers = {}
    try:
        app = make_app()

        def stop(signum=None, frame=None):
            requested["stop"] = True

        def request_restart():
            requested["restart"] = True

        def show(listener):
            if beyond_this_pc(listener.host):
                log.warning("listening beyond this PC on %s: the pairing code and tokens travel unencrypted (plain HTTP); "
                            "use it only on a network you trust, or 127.0.0.1 with a USB connection", listener.listen)
            publish_connection(places.runtime, listener.host, listener.port, app.state.manage_key)
            app.state.server = {"listen": listener.listen,
                                "startedAt": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")}

        app.state.stop = stop
        app.state.restart = request_restart
        app.state.restart_result = None
        for signum in (signal.SIGINT, signal.SIGTERM):
            old_handlers[signum] = signal.signal(signum, stop)
        async with app.router.lifespan_context(app):
            # Listeners close before the lifespan ends, as uvicorn would order it.
            try:
                try:
                    sock = bind(config.host, config.port)
                except OSError as exc:
                    # Said where, as a restart says it: the window shows this when a start from it fails.
                    raise OSError(f"{config.host}:{config.port}: {exc.strerror or exc}") from exc
                listener = Listener(app, sock, config.host, config.shutdown_timeout)
                if not await listener.started(config.startup_timeout, config.poll_seconds) or requested["stop"]:
                    return 0
                show(listener)
                published = True
                log.info("listening on %s", listener.listen)
                if on_ready is not None:
                    on_ready()
                while not requested["stop"] and not listener.task.done():
                    config = live.current
                    if requested["restart"]:
                        requested["restart"] = False
                        listener = await relisten(app, live, listener, show)
                        if listener is None:
                            return 1
                    await asyncio.sleep(config.poll_seconds)
                return 0
            finally:
                if listener is not None:
                    await listener.close()
    finally:
        if published:
            connection_path(places.runtime).unlink(missing_ok=True)
        lock.close()
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)


async def relisten(app, live, old, show):
    """A restart from the window: read the settings again and listen where they say now. If that fails, listen where
    it did before and say why in the window. Returns the listener, None only when neither address could be listened
    on again."""
    number = (app.state.restart_result or {}).get("number", 0) + 1
    before = live.current

    def result(listener, problem=None):
        app.state.restart_result = {"number": number, "ok": problem is None, "listen": listener and listener.listen,
                                    "problem": problem}
        if problem:
            # The address in use stays the one the settings show as running; the saved one still differs.
            live.current = replace(live.current, host=before.host, port=before.port)
            log.error("restart from the window failed (%s); listening on %s as before", problem,
                      listener.listen if listener else "nothing")
        else:
            log.info("restarted from the window; listening on %s", listener.listen)

    try:
        config = live.reload(RELOADED)
    except (OSError, ValueError, TypeError) as exc:
        result(old, f"settings.json: {exc}")
        return old
    # An automatic port (0) keeps the port it has, so OctantPath finds it again.
    host, port = config.host, config.port or old.port
    timeout, poll = config.startup_timeout, config.poll_seconds
    if port != old.port:
        # Another port: start there while the old one still answers, and close the old one once the new one does.
        try:
            listener = Listener(app, bind(host, port), host, config.shutdown_timeout)
        except OSError as exc:
            result(old, f"{host}:{port}: {exc.strerror or exc}")
            return old
        if not await listener.started(timeout, poll):
            await listener.close()
            result(old, f"{host}:{port}: did not start")
            return old
        show(listener)
        await old.close()
        result(listener)
        return listener
    # The same port: let go of it first, then listen again; back to the old address if the new one fails.
    await old.close()
    problem = None
    for attempt_host in (host, old.host):
        try:
            listener = Listener(app, bind(attempt_host, port), attempt_host, config.shutdown_timeout)
        except OSError as exc:
            problem = problem or f"{attempt_host}:{port}: {exc.strerror or exc}"
            continue
        if await listener.started(timeout, poll):
            show(listener)
            result(listener, problem)
            return listener
        await listener.close()
        problem = problem or f"{attempt_host}:{port}: did not start"
    result(None, problem)
    return None
