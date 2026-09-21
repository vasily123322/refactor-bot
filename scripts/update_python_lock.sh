#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3.12}"

"${PYTHON_BIN}" - <<'PY'
import sys

if sys.version_info[:2] != (3, 12):
    raise SystemExit(
        f"requirements.lock must be generated with Python 3.12; got "
        f"{sys.version_info.major}.{sys.version_info.minor}"
    )
PY

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT
VENV="$TMP_DIR/venv"

"${PYTHON_BIN}" -m venv "$VENV"
PYTHON="$VENV/bin/python"

"$PYTHON" -m pip install --upgrade pip
"$PYTHON" -m pip install --upgrade   -r "$ROOT/requirements.in"   -r "$ROOT/requirements-dev.in"

{
  printf '%s\n' '# Generated from requirements.in + requirements-dev.in on canonical Python 3.12.'
  printf '%s\n' '# Refresh intentionally with: bash scripts/update_python_lock.sh'
  printf '%s\n' '# Do not hand-edit package versions in this file.'
  "$PYTHON" -m pip freeze | LC_ALL=C sort -f
} > "$ROOT/requirements.lock"

echo "Updated $ROOT/requirements.lock"
