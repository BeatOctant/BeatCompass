"""The OctantCompass window: the page in ui/ in a window of its own, not a browser tab (pywebview: Edge WebView2 on
Windows, WebKitGTK on Linux; 2026-10-18). It shows in the taskbar as OctantCompass, and closing it leaves the host
running for OctantPath.

The page talks to Python through pywebview's bridge (Bridge), not over HTTP: the window has no server of its own.
The bridge passes the running host's management API (manage.py) through and does what the window owns (manager.py),
so settings, starting and stopping work while the host is stopped.

One window per data folder: a second `gui` (a double-click) writes window-request.json in the runtime folder and
ends, and the open window comes to the front.

The window keeps its own choices in window.json in the config folder: the language and the theme (light, dark, or
the system's), and where the window was when it last closed (placement.py).
"""

import json
import os
import sys
import threading
from pathlib import Path

from . import minsize, placement
from .frozen import use_system_environment
from .lifecycle import InstanceLock
from .manager import Refused
from .permissions import write_private

PAGE = Path(__file__).parent / "ui"
LANGUAGES = ("en", "ja")
THEMES = ("auto", "dark", "light")
# The window's color before the page shows (the page's --bg); the system's theme is not known here, so auto is dark.
BACKGROUND = {"auto": "#0f1115", "dark": "#0f1115", "light": "#f5f6f8"}
TITLE = "OctantCompass"
# The window's first size in CSS pixels, which pywebview takes as the inside on Linux and as the outside, frame
# included, on Windows: the page's smallest (minsize) and room for a frame (Windows 10 and 11: about 16 x 39), so the
# window does not grow to its minimum just after it shows.
SIZE = (minsize.PAGE_MIN[0] + 40, minsize.PAGE_MIN[1] + 80)
REQUEST = "window-request.json"
# How often the open window looks for a request to come to the front.
REQUEST_SECONDS = 0.5
# The name the desktop knows the window by: the Linux menu entry is octantcompass.desktop (shortcut.py).
APP_ID = "octantcompass"


class WindowUnavailable(Exception):
    """No window can be shown here: pywebview is not installed (the gui extra), or the system has no web view."""


def run(places, manager):
    """Open the window, or bring the open one to the front; start the host first when it is not running (manager: a
    Manager of places, with the host options given to `gui`). Returns the exit code."""
    lock = InstanceLock(places.runtime, "window.lock")
    if not lock.acquire():
        lock.close()
        write_private(places.runtime / REQUEST, "")
        return 0
    try:
        webview = load_webview()
        bridge = Bridge(manager, places)
        theme = bridge.theme()
        shown = webview.create_window(TITLE, html=page(theme), js_api=bridge, width=SIZE[0], height=SIZE[1],
                                      background_color=BACKGROUND[theme])
        bridge._attach(shown, placement.keep(shown, bridge._bounds()))
        minsize.keep(shown)
        hide_console()
        # Started from here, the host is its own process: it keeps running when the window closes.
        threading.Thread(target=manager.start, daemon=True).start()
        webview.start(watch_requests, (bridge, places.runtime / REQUEST))
        return 0
    finally:
        lock.close()


def load_webview():
    try:
        import webview
    except ImportError as exc:
        raise WindowUnavailable("the window needs pywebview (pip install 'octantcompass[gui]')") from exc
    if sys.platform == "linux":
        # Before GTK starts: the window's application id (Wayland) and WM_CLASS (X11), so the desktop shows it as
        # OctantCompass under its menu entry, not as python or an unknown program.
        try:
            from gi.repository import GLib
        except ImportError as exc:
            raise WindowUnavailable("the window needs PyGObject and WebKitGTK 4.1 (libwebkit2gtk-4.1)") from exc
        GLib.set_prgname(APP_ID)
        GLib.set_application_name(TITLE)
        # WebKitGTK shows the page through programs installed with it, which must not load the executable's libraries.
        use_system_environment()
    return webview


def page(theme="auto"):
    """The page, with the theme chosen in the window (the page resolves auto with the system's before it shows) and
    its smallest size (minsize)."""
    html = (PAGE / "index.html").read_text(encoding="utf-8")
    width, height = minsize.PAGE_MIN
    return html.replace('<html lang="en">', f'<html lang="en" data-theme-setting="{theme}" '
                        f'style="--page-min-width: {width}px; --page-min-height: {height}px">', 1)


def hide_console():
    """On Windows a double-click opens a console for the executable; hide it when nothing else uses it (OctantCompass
    started from a terminal keeps the terminal). The standalone executable is two processes, both in it."""
    if sys.platform != "win32":
        return
    import ctypes
    kernel = ctypes.WinDLL("kernel32")
    processes = (ctypes.c_uint32 * 8)()
    count = kernel.GetConsoleProcessList(processes, len(processes))
    ours = {os.getpid(), os.getppid()} if getattr(sys, "frozen", False) else {os.getpid()}
    if 0 < count <= len(processes) and set(processes[:count]) <= ours:
        ctypes.WinDLL("user32").ShowWindow(kernel.GetConsoleWindow(), 0)  # SW_HIDE


