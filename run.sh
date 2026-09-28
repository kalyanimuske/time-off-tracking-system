#!/usr/bin/env bash
# One-command start for macOS / Linux: creates a virtualenv, installs, runs.
set -e
cd "$(dirname "$0")"

PYTHON=${PYTHON:-python3}
if [ ! -d .venv ]; then
  echo "Creating virtual environment..."
  "$PYTHON" -m venv .venv
fi
source .venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt

echo ""
echo "  Time Off is running at  http://localhost:8000"
echo "  API docs:               http://localhost:8000/docs"
echo ""
exec uvicorn app.main:app --reload --port 8000
