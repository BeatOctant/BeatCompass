#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"
if [ ! -x .venv/bin/python ]; then
  uv venv --cache-dir .uv-cache --python python3 --no-python-downloads .venv
fi
uv sync --frozen --cache-dir .uv-cache --python .venv/bin/python --no-python-downloads --extra test "$@"
