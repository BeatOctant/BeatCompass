"""Where the window opens: where it was when it last closed (2026-10-21), its place and size and whether it was
maximized, kept in window.json as "bounds". Without one, or when it would open off the screens (a screen was taken
away), the window opens as it would the first time: in the middle of the first screen, at its first size.

The place and size are the native window's own, in its units: on Windows the outer bounds in physical pixels, on
Linux the position of the frame and the size of the inside in logical pixels. A maximized window keeps the bounds it
had before it was maximized, so it comes back there when it is restored. On Linux under Wayland a program cannot see
or choose where its window is: there only the size and maximized are kept, and the desktop places the window.
"""

import sys

# How much of the window's top (where the title bar is) must be on a screen's work area for it to open there: enough
# to take it with the mouse.
TOP = 40
SEEN = (100, TOP // 2)


def fit(bounds, work_areas):
    """The bounds to open with, from those saved: {"x", "y", "width", "height", "maximized"} (x and y None when the
    place is not known), and the work areas of the screens as (x, y, width, height). The window is made no larger
    than the work area it opens on. -> the bounds, or None to open as the first time."""
    try:
        x, y = bounds["x"], bounds["y"]
        width, height, maximized = bounds["width"], bounds["height"], bounds["maximized"]
    except (KeyError, TypeError):
        return None
    place = (x, y) if x is not None or y is not None else ()
    if (not all(type(n) is int for n in (*place, width, height)) or type(maximized) is not bool
            or width <= 0 or height <= 0 or not work_areas):
        return None
    if place:
        area = next((a for a in work_areas if shows_top((x, y, width), a)), None)
        if area is None:
            return None
    else:
        area = work_areas[0]
    return {"x": x, "y": y, "width": min(width, area[2]), "height": min(height, area[3]), "maximized": maximized}


def shows_top(window, area):
    """Whether enough of the window's top (x, y, width) is on the work area (x, y, width, height)."""
    x, y, width = window
    ax, ay, aw, ah = area
    across = min(x + width, ax + aw) - max(x, ax)
    down = min(y + TOP, ay + ah) - max(y, ay)
    return across >= SEEN[0] and down >= SEEN[1]


def keep(window, saved):
    """Open a pywebview window where it was (saved: the bounds from window.json, or None). Set up before the window
    shows. -> what reads the window's bounds when it closes (bounds()), or None on a system without one."""
    if sys.platform == "linux":
        keeper = GtkPlacement(window)
    elif sys.platform == "win32":
        keeper = WinFormsPlacement(window)
    else:
        return None
    window.events.before_show += lambda: keeper.open(fit(saved, keeper.work_areas()) if saved else None)
    return keeper


class GtkPlacement:
    """Linux: the native window is a Gtk.Window, set up and read on GTK's thread (before_show and closing come
    there). The bounds before maximizing are followed as the window changes (GTK has no restored bounds of its own)."""

    def __init__(self, window):
        self._window = window
        self._maximized = False
        self._normal = None

    def _wayland(self):
        from gi.repository import Gdk
        return type(Gdk.Display.get_default()).__name__.startswith("Wayland")

    def work_areas(self):
        from gi.repository import Gdk
        display = Gdk.Display.get_default()
        areas = [display.get_monitor(i).get_workarea() for i in range(display.get_n_monitors())]
        return [(a.x, a.y, a.width, a.height) for a in areas]

    def open(self, bounds):
        native = self._window.native
        native.connect("window-state-event", self._state)
        native.connect("configure-event", self._changed)
        if bounds is None:
            return
        # Opened maximized, the window has no other bounds until it is restored.
        self._normal = (bounds["x"], bounds["y"], bounds["width"], bounds["height"])
        if bounds["x"] is not None and not self._wayland():
            native.move(bounds["x"], bounds["y"])
        native.resize(bounds["width"], bounds["height"])
        if bounds["maximized"]:
            native.maximize()

    def _state(self, native, event):
        from gi.repository import Gdk
        self._maximized = bool(event.new_window_state & Gdk.WindowState.MAXIMIZED)
        return False

    def _changed(self, native, event):
        if not self._maximized:
            x, y = (None, None) if self._wayland() else native.get_position()
            self._normal = (x, y, *native.get_size())
        return False

    def bounds(self):
        native = self._window.native
        if self._normal is None:
            x, y = (None, None) if self._wayland() else native.get_position()
            self._normal = (x, y, *native.get_size())
        x, y, width, height = self._normal
        return {"x": x, "y": y, "width": width, "height": height, "maximized": self._maximized}


class WinFormsPlacement:
    """Windows: the native window is a WinForms Form, set up and read on its thread (before_show and closing come
    there). The Form keeps its bounds from before it was maximized or minimized (RestoreBounds)."""

    def __init__(self, window):
        self._window = window

    def work_areas(self):
        from System.Windows.Forms import Screen
        return [(s.WorkingArea.X, s.WorkingArea.Y, s.WorkingArea.Width, s.WorkingArea.Height) for s in Screen.AllScreens]

    def open(self, bounds):
        if bounds is None:
            return
        from System.Drawing import Point, Size
        from System.Windows.Forms import FormStartPosition, FormWindowState
        form = self._window.native
        if bounds["x"] is not None:
            form.StartPosition = FormStartPosition.Manual
            form.Location = Point(bounds["x"], bounds["y"])
        form.Size = Size(bounds["width"], bounds["height"])
        if bounds["maximized"]:
            form.WindowState = FormWindowState.Maximized

    def bounds(self):
        from System.Windows.Forms import FormWindowState
        form = self._window.native
        shown = form.Bounds if form.WindowState == FormWindowState.Normal else form.RestoreBounds
        return {"x": shown.X, "y": shown.Y, "width": shown.Width, "height": shown.Height,
                "maximized": form.WindowState == FormWindowState.Maximized}
