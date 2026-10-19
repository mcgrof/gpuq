#!/bin/sh
# SPDX-License-Identifier: MIT
set -e
uuid=gpuq@mcgrof.github.io
source=$(dirname "$0")
target="$HOME/.local/share/gnome-shell/extensions/$uuid"
glib-compile-schemas --strict --targetdir="$source/schemas" "$source/schemas"
rm -rf "$target"
mkdir -p "$target"
cp -r "$source/metadata.json" "$source/extension.js" "$source/prefs.js" \
	"$source/stylesheet.css" "$source/schemas" "$target/"
echo "Installed to $target"
echo "Enable with: gnome-extensions enable $uuid"
echo "A first install may need a log out and back in on Wayland."
