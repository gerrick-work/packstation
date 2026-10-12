#!/bin/bash
# One-time updater: installs this downloaded Pack Station into ~/Applications/PackStation,
# keeping config.toml, data/ (history, product list, log) and the Python setup, then restarts it.
# After this, future updates are done from Settings > Install an update.
set -e
SRC="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$HOME/Applications/PackStation"
APP="$HOME/Applications/Pack Station.app"

echo "Updating Pack Station in $DEST ..."
if [ ! -d "$DEST" ]; then
  echo "Pack Station isn't installed at $DEST. Nothing changed."; read -r -p "Press Enter to close." _; exit 1
fi
if [ "$SRC" = "$DEST" ]; then
  echo "Run this from the downloaded folder, not the installed one."; read -r -p "Press Enter to close." _; exit 1
fi

mkdir -p "$DEST/data"
# Keep the product list: move it into data/ before the new version replaces labels/products.csv
if [ -f "$DEST/labels/products.csv" ] && [ ! -f "$DEST/data/products.csv" ]; then
  cp "$DEST/labels/products.csv" "$DEST/data/products.csv"
fi
cp "$DEST/config.toml" "$DEST/data/config.toml.before-update" 2>/dev/null || true

(cd "$SRC" && tar -cf - --exclude './config.toml' --exclude './data' --exclude './.venv' --exclude './.git' .) | (cd "$DEST" && tar -xf -)

PY="$DEST/.venv/bin/python"
[ -x "$PY" ] || PY="python3"
echo "Installing new parts (about a minute)..."
if ! "$PY" -m pip install -q -r "$DEST/requirements.txt"; then
  echo ""; echo "Couldn't install the new parts. Check the internet connection and run this again,"
  echo "or send Claude a screenshot of this window."; read -r -p "Press Enter to close." _; exit 1
fi

echo "Restarting Pack Station..."
lsof -ti tcp:8765 | xargs -r kill 2>/dev/null || true
sleep 2
if [ -d "$APP" ]; then open "$APP"; fi
echo ""
echo "Done. Pack Station is restarting. You can close this window."
