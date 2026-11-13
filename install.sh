#!/usr/bin/env bash
# Install: JS dependencies, runtime bundle and the extension symlink.
set -euo pipefail
cd "$(dirname "$0")"
UUID=uebersicht@local
EXT_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/gnome-shell/extensions"
WIDGETS="${UBERSICHT_WIDGETS_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/uebersicht/widgets}"

npm install --no-audit --no-fund
npm run build
mkdir -p "$EXT_DIR" "$WIDGETS"
ln -sfn "$PWD/extension" "$EXT_DIR/$UUID"
[ -e "$WIDGETS/hello.jsx" ] || cp examples/hello.jsx "$WIDGETS/"

echo
echo "Installed $UUID → $EXT_DIR/$UUID"
echo "Widgets: $WIDGETS"
echo "On Wayland, GNOME picks up a new extension after you log out and back in, then run:"
echo "  gnome-extensions enable $UUID"
