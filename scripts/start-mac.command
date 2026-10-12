#!/bin/bash
# Double-click in Finder to start Pack Station. First run sets everything up.
cd "$(dirname "$0")/.." || exit 1
if [ ! -d .venv ]; then
  echo "First run: installing (one minute)..."
  python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt || exit 1
fi
[ -f config.toml ] || cp config.example.toml config.toml
exec .venv/bin/python -m packstation
