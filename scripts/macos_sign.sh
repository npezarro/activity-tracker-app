#!/bin/bash
# Sign ActivityTracker.app inside-out with the hardened runtime (what notarization requires).
#   scripts/macos_sign.sh <identity> <path/to/ActivityTracker.app>
# identity "-" = ad-hoc (CI without an Apple certificate: still proves the app runs
# under the hardened runtime); otherwise "Developer ID Application: Name (TEAMID)".
set -euo pipefail
identity="$1"; app="$2"
ent="$(cd "$(dirname "$0")/.." && pwd)/installer/entitlements.plist"
ts=(--timestamp); { [ "$identity" = "-" ] || [ "${SIGN_TIMESTAMP:-}" = "none" ]; } && ts=(--timestamp=none)
# 1) every nested Mach-O binary (dylibs, .so modules, helper executables), deepest first
find "$app/Contents" -type f \( -name "*.dylib" -o -name "*.so" -o -perm -u+x \) -print0 |
  while IFS= read -r -d '' f; do
    if file -b "$f" | grep -q "Mach-O"; then
      codesign --force --options runtime "${ts[@]}" --entitlements "$ent" --sign "$identity" "$f"
    fi
  done
# 2) the bundle itself
codesign --force --options runtime "${ts[@]}" --entitlements "$ent" --sign "$identity" "$app"
codesign --verify --strict --verbose=2 "$app"
codesign -d --entitlements - "$app" 2>/dev/null | grep -q apple-events
echo "signed $app ($identity, hardened runtime)"
