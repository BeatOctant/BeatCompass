"""Launcher entries: one that opens the window (the Linux applications menu or the Windows Start menu),
and one that starts OctantCompass at login (the setting start_at_login, off by default; OctantSpecs
octantcompass/law.md 3.1)."""

import os
import subprocess
import sys
from pathlib import Path

from .places import Places


def command(places, verb="gui"):
    """How this OctantCompass is started again: the standalone executable, or this Python with -m octantcompass.
    places: a Places, or a folder (--data-dir); a folder is passed on, the platform default is not."""
    parts = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, "-m", "octantcompass"]
    parts.append(verb)
    folder = Places.of(places).folder
    if folder is not None:
        parts += ["--data-dir", str(Path(folder).resolve())]
    return parts


def detached():
    """Popen arguments for a OctantCompass started from another one (the window starting `serve`, the host opening the
    window): it outlives the one that started it, and on Windows no console window comes up with it."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}


def make_shortcut(places):
    """-> the path of the entry written."""
    parts = command(places)
    if sys.platform == "win32":
        return windows_shortcut(parts)
    if sys.platform == "darwin":
        raise RuntimeError("shortcuts are not made on macOS yet; run `octantcompass gui`")
    return linux_entry(parts)


def linux_entry(parts):
    applications = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "applications"
    applications.mkdir(parents=True, exist_ok=True)
    path = applications / "octantcompass.desktop"
    path.write_text("[Desktop Entry]\nType=Application\nName=OctantCompass\n"
                    "Comment=Open the OctantCompass window\n"
                    f"Exec={' '.join(map(exec_quote, parts))}\nTerminal=false\nCategories=Game;Utility;\n"
                    # The window names itself octantcompass (window.py), so the desktop shows it under this entry
                    "StartupWMClass=octantcompass\n")
    return path


UNIT = "octantcompass.service"


def login_entry_path():
    """Where the entry that starts OctantCompass at login lives on this OS: on Linux the systemd user unit, so
    `systemctl --user stop|status octantcompass` works on it."""
    if sys.platform == "win32":
        return Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup/OctantCompass.lnk"
    if sys.platform == "darwin":
        raise RuntimeError("starting at login is not made on macOS yet")
    return config_home() / "systemd/user" / UNIT


def autostart_path():
    """XDG Autostart: the Linux entry where there is no systemd user manager."""
    return config_home() / "autostart/octantcompass.desktop"


def config_home():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def systemctl(*args):
    """systemctl --user; raises RuntimeError when there is no systemd user manager to ask."""
    try:
        subprocess.run(["systemctl", "--user", *args], check=True, capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"systemctl --user {' '.join(args)} failed") from exc


def set_start_at_login(places, on):
    """Write (on) or remove (off) the login entry. At login OctantCompass starts with `serve`: no window, and with
    idle_minutes set it holds no DB until a paired environment asks (contract 6 (client to OctantCompass) 2.0).
    -> the entry's path. Raises RuntimeError when it cannot be written."""
    path = login_entry_path()
    if sys.platform == "win32":
        if not on:
            path.unlink(missing_ok=True)
            return path
        return windows_shortcut(command(places, "serve"), path, "Start OctantCompass at login")
    if not on:
        if path.exists():
            try:
                systemctl("disable", UNIT)
            except RuntimeError:
                pass  # no user manager now: the link in default.target.wants goes with the unit's folder below
            (path.parent / "default.target.wants" / UNIT).unlink(missing_ok=True)
            path.unlink()
            try:
                systemctl("daemon-reload")
            except RuntimeError:
                pass
        autostart_path().unlink(missing_ok=True)
        return path
    try:
        return systemd_unit(path, command(places, "serve"))
    except RuntimeError:
        return autostart_entry(command(places, "serve"))


def systemd_unit(path, parts):
    """The user unit, enabled for default.target (the user's session). Written again only when it changed, so the
    refresh at every start costs nothing."""
    text = ("[Unit]\nDescription=OctantCompass (started at login; set in its window)\n\n"
            f"[Service]\nExecStart={' '.join(map(unit_quote, parts))}\n\n[Install]\nWantedBy=default.target\n")
    enabled = path.parent / "default.target.wants" / UNIT
    if path.is_file() and path.read_text() == text and enabled.exists():
        return path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    except OSError as exc:
        raise RuntimeError("the login entry could not be written") from exc
    try:
        systemctl("daemon-reload")
        systemctl("enable", UNIT)
    except RuntimeError:
        path.unlink(missing_ok=True)
        raise
    autostart_path().unlink(missing_ok=True)  # one entry only: the unit replaces an older autostart entry
    return path


def autostart_entry(parts):
    path = autostart_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[Desktop Entry]\nType=Application\nName=OctantCompass\n"
                        "Comment=Start OctantCompass at login (set in its window)\n"
                        f"Exec={' '.join(map(exec_quote, parts))}\nTerminal=false\nNoDisplay=true\n"
                        "X-GNOME-Autostart-enabled=true\n")
    except OSError as exc:
        raise RuntimeError("the login entry could not be written") from exc
    return path


def unit_quote(arg):
    """systemd command-line quoting: double quotes with \\ and " escaped; % and $ doubled so that no specifier or
    variable is expanded."""
    arg = arg.replace("%", "%%").replace("$", "$$")
    if arg and not any(c in arg for c in ' \t\n"\'\\;'):
        return arg
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"'


def exec_quote(arg):
    """Desktop Entry Exec quoting: double quotes, with \\ " ` $ escaped by a backslash; % doubled. The string
    escape of the file then doubles every backslash (the spec's four backslashes for a literal one)."""
    arg = arg.replace("%", "%%")
    if arg and not any(c in arg for c in ' \t\n"\'\\><~|&;$*?#()`'):
        return arg
    quoted = '"' + "".join("\\" + c if c in '"`$\\' else c for c in arg) + '"'
    return quoted.replace("\\", "\\\\")


def windows_shortcut(parts, path=None, description="Open the OctantCompass window"):
    # Provisional (not run on Windows yet): a .lnk through the WScript.Shell COM object that PowerShell ships with.
    # Values go through the environment so no quoting of paths is needed in the script. WindowStyle 7 starts the
    # console minimized.
    path = path or Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/OctantCompass.lnk"
    script = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:BC_LINK); "
              "$s.TargetPath = $env:BC_TARGET; $s.Arguments = $env:BC_ARGS; "
              "$s.Description = $env:BC_DESCRIPTION; $s.WindowStyle = 7; $s.Save()")
    environment = {**os.environ, "BC_LINK": str(path), "BC_TARGET": parts[0],
                   "BC_ARGS": subprocess.list2cmdline(parts[1:]), "BC_DESCRIPTION": description}
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                       env=environment, check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("the Start menu shortcut could not be written") from exc
    return path
