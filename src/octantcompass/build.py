"""Which build is running: the commit it was made from, shown next to the name in the window (2026-10-13)."""

import subprocess
import sys
from functools import lru_cache
from pathlib import Path

# build-standalone.py writes the commit here before PyInstaller bundles the package.
COMMIT_FILE = Path(__file__).with_name("_commit.txt")


@lru_cache(maxsize=1)
def commit():
    """The short commit hash, with "+" when run from a checkout that has uncommitted changes; None when unknown."""
    if COMMIT_FILE.is_file():
        return COMMIT_FILE.read_text(encoding="utf-8").strip() or None
    if getattr(sys, "frozen", False):
        return None
    return describe(Path(__file__).resolve().parent)


def describe(folder):
    """The commit of the git checkout holding the folder, or None outside a checkout or without git."""
    try:
        run = lambda *args: subprocess.run(["git", "-C", str(folder), *args], capture_output=True, text=True,
                                           timeout=5, check=True).stdout.strip()
        head = run("rev-parse", "--short", "HEAD")
        return head + ("+" if run("status", "--porcelain", "--untracked-files=no") else "")
    except (OSError, subprocess.SubprocessError):
        return None
