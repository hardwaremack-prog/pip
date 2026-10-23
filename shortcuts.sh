#!/usr/bin/env bash
# Creates the "Pip" and "Update Pip" icons on the desktop and in the menu.
HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
DESK="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
APPS="$HOME/.local/share/applications"
mkdir -p "$DESK" "$APPS"

entry() { # file name comment exec terminal
  cat <<EOF
[Desktop Entry]
Type=Application
Name=$2
Comment=$3
Exec=$4
Path=$HERE
Icon=$HERE/web/icon.png
Terminal=$5
Categories=Utility;
EOF
}

entry pip "Pip" "Offline voice chat" "$HERE/pip.sh" false > "$APPS/pip-voice.desktop"
entry upd "Update Pip" "Get the newest version of Pip" "$HERE/update.sh --pause" true > "$APPS/pip-update.desktop"
cp "$APPS/pip-voice.desktop" "$DESK/Pip.desktop"
cp "$APPS/pip-update.desktop" "$DESK/Update Pip.desktop"
for f in "$DESK/Pip.desktop" "$DESK/Update Pip.desktop"; do
  chmod +x "$f"
  gio set "$f" metadata::trusted true 2>/dev/null || true
done
echo "Shortcuts ready."
