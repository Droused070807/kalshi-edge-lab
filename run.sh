#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
. .venv/bin/activate
pip install -q --disable-pip-version-check -r requirements.txt
if [ ! -f .env ]; then cp .env.example .env; echo "Created .env - add your ODDS_API_KEY, then run again."; exit 0; fi
python -m app
