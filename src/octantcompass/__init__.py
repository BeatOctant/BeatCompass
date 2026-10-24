from importlib import metadata

# The version is written once, in pyproject.toml; the executable carries the metadata (build-standalone.py).
try:
    __version__ = metadata.version("octantcompass")
except metadata.PackageNotFoundError:
    __version__ = "unknown"
