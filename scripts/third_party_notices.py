"""The third-party notices of the standalone executable, one text file (octantcompass.spec bundles it and build.yml
puts it next to the executable on the Release). It is written from what the executable carries: the Python
distributions whose modules or files are bundled, with the licence files they install; Python itself; and the shared
libraries that belong to no distribution, with their Debian copyright file when the build OS has one."""

import importlib.metadata
import subprocess
import sys
from pathlib import Path

LICENCE_NAMES = ("LICENSE", "LICENCE", "COPYING", "NOTICE", "AUTHORS", "COPYRIGHT")
RULE = "=" * 78


def top_level(entries):
    """-> the first path segment or dotted name of each (name, source, kind) entry PyInstaller collects."""
    names = set()
    for name, _, _ in entries:
        names.add(name.replace("\\", "/").split("/")[0].split(".")[0])
    return names


def licence_files(dist):
    """-> (relative name, text) of the licence files a distribution installed."""
    found = []
    for file in dist.files or ():
        parts = file.parts
        if ".dist-info" not in str(file):
            continue
        if "licenses" in parts or parts[-1].upper().startswith(LICENCE_NAMES):
            path = Path(dist.locate_file(file))
            if path.is_file():
                found.append((str(file), path.read_text(encoding="utf-8", errors="replace")))
    return sorted(found)


def licence_name(dist):
    meta = dist.metadata
    if meta.get("License-Expression"):
        return meta["License-Expression"]
    classifiers = [c.split(" :: ")[-1] for c in meta.get_all("Classifier") or () if c.startswith("License ::")]
    if classifiers:
        return ", ".join(classifiers)
    short = (meta.get("License") or "").strip()
    return short if short and "\n" not in short and len(short) < 80 else "see the licence files"


def distributions(names):
    """-> the distributions that provide any of the top-level `names`, by name."""
    provides = importlib.metadata.packages_distributions()
    wanted = {dist for name in names for dist in provides.get(name, ())}
    found = {}
    for dist in importlib.metadata.distributions():
        name = dist.metadata["Name"]
        if name in wanted and name not in found:
            found[name] = dist
    return [found[name] for name in sorted(found, key=str.lower)]


def python_licence():
    for path in (Path(sys.base_prefix) / "LICENSE.txt",
                 Path(sys.base_prefix) / "lib" / f"python{sys.version_info[0]}.{sys.version_info[1]}" / "LICENSE.txt"):
        if path.is_file():
            return path.read_text(encoding="utf-8", errors="replace")
    return None


def debian_copyright(source):
    """-> (package, copyright text) of the Debian package that installed `source`, or None."""
    try:
        owner = subprocess.run(["dpkg", "-S", str(Path(source).resolve())], capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    package = owner.stdout.split(":")[0].strip()
    copyright = Path("/usr/share/doc") / package / "copyright"
    if not package or not copyright.is_file():
        return None
    return package, copyright.read_text(encoding="utf-8", errors="replace")


def write(path, pure, binaries, datas):
    """Write the notices for what an Analysis collected (its pure, binaries and datas) to `path`."""
    names = top_level(pure) | top_level(binaries) | top_level(datas)
    dists = distributions(names)
    owned = set()
    for dist in dists:
        for file in dist.files or ():
            owned.add(Path(file).name)
    out = ["OctantCompass third-party notices", "",
           "The OctantCompass executable bundles the software below. Each part is under its own licence, reproduced",
           "here as that licence asks. OctantCompass itself is under the MIT License (LICENSE).", ""]
    out += [RULE, f"Python {sys.version.split()[0]}", "https://www.python.org/", RULE]
    text = python_licence()
    out += [text if text else "Python Software Foundation License Version 2 (the licence file was not found at build)",
            ""]
    for dist in dists:
        meta = dist.metadata
        url = meta.get("Home-page") or next((u.split(", ", 1)[-1] for u in meta.get_all("Project-URL") or ()), "")
        out += [RULE, f"{meta['Name']} {dist.version}  ({licence_name(dist)})"] + ([url] if url else []) + [RULE]
        files = licence_files(dist)
        if not files:
            out += ["(the distribution installed no licence file; licence as above)", ""]
        for name, body in files:
            out += [f"--- {name}", body.rstrip(), ""]
    others = sorted({(name, source) for name, source, kind in binaries
                     if kind == "BINARY" and "/" not in name.replace("\\", "/") and name not in owned
                     and not name.lower().startswith(("libpython", "python3"))})
    if others:
        out += [RULE, "Shared libraries from the build system", RULE]
        seen = set()
        for name, source in others:
            found = debian_copyright(source) if sys.platform == "linux" else None
            if found is None:
                out.append(f"{name}: from the build system (on Windows, the Python or Microsoft Visual C++ "
                           "runtime, whose terms are in the Python licence above and Microsoft's redistribution terms)")
            elif found[0] in seen:
                out.append(f"{name}: Debian package {found[0]} (above)")
            else:
                seen.add(found[0])
                out += [f"{name}: Debian package {found[0]}", f"--- /usr/share/doc/{found[0]}/copyright",
                        found[1].rstrip(), ""]
        out.append("")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("\n".join(out) + "\n", encoding="utf-8")
    return [dist.metadata["Name"] for dist in dists]
