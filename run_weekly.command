#!/bin/bash
cd "$(dirname "$0")" || exit 1
PY="${WEEKLY_PYTHON:-python3}"
exec "$PY" weekly.py "$@"
