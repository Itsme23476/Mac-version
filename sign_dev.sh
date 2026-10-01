#!/bin/bash
# Sign the current ~/Developer/Mac_app build with your Developer ID cert (stable identity).
# RUN THIS IN Terminal.app so macOS can show the keychain dialog — click "Always Allow".
# After that one approval, Accessibility granted to this app PERSISTS across future
# rebuilds (Developer ID identity never changes), and automated builds can sign silently.
set -uo pipefail
ROOT="/Users/damianosmalliaros/Developer/Mac_app"
ENT="$ROOT/build/entitlements.plist"
SIGN_ID="A7C280E7D88D8759440A432944F333040CA7862A"   # Developer ID Application: Damianos Malliaros (B3L4MXAS22)
CLEAN="/tmp/Filect-clean.app"

[ -d "$ROOT/dist/Filect.app" ] || { echo "No dist/Filect.app — build first."; exit 1; }

echo "== quit any running Filect =="
pkill -9 -f "Filect.app/Contents/MacOS/Filect" 2>/dev/null || true
sleep 1

echo "== clean copy (strips the provenance xattr that blocks codesign) =="
rm -rf "$CLEAN"
cp -RpX "$ROOT/dist/Filect.app" "$CLEAN"

echo "== signing with Developer ID — CLICK 'Always Allow' on the keychain dialog =="
codesign --force --deep --options runtime --sign "$SIGN_ID" --entitlements "$ENT" "$CLEAN" || {
  echo "SIGN FAILED. If you saw errSecInternalComponent, the keychain dialog was not"
  echo "approved — make sure you're in Terminal.app and click Always Allow, then re-run."
  exit 1
}

echo "== verify (should show your Developer ID Authority + Team) =="
codesign -dv --verbose=4 "$CLEAN" 2>&1 | grep -iE "Authority=|TeamIdentifier=|flags=" | head

echo "== move signed app back + launch =="
rm -rf "$ROOT/dist/Filect.app"
mv "$CLEAN" "$ROOT/dist/Filect.app"
open "$ROOT/dist/Filect.app"
echo "SIGNED_OK — now: System Settings > Privacy & Security > Accessibility >"
echo "remove any old 'Filect', add this one, toggle ON. It will STICK from now on."
