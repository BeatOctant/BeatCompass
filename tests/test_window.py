import json
import re
import threading
from types import SimpleNamespace

import pytest

from octantcompass import minsize, placement, window
from octantcompass.engine.fake import FakeVectorEngine
from octantcompass.lifecycle import InstanceLock
from octantcompass.manager import Manager
from octantcompass.places import Places

from test_lifecycle import await_connection, server  # noqa: F401 (fixture)


def bridge_for(data_dir):
    places = Places.at(data_dir)
    return window.Bridge(Manager(places, ["--fake-engine"], FakeVectorEngine()), places)


class Handlers:
    """A pywebview window event: handlers are added with +=."""

    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self


class Window:
    """What the bridge uses of a pywebview window."""

    def __init__(self):
        self.calls = []
        self.events = SimpleNamespace(closed=threading.Event(), closing=Handlers())

    def restore(self):
        self.calls.append("restore")

    def show(self):
        self.calls.append("show")


def test_languages_cover_the_page_and_the_page_calls_only_the_bridge():
    english = json.loads((window.PAGE / "lang" / "en.json").read_text())
    for path in (window.PAGE / "lang").glob("*.json"):
        assert set(json.loads(path.read_text())) == set(english), path.name
    assert {path.stem for path in (window.PAGE / "lang").glob("*.json")} == set(window.LANGUAGES)
    page = window.page()
    used = set(re.findall(r"""data-t(?:-title|-ph)?="([\w.]+)\"""", page))
    used |= set(re.findall(r"""[TH]\('([\w.]+)'""", page))
    assert used and used <= set(english)
    # No server behind the window: everything goes through the bridge, and only to what it offers.
    assert "fetch(" not in page and "location.reload" not in page
    called = set(re.findall(r"bridge\(\)\.(\w+)\(", page))
    offered = {name for name in dir(window.Bridge) if not name.startswith("_")}
    assert called and called <= offered, called - offered


def test_language_and_theme_chosen_in_the_window(tmp_path):
    bridge = bridge_for(tmp_path)
    assert bridge.language() == "auto" and bridge.theme() == "auto"
    assert bridge.choose_language("ja") == {"ok": True, "data": "ja"}
    assert bridge.choose_theme("light") == {"ok": True, "data": "light"}
    # The next window keeps both; choosing one keeps the other.
    again = bridge_for(tmp_path)
    assert again.language() == "ja" and again.theme() == "light"
    assert json.loads((tmp_path / "window.json").read_text()) == {"language": "ja", "theme": "light"}
    assert bridge.choose_language("xx") == {"ok": False, "data": {"error": {"code": "invalid_language"}}}
    assert bridge.choose_theme("blue") == {"ok": False, "data": {"error": {"code": "invalid_theme"}}}
    assert bridge.choose_language("auto") == {"ok": True, "data": "auto"} and bridge.theme() == "light"
    # A value written by hand that the window does not know is the default.
    (tmp_path / "window.json").write_text(json.dumps({"language": "xx", "theme": "<b>"}))
    assert bridge.language() == "auto" and bridge.theme() == "auto"
    # The page is drawn in the chosen theme from the start (the page resolves auto with the system's).
    assert '<html lang="en" data-theme-setting="light" ' in window.page("light")
    assert bridge.texts("ja")["top.start"] == "起動" and bridge.texts("../ui/lang/en") == {}


def test_the_window_keeps_room_for_the_page():
    # The page knows its smallest size, and the window keeps that much room inside, but the window with its frame is
    # not larger than the work area: on a 1920x1080 screen at 150% the window fits and the page scrolls a little.
    assert 'style="--page-min-width: 1280px; --page-min-height: 720px"' in window.page()
    # Windows: the outer size in the window's pixels (the page at the scale, plus the frame).
    assert minsize.outer_minimum((1280, 720), 1, (16, 39), (1920, 1040)) == (1296, 759)
    assert minsize.outer_minimum((1280, 720), 1.5, (16, 39), (1920, 1032)) == (1920, 1032)
    # Linux: the inside in CSS pixels.
    assert minsize.inner_minimum((1280, 720), (0, 37), (1920, 1052)) == (1280, 720)
    assert minsize.inner_minimum((1280, 720), (0, 37), (1280, 720)) == (1280, 683)
    # The first window is not below its minimum (Windows takes the size with its frame).
    first = minsize.outer_minimum(minsize.PAGE_MIN, 1, (16, 39), (1920, 1040))
    assert all(m <= s for m, s in zip(first, window.SIZE))


