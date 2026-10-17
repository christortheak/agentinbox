#!/bin/bash
# Release build + signing for calexport.
# Usage: ./build.sh ["Developer ID Application: Your Name (TEAMID)" | -]
#
# Signing identity, in order: the argument, $CODESIGN_IDENTITY, the first
# "Developer ID Application" certificate in your keychain, else ad-hoc ("-").
# A stable Developer ID keeps macOS privacy grants across rebuilds; with
# ad-hoc signing the grants are tied to this exact build, so after a rebuild
# you approve Calendar/Reminders access again.
set -euo pipefail
cd "$(dirname "$0")"

IDENTITY="${1:-${CODESIGN_IDENTITY:-}}"
if [ -z "$IDENTITY" ]; then
    IDENTITY=$(security find-identity -v -p codesigning 2>/dev/null \
        | grep -o '"Developer ID Application: [^"]*"' | head -1 | tr -d '"') || true
fi
if [ -z "$IDENTITY" ]; then
    IDENTITY="-"
    echo "note: no Developer ID certificate found; signing ad-hoc." >&2
fi

swift build -c release
BIN=.build/release/calexport

codesign --force --options runtime \
    --identifier com.agentinbox.calexport \
    --entitlements ../launcher/entitlements.plist \
    --sign "$IDENTITY" "$BIN"
codesign --verify --strict "$BIN"

DEST="$HOME/.agentinbox/bin"
mkdir -p "$DEST"
# Remove first so TCC sees a clean replacement rather than a modified file.
rm -f "$DEST/calexport"
cp "$BIN" "$DEST/calexport"
if [ "$IDENTITY" = "-" ]; then
    echo "installed: $DEST/calexport (ad-hoc signed)"
else
    echo "installed: $DEST/calexport (signed as: $IDENTITY)"
fi
