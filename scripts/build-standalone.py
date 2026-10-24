"""Build on the target OS after setup-python.sh --extra build --extra gui [--extra octantvector]. What goes into the
executable is in octantcompass.spec."""

import argparse
import importlib.util
import os
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--without-octantvector", action="store_true",
                        help="build a development executable with only --fake-engine support")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if importlib.util.find_spec("webview") is None:
        parser.error("install the gui extra before building (the window)")
    if not args.without_octantvector and importlib.util.find_spec("octantvector") is None:
        parser.error("install the octantvector extra before building")
    # The executable cannot ask git which commit it is, so record it now and bundle it next to the package
    # (shown in the window). It is written under build/ so a run from the source never reads it.
    sys.path.insert(0, str(root / "src"))
    from octantcompass.build import COMMIT_FILE, describe
    stamp = root / "build" / COMMIT_FILE.name
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(describe(root) or "", encoding="utf-8")
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", str(root / "dist"),
               "--workpath", str(root / "build"), str(root / "scripts" / "octantcompass.spec"),
               "--", "--commit", str(stamp)]
    if args.without_octantvector:
        command.append("--without-octantvector")
    environment = {**os.environ, "PYINSTALLER_CONFIG_DIR": str(root / "build" / "cache")}
    return subprocess.call(command, cwd=root, env=environment)


if __name__ == "__main__":
    raise SystemExit(main())
