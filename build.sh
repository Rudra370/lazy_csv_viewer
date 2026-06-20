#!/usr/bin/env bash
#
# Build a standalone macOS .app bundle for the Lazy CSV Viewer.
#
#   ./build.sh
#
# Requires PyInstaller (pip install pyinstaller). On macOS it also uses the
# built-in iconutil/sips to generate an app icon; without them it builds
# without a custom icon.
set -euo pipefail
cd "$(dirname "$0")"

APP_NAME="Lazy CSV Viewer"
PY="${PYTHON:-python3}"

# 1. Generate the icon (pure-stdlib; no extra deps).
"$PY" scripts/make_icon.py

ICON_ARGS=()
if command -v iconutil >/dev/null 2>&1 && command -v sips >/dev/null 2>&1; then
    rm -rf build/icon.iconset
    mkdir -p build/icon.iconset
    for size in 16 32 128 256 512; do
        sips -z "$size" "$size" assets/icon_1024.png \
            --out "build/icon.iconset/icon_${size}x${size}.png" >/dev/null
        double=$((size * 2))
        sips -z "$double" "$double" assets/icon_1024.png \
            --out "build/icon.iconset/icon_${size}x${size}@2x.png" >/dev/null
    done
    iconutil -c icns build/icon.iconset -o build/icon.icns
    ICON_ARGS=(--icon build/icon.icns)
    echo "built build/icon.icns"
else
    echo "iconutil/sips not found; building without a custom icon"
fi

# 2. Build the bundle.
if ! "$PY" -m PyInstaller --version >/dev/null 2>&1; then
    echo "ERROR: PyInstaller is not installed. Run:  $PY -m pip install pyinstaller" >&2
    exit 1
fi

"$PY" -m PyInstaller --noconfirm --windowed --name "$APP_NAME" \
    "${ICON_ARGS[@]}" main.py

echo
echo "Done. Bundle at: dist/$APP_NAME.app"
echo "Launch with:    open \"dist/$APP_NAME.app\""
