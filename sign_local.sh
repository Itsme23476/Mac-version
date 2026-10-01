#!/bin/bash
# Sign the already-built dist/Filect.app with your Developer ID cert.
# RUN THIS IN Terminal.app (Applications > Utilities > Terminal) — not the Claude
# Run button — so macOS can show the keychain "Allow / Always Allow" dialog for your
# private key. Click "Always Allow" (you may get one dialog per nested binary the
# first time; after that it stops asking).
#
# WHY: the Accessibility (paste) permission is keyed to the code-signing identity.
# Ad-hoc builds get a new identity every rebuild, so the grant never sticks. Signing
# with the stable Developer ID cert (Team B3L4MXAS22) makes the grant persist.
set -uo pipefail

ROOT="/Users/damianosmalliaros/Desktop/Mac_app"
ENT="$ROOT/build/entitlements.plist"
SIGN_ID="A7C280E7D88D8759440A432944F333040CA7862A"   # Developer ID Application: Damianos Malliaros (B3L4MXAS22)
CLEAN="/tmp/Filect-clean.app"

if [ ! -d "$ROOT/dist/Filect.app" ]; then
  echo "ERROR: $ROOT/dist/Filect.app not found — build first."; exit 1
fi

echo "== Quit any running Filect =="
pkill -9 -f "dist/Filect.app/Contents/MacOS/Filect" 2>/dev/null || true
sleep 1

echo "== Clean copy (strips SIP xattr detritus so codesign won't choke) =="
rm -rf "$CLEAN"
cp -RpX "$ROOT/dist/Filect.app" "$CLEAN"

echo "== Signing with Developer ID (approve the keychain dialog: Always Allow) =="
codesign --force --deep --options runtime \
  --sign "$SIGN_ID" \
  --entitlements "$ENT" \
  "$CLEAN"
if [ $? -ne 0 ]; then
  echo "SIGN_FAILED — if you saw errSecInternalComponent, the keychain dialog was not"
  echo "approved. Re-run in Terminal.app and click Always Allow."
  exit 1
fi

echo "== Verify =="
codesign -dvvv "$CLEAN" 2>&1 | grep -iE "Authority=|TeamIdentifier=|Identifier=|flags="

echo "== Move signed app back into dist/ =="
rm -rf "$ROOT/dist/Filect.app"
mv "$CLEAN" "$ROOT/dist/Filect.app"

echo "== Launch signed build =="
open "$ROOT/dist/Filect.app"

echo "SIGN_DONE — now grant Accessibility + Microphone to this Filect in System Settings."
