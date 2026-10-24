"""The window's smallest size (2026-10-19: 720p, the same as OctantPath Desktop).

The page always has PAGE_MIN (CSS pixels) of room, except on a screen too small for it: there the window fits the
screen's work area (the screen without the taskbar or panels) and the page scrolls. pywebview takes a minimum only when
it makes the window, in units that differ by system and leave out the window's frame (title bar and borders), so the
minimum is set on the window itself once it shows, and again whenever it moves (another screen has another work area).
"""

import math
import sys

PAGE_MIN = (1280, 720)


def outer_minimum(page, scale, frame, work):
    """Windows: the window's minimum as its outer size, in the window's pixels: the page's minimum at the scale
    (window pixels per CSS pixel) plus the frame, but not larger than the work area."""
    return tuple(min(math.ceil(p * scale) + f, w) for p, f, w in zip(page, frame, work))


def inner_minimum(page, frame, work):
    """Linux: the minimum of the window's inside, in the page's CSS pixels: the page's minimum, but not so large that
    the window with its frame is larger than the work area."""
    return tuple(min(p, w - f) for p, f, w in zip(page, frame, work))


def keep(window):
    """Keep the minimum of a pywebview window from when it shows (its native window: a Gtk.Window on Linux, a WinForms
    Form on Windows)."""
    if sys.platform == "linux":
        keeper = GtkMinimum(window)
    elif sys.platform == "win32":
        keeper = WinFormsMinimum(window)
    else:
        return
    window.events.shown += keeper.update
    window.events.moved += keeper.update


class GtkMinimum:
    """Linux: GTK takes the minimum as the size of the window's inside, in logical pixels (the page's CSS pixels); the
    frame only counts toward fitting the work area. pywebview's events come on threads of their own, so the work is
    handed to GTK's."""

    def __init__(self, window):
        self._window = window
        self._applied = None

    def update(self):
        from gi.repository import GLib
        GLib.idle_add(self._apply)

    def _apply(self):
        from gi.repository import Gdk
        native = self._window.native
        gdk_window = native.get_window()
        if gdk_window is None or not native.get_mapped():
            return False
        outer = gdk_window.get_frame_extents()
        width, height = native.get_size()
        frame = (outer.width - width, outer.height - height)
        work = Gdk.Display.get_default().get_monitor_at_window(gdk_window).get_workarea()
        minimum = inner_minimum(PAGE_MIN, frame, (work.width, work.height))
        # Setting the hints moves the window's frame again (and pywebview reports a move), so only when they change.
        if minimum != self._applied:
            self._applied = minimum
            geometry = Gdk.Geometry()
            geometry.min_width, geometry.min_height = minimum
            native.set_geometry_hints(None, geometry, Gdk.WindowHints.MIN_SIZE)
        return False


class WinFormsMinimum:
    """Windows: WinForms takes the minimum as the window's outer size in its pixels, and the page is drawn at the
    system's DPI (pywebview makes the process aware of that one DPI, on every screen). The work is handed to the
    window's thread. A minimized window has no size of its own to go by."""

    def __init__(self, window):
        self._window = window

    def update(self):
        from System import Action
        self._window.native.Invoke(Action(self._apply))

    def _apply(self):
        import ctypes

        from System.Drawing import Size
        from System.Windows.Forms import FormWindowState, Screen
        form = self._window.native
        if form.WindowState == FormWindowState.Minimized:
            return
        scale = ctypes.windll.user32.GetDpiForWindow(form.Handle.ToInt32()) / 96
        frame = (form.Size.Width - form.ClientSize.Width, form.Size.Height - form.ClientSize.Height)
        work = Screen.FromControl(form).WorkingArea
        width, height = outer_minimum(PAGE_MIN, scale, frame, (work.Width, work.Height))
        if (form.MinimumSize.Width, form.MinimumSize.Height) != (width, height):
            form.MinimumSize = Size(width, height)
