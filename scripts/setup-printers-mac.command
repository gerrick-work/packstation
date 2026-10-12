#!/bin/bash
# Connects the two AiYin printers to Pack Station, step by step.
cd "$(dirname "$0")/.." || exit 1
if [ ! -d .venv ]; then
  echo "First run: installing (one minute)..."
  python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt || exit 1
fi
exec .venv/bin/python scripts/setup_printers.py
