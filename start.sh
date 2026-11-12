#!/usr/bin/env bash
# Linux / macOS equivalent of START.bat: install deps, pick the SDK, launch the app.
set -u

cd "$(dirname "$0")" || exit 1

PY=""
if [ -x ".venv/bin/python" ]; then
    PY=".venv/bin/python"
    echo "[env] Using virtual environment: .venv"
elif command -v python3 >/dev/null 2>&1; then
    PY="python3"
elif command -v python >/dev/null 2>&1; then
    PY="python"
else
    echo "[ERROR] Python 3 was not found. Install it (https://www.python.org/downloads/) and re-run."
    exit 1
fi

echo "[env] Interpreter: $PY"
"$PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)' || {
    echo "[ERROR] Python 3.8 or newer is required."
    exit 1
}

exec "$PY" bootstrap.py "$@"