def watch_requests(bridge, request):
    """Runs beside the window until it closes: a second `gui` asked for this window."""
    while not bridge._window.events.closed.wait(REQUEST_SECONDS):
        if take_request(request):
            bridge._bring_forward()


def take_request(request):
    """Whether a second `gui` asked for the window since the last look; the request is removed so it is answered
    once."""
    try:
        request.unlink()
    except OSError:
        return False
    return True


def answer(call, *args):
    """A bridge result: {"ok": true, "data": ...}, or {"ok": false, "data": {"error": {"code": ...}}} as the host's
    management API answers a refusal."""
    try:
        return {"ok": True, "data": call(*args)}
    except Refused as refused:
        return {"ok": False, "data": {"error": {"code": refused.code}}}


class Bridge:
    """What the page calls (window.pywebview.api.<name>). Names with an underscore are not offered to the page; they
    are what this module does with the window."""

    def __init__(self, manager, places):
        self._manager = manager
        self._preferences = places.config / "window.json"
        self._window = None
        self._placement = None
        self._close_refused = False

    def _attach(self, window, placement=None):
        """The window this bridge serves. It is created with the bridge (js_api), so it comes after. placement reads
        the window's bounds when it closes (placement.keep)."""
        self._window = window
        self._placement = placement
        window.events.closing += self._closing

    def _bring_forward(self):
        self._window.restore()
        self._window.show()

    def _closing(self):
        """The window stays open while a DB switch, import or update runs here (it would stop halfway); the page
        says so."""
        if self._manager.busy():
            self._close_refused = True
            return False
        if self._placement is not None:
            self._remember("bounds", self._placement.bounds())
        return True

    def _preference(self, key, allowed, default):
        try:
            value = json.loads(self._preferences.read_text()).get(key)
        except (OSError, ValueError, AttributeError):
            value = None
        return value if value in allowed else default

    def _bounds(self):
        """Where the window was when it last closed, as placement.py keeps it (placement.fit checks it)."""
        try:
            return json.loads(self._preferences.read_text()).get("bounds")
        except (OSError, ValueError, AttributeError):
            return None

    def _remember(self, key, value):
        """Write one choice into window.json, keeping the others."""
        try:
            kept = json.loads(self._preferences.read_text())
        except (OSError, ValueError):
            kept = {}
        self._preferences.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        write_private(self._preferences, json.dumps({**(kept if isinstance(kept, dict) else {}), key: value}))

    def language(self):
        """The language chosen in the window, "auto" when none (the page then follows the system's)."""
        return self._preference("language", LANGUAGES, "auto")

    def choose_language(self, language):
        """Kept in window.json for the next window."""
        def choose():
            if language != "auto" and language not in LANGUAGES:
                raise Refused("invalid_language")
            self._remember("language", language)
            return self.language()
        return answer(choose)

    def theme(self):
        """The theme chosen in the window: "auto" (the system's), "dark" or "light"."""
        return self._preference("theme", THEMES, "auto")

    def choose_theme(self, theme):
        """Kept in window.json for the next window."""
        def choose():
            if theme not in THEMES:
                raise Refused("invalid_theme")
            self._remember("theme", theme)
            return self.theme()
        return answer(choose)

    def texts(self, language):
        """The page's texts in a language (ui/lang/<language>.json)."""
        if language not in LANGUAGES:
            return {}
        return json.loads((PAGE / "lang" / f"{language}.json").read_text(encoding="utf-8"))

    def host(self, path, body=None):
        """The running host's management API, as the page would call it over HTTP: {"ok", "status", "data"}."""
        try:
            status, data = self._manager.host(path, body)
        except Refused as refused:
            return {"ok": False, "status": refused.status, "data": {"error": {"code": refused.code}}}
        return {"ok": 200 <= status < 300, "status": status, "data": data}

    def status(self):
        """The Manager's status, and whether closing the window was refused while the DB change still runs."""
        status = self._manager.status()
        if status["db"]["work"] is None:
            self._close_refused = False
        status["closeRefused"] = self._close_refused
        return status

    def switch_db(self, path):
        return answer(self._manager.switch_db, path)

    def import_db(self, path):
        return answer(self._manager.import_db, path)

    def check_update(self, index=None):
        return answer(self._manager.check_update, index)

    def update_db(self, index=None):
        return answer(self._manager.update_db, index)

    def cancel_update(self):
        return answer(self._manager.cancel_update)

    def start(self):
        return answer(self._manager.start)

    def settings(self):
        return answer(self._manager.settings)

    def save_setting(self, key, value):
        return answer(self._manager.save_setting, key, value)

    def pick(self, start=None, file=False):
        """The OS chooser on this PC: a DB folder, or a DB archive (.tar) when file. -> the path chosen, or None."""
        import webview
        start = Path(start).expanduser() if start else Path.home()
        while not start.is_dir() and start != start.parent:
            start = start.parent
        kind = webview.FileDialog.OPEN if file else webview.FileDialog.FOLDER
        types = ("DB (*.tar)",) if file else ()
        chosen = self._window.create_file_dialog(kind, directory=str(start), file_types=types)
        return {"ok": True, "data": str(chosen[0]) if chosen else None}
