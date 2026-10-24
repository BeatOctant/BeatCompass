# The standalone executable, one file. Run by scripts/build-standalone.py on the target OS, which passes after `--`:
# --commit FILE (the commit, bundled next to the package) and --without-octantvector.
#
# On Linux the window (pywebview on GTK) uses the system's GTK 3 and WebKitGTK 4.1: WebKitGTK runs pages in its own
# programs installed with it, which only work with the libraries they were installed with. So the executable carries
# PyGObject, its libgirepository and the typelibs, and leaves out every library the system's WebKitGTK and GTK load,
# with GTK's data (themes, icons, schemas) and the runtime hooks that would point GTK at it. Libraries Python needs too
# (libstdc++, libffi, zlib, OpenSSL ...) come from the system as well, so one copy of each is loaded.

import argparse
import os
import sys
from pathlib import Path

from PyInstaller.depend.bindepend import get_imports, resolve_library_path
from PyInstaller.utils.hooks import copy_metadata

sys.path.insert(0, SPECPATH)
import third_party_notices

parser = argparse.ArgumentParser()
parser.add_argument("--commit", required=True)
parser.add_argument("--without-octantvector", action="store_true")
options = parser.parse_args()
root = Path(SPECPATH).parent
package = root / "src" / "octantcompass"

hidden = ["uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.auto", "uvicorn.lifespan.on"]
# OctantVector is found through its public runtime imports.
hidden += [] if options.without_octantvector else ["octantvector.runtime"]

a = Analysis(
    [str(root / "scripts" / "frozen-entry.py")],
    pathex=[str(root / "src")],
    # The window's page and the default settings are read from files next to the package; the package version
    # (octantcompass.__version__) from the distribution metadata.
    datas=[(str(package / "ui"), "octantcompass/ui"), (str(package / "defaults.json"), "octantcompass"),
           (options.commit, "octantcompass")] + copy_metadata("octantcompass"),
    hiddenimports=hidden,
    # readline (and rlcompleter, which loads it) is GNU readline, GPL-3.0: OctantCompass has no interactive prompt.
    excludes=["readline", "rlcompleter"] + (["octantvector"] if options.without_octantvector else []),
    hookspath=[str(root / "scripts" / "pyinstaller-hooks")],
    # GTK's themes, icons and translations are the system's.
    hooksconfig={"gi": {"icons": [], "themes": [], "languages": []}},
)

# What GTK loads by itself besides its libraries: the GIO modules and the image loaders the gi hooks collected.
GTK_MODULES = ("gio_modules/", "lib/gdk-pixbuf/")
# GTK's data, and the runtime hooks that point GTK, GIO and gdk-pixbuf at the copies in the executable.
GTK_DATA = ("share/", "gio_modules/", "lib/gdk-pixbuf/")
GTK_HOOKS = {"pyi_rth_gtk", "pyi_rth_gio", "pyi_rth_glib", "pyi_rth_gdkpixbuf"}
# Bundled even though they load the system's GLib: the bundled Python, and the libgirepository the bundled PyGObject
# was built with (newer systems have only libgirepository-2.0).
OWN = ("libpython", "libgirepository-1.0.")


def system_libraries(binaries):
    """The libraries the executable takes from the system: what the system's WebKitGTK and GTK load, and what loads
    any of that (OpenSSL's libssl with libcrypto, a typelib's library). Python's extension modules are never among
    them: one at the top level (cffi's _cffi_backend, which cryptography needs to check a DB's signature) loads libc
    like any library, and the system has no copy of it."""
    webkit = resolve_library_path("libwebkit2gtk-4.1.so.0")
    if webkit is None:
        raise SystemExit("building for Linux needs WebKitGTK 4.1 (libwebkit2gtk-4.1-0 and gir1.2-webkit2-4.1)")
    names, pending = set(), [webkit] + [source for name, source, _ in binaries if name.startswith(GTK_MODULES)]
    while pending:
        path = pending.pop()
        if Path(path).name not in names:
            names.add(Path(path).name)
            pending += [found for _, found in get_imports(path) if found]
    bundled = {name: source for name, source, kind in binaries
               if kind == "BINARY" and "/" not in name and not name.startswith(OWN)}
    added = True
    while added:
        loading = {name for name, source in bundled.items()
                   if name not in names and any(Path(found).name in names for _, found in get_imports(source) if found)}
        names |= loading
        added = bool(loading)
    return names


if sys.platform == "linux":
    if not any(name == "gi_typelibs/WebKit2-4.1.typelib" for name, _, _ in a.datas):
        raise SystemExit("building for Linux needs the WebKit2 4.1 typelib (gir1.2-webkit2-4.1)")
    system = system_libraries(a.binaries)
    a.binaries = [entry for entry in a.binaries
                  if not entry[0].startswith(GTK_MODULES) and not ("/" not in entry[0] and entry[0] in system)]
    a.datas = [entry for entry in a.datas if not entry[0].startswith(GTK_DATA)]
    a.scripts = [entry for entry in a.scripts if entry[0] not in GTK_HOOKS]

# The licences of what the executable carries, bundled at its top level and kept under build/ for the Release.
NOTICES = Path(workpath) / "THIRD-PARTY-NOTICES.txt"
third_party_notices.write(NOTICES, a.pure, a.binaries, a.datas)
a.datas += [("THIRD-PARTY-NOTICES.txt", str(NOTICES), "DATA"), ("LICENSE", str(root / "LICENSE"), "DATA")]

pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="octantcompass", console=True, upx=False)