def test_the_window_opens_where_it_was():
    left, right = (0, 0, 1920, 1040), (1920, 0, 2560, 1400)
    bounds = {"x": 2100, "y": 100, "width": 1400, "height": 900, "maximized": False}
    assert placement.fit(bounds, [left, right]) == bounds
    # Maximized: the bounds before it was maximized, so it comes back there when restored.
    assert placement.fit({**bounds, "maximized": True}, [left, right]) == {**bounds, "maximized": True}
    # The screen it was on was taken away: as the first time (the middle of the first screen).
    assert placement.fit(bounds, [left]) is None
    # Only the title bar's edge is left on a screen: too little to take with the mouse.
    assert placement.fit({**bounds, "x": 1840}, [left]) is None
    assert placement.fit({**bounds, "x": 1700, "y": 1030}, [left]) is None
    # A little off the edge (Windows puts a window's invisible border there) is still on the screen.
    assert placement.fit({**bounds, "x": -8, "y": -8}, [left]) == {**bounds, "x": -8, "y": -8, "height": 900}
    # The screen became smaller: the window fits its work area.
    assert placement.fit({**bounds, "width": 3000, "height": 2000}, [right]) == {**bounds, "width": 2560,
                                                                                 "height": 1400}
    # Wayland: no place, only the size (fitting the first screen) and maximized.
    assert placement.fit({**bounds, "x": None, "y": None, "width": 2500}, [left, right]) == {
        **bounds, "x": None, "y": None, "width": 1920}
    # Bounds written by hand that are not whole numbers are not used.
    for broken in ({}, [], {**bounds, "x": "1"}, {**bounds, "x": None}, {**bounds, "width": 0},
                   {**bounds, "maximized": 1}, {**bounds, "height": True}):
        assert placement.fit(broken, [left]) is None
    assert placement.fit(bounds, []) is None


def test_the_window_remembers_where_it_closed(tmp_path):
    bridge = bridge_for(tmp_path)
    assert bridge._bounds() is None
    bridge.choose_theme("light")
    bounds = {"x": 10, "y": 20, "width": 1400, "height": 900, "maximized": True}
    bridge._attach(Window(), SimpleNamespace(bounds=lambda: bounds))
    assert bridge._closing() is True
    # The next window opens there, and the other choices are kept.
    assert bridge_for(tmp_path)._bounds() == bounds and bridge.theme() == "light"
    # A window without a native one to read (another system) keeps nothing.
    (tmp_path / "window.json").unlink()
    other = bridge_for(tmp_path)
    other._attach(Window())
    assert other._closing() is True and not (tmp_path / "window.json").exists()


def test_the_bridge_passes_the_host_api_through(tmp_path, server):  # noqa: F811
    bridge = bridge_for(tmp_path)
    assert bridge.host("/manage/status") == {"ok": False, "status": 503, "data": {"error": {"code": "not_running"}}}
    assert bridge.status()["running"] is False
    process = server("--fake-engine")
    await_connection(tmp_path, process)
    status = bridge.host("/manage/status")
    assert status["ok"] is True and status["data"]["pid"] == process.pid
    assert bridge.host("/manage/environments/unknown/revoke", {}) == {
        "ok": False, "status": 404, "data": {"error": {"code": "not_found"}}}
    assert bridge.settings()["data"]["running"] is True
    assert bridge.host("/manage/stop", {})["data"] == {"stopping": True}
    assert process.wait(timeout=15) == 0


def test_a_second_gui_brings_the_open_window_forward(tmp_path, monkeypatch):
    places = Places.at(tmp_path)
    open_window = InstanceLock(places.runtime, "window.lock")
    assert open_window.acquire()
    monkeypatch.setattr(window, "load_webview", lambda: pytest.fail("a second window was made"))
    try:
        assert window.run(places, Manager(places, engine=FakeVectorEngine())) == 0
    finally:
        open_window.close()
    request = places.runtime / window.REQUEST
    assert window.take_request(request) is True and not request.exists() and window.take_request(request) is False

    bridge = bridge_for(tmp_path)
    shown = Window()
    bridge._attach(shown)
    # The second gui's request is answered while the window is open; the watch ends when it closes, so the program
    # ends with the window.
    request.write_text("")
    watch = threading.Thread(target=window.watch_requests, args=(bridge, request), daemon=True)
    watch.start()
    for _ in range(50):
        if shown.calls:
            break
        watch.join(0.1)
    assert shown.calls == ["restore", "show"] and not request.exists()
    shown.events.closed.set()
    watch.join(5)
    assert not watch.is_alive()


def test_the_window_stays_open_while_a_db_change_runs(tmp_path):
    bridge = bridge_for(tmp_path)
    shown = Window()
    bridge._attach(shown)
    assert shown.events.closing.handlers == [bridge._closing]
    assert bridge._closing() is True and bridge.status()["closeRefused"] is False

    def held(what):
        """A DB change that runs until released, as one started from the page does."""
        started, release = threading.Event(), threading.Event()
        work = threading.Thread(target=bridge._manager.local._run,
                                args=(what, lambda updater: started.set() or release.wait(5)), daemon=True)
        work.start()
        assert started.wait(5)
        return work, release

    work, release = held("update")
    # Closing now would stop it halfway: the window stays, and the page says why until it ends.
    assert bridge._closing() is False
    status = bridge.status()
    assert status["closeRefused"] is True and status["db"]["work"] == {"what": "update", "progress": None}
    assert bridge.switch_db(str(tmp_path)) == {"ok": False, "data": {"error": {"code": "update_running"}}}
    release.set()
    work.join(5)
    assert bridge.status()["closeRefused"] is False and bridge._closing() is True
    # A check of the index changes nothing: the window closes.
    work, release = held("check")
    assert bridge._closing() is True
    release.set()
    work.join(5)
