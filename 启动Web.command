#!/bin/bash
cd "$(dirname "$0")" || exit 1
PY="${WEEKLY_PYTHON:-python3}"
if [ ! -d .venv ]; then "$PY" -m venv .venv || exit 1; fi
.venv/bin/python -m pip install -q -r requirements.txt || exit 1
open "http://127.0.0.1:8000" >/dev/null 2>&1 &
exec .venv/bin/python -m uvicorn app.web.app:app --host 127.0.0.1 --port 8000
