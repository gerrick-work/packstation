#!/bin/bash
# Double-click once. Installs Pack Station, puts it in your Dock, and starts it every time you log in.
cd "$(dirname "$0")" || exit 1
DIR="$PWD"
APP="$HOME/Applications/Pack Station.app"
echo ""
echo "=== Installing Pack Station (about 1 minute) ==="

if ! /usr/bin/python3 -c 'import sys; assert sys.version_info >= (3, 9)' >/dev/null 2>&1; then
  echo ""
  echo "Your Mac needs Apple's free developer tools first. A window will pop up: click Install."
  echo "When it finishes, double-click 'Install Pack Station' again."
  xcode-select --install >/dev/null 2>&1
  read -r -p "Press Enter to close..."
  exit 1
fi

echo "1/4 Installing the Pack Station software..."
[ -d .venv ] || /usr/bin/python3 -m venv .venv || exit 1
.venv/bin/pip install -q --upgrade pip >/dev/null 2>&1
.venv/bin/pip install -q -r requirements.txt || { echo "Install failed. Send Claude a screenshot of this window."; read -r; exit 1; }
[ -f config.toml ] || cp mac/config.gerrick.toml config.toml
mkdir -p data

echo "2/4 Making the Pack Station app..."
pkill -f "m packstation" >/dev/null 2>&1; lsof -ti tcp:8765 | xargs kill 2>/dev/null; sleep 2
mkdir -p "$HOME/Applications"
rm -rf "$APP"
sed "s|__INSTALL_DIR__|$DIR|" mac/launcher.applescript > data/launcher.applescript
osacompile -o "$APP" data/launcher.applescript || exit 1
/usr/bin/plutil -insert NSBluetoothAlwaysUsageDescription -string "Pack Station prints waybills and labels on your AiYin printers over Bluetooth." "$APP/Contents/Info.plist" 2>/dev/null
/usr/bin/plutil -replace CFBundleIdentifier -string "com.atlasdsf.packstation" "$APP/Contents/Info.plist"
if [ -f mac/icon.icns ]; then
  cp mac/icon.icns "$APP/Contents/Resources/applet.icns"
  /usr/bin/plutil -remove CFBundleIconName "$APP/Contents/Info.plist" 2>/dev/null   # use applet.icns, not the default in Assets.car
  rm -f "$APP/Contents/Resources/Assets.car"
fi
codesign --force --deep -s - "$APP" >/dev/null 2>&1

echo "3/4 Starting it automatically when you log in, and adding it to your Dock..."
osascript -e 'tell application "System Events" to delete (every login item whose name is "Pack Station")' >/dev/null 2>&1
osascript -e "tell application \"System Events\" to make login item at end with properties {path:\"$APP\", hidden:false}" >/dev/null 2>&1
if ! defaults read com.apple.dock persistent-apps 2>/dev/null | grep -q "Pack Station.app"; then
  defaults write com.apple.dock persistent-apps -array-add "<dict><key>tile-data</key><dict><key>file-data</key><dict><key>_CFURLString</key><string>file://${APP// /%20}/</string><key>_CFURLStringType</key><integer>15</integer></dict></dict></dict>"
  killall Dock
fi

echo "4/4 Opening Pack Station..."
open "$APP"
echo ""
echo "=== Done! ==="
echo "Pack Station is in your Dock and opens by itself whenever you log in."
echo "If the Mac asks to let Pack Station use Bluetooth, click Allow."
echo "On your phone (same Wi-Fi), open:  http://$(scutil --get LocalHostName 2>/dev/null).local:8765"
echo ""

