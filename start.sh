#!/usr/bin/env bash
# One-command start: create the virtualenv if needed, install dependencies, run checks, launch API + UI.
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
if [ ! -d .venv ]; then
  echo "Creating virtual environment ..."
  "$PYTHON" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt
[ -f .env ] || cp .env.example .env
python verify_setup.py
exec python run_local.py
