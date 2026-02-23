#!/bin/bash
#
# Lumina macOS Build Script
# Builds the app bundle and optionally creates a DMG installer
#

set -e  # Exit on error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Script directory
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
APP_DIR="$SCRIPT_DIR/ai_file_organizer"
BUILD_DIR="$SCRIPT_DIR/build"
DIST_DIR="$SCRIPT_DIR/dist"

# App info
APP_NAME="Lumina"
VERSION=$(grep -oP 'VERSION = "\K[^"]+' "$APP_DIR/app/version.py" 2>/dev/null || echo "1.0.0")

echo -e "${BLUE}============================================${NC}"
echo -e "${BLUE}  Building $APP_NAME v$VERSION for macOS${NC}"
echo -e "${BLUE}============================================${NC}"
echo ""

# Check for required tools
echo -e "${YELLOW}Checking requirements...${NC}"

if ! command -v python3 &> /dev/null; then
    echo -e "${RED}Error: python3 is not installed${NC}"
    exit 1
fi

if ! python3 -c "import PyInstaller" &> /dev/null; then
    echo -e "${YELLOW}Installing PyInstaller...${NC}"
    pip3 install pyinstaller
fi

# Install/update dependencies
echo -e "${YELLOW}Installing dependencies...${NC}"
pip3 install -r "$APP_DIR/requirements.txt" --quiet

# Clean previous builds
echo -e "${YELLOW}Cleaning previous builds...${NC}"
rm -rf "$DIST_DIR/$APP_NAME.app"
rm -rf "$DIST_DIR/$APP_NAME"
rm -rf "$SCRIPT_DIR/build/$APP_NAME"

# Run PyInstaller
echo -e "${YELLOW}Running PyInstaller...${NC}"
cd "$SCRIPT_DIR"
python3 -m PyInstaller \
    --clean \
    --noconfirm \
    Lumina.spec

# Check if build succeeded
if [ ! -d "$DIST_DIR/$APP_NAME.app" ]; then
    echo -e "${RED}Error: Build failed - $APP_NAME.app not created${NC}"
    exit 1
fi

echo -e "${GREEN}✓ App bundle created: $DIST_DIR/$APP_NAME.app${NC}"

# Get app size
APP_SIZE=$(du -sh "$DIST_DIR/$APP_NAME.app" | cut -f1)
echo -e "${BLUE}  App size: $APP_SIZE${NC}"

# Optional: Create DMG
if [ "$1" == "--dmg" ] || [ "$1" == "-d" ]; then
    echo ""
    echo -e "${YELLOW}Creating DMG installer...${NC}"
    
    DMG_NAME="$APP_NAME-$VERSION-mac"
    DMG_PATH="$DIST_DIR/$DMG_NAME.dmg"
    
    # Remove existing DMG
    rm -f "$DMG_PATH"
    
    # Check if create-dmg is installed
    if command -v create-dmg &> /dev/null; then
        # Use create-dmg for prettier DMG
        create-dmg \
            --volname "$APP_NAME $VERSION" \
            --volicon "$APP_DIR/resources/icon.icns" \
            --window-pos 200 120 \
            --window-size 600 400 \
            --icon-size 100 \
            --icon "$APP_NAME.app" 150 190 \
            --hide-extension "$APP_NAME.app" \
            --app-drop-link 450 185 \
            "$DMG_PATH" \
            "$DIST_DIR/$APP_NAME.app"
    else
        # Fallback to hdiutil
        echo -e "${YELLOW}Note: Install 'create-dmg' for prettier DMG (brew install create-dmg)${NC}"
        
        # Create temporary directory for DMG contents
        DMG_TEMP="$DIST_DIR/dmg_temp"
        rm -rf "$DMG_TEMP"
        mkdir -p "$DMG_TEMP"
        
        # Copy app to temp directory
        cp -R "$DIST_DIR/$APP_NAME.app" "$DMG_TEMP/"
        
        # Create symlink to Applications
        ln -s /Applications "$DMG_TEMP/Applications"
        
        # Create DMG
        hdiutil create -volname "$APP_NAME $VERSION" \
            -srcfolder "$DMG_TEMP" \
            -ov -format UDZO \
            "$DMG_PATH"
        
        # Clean up
        rm -rf "$DMG_TEMP"
    fi
    
    if [ -f "$DMG_PATH" ]; then
        DMG_SIZE=$(du -sh "$DMG_PATH" | cut -f1)
        echo -e "${GREEN}✓ DMG created: $DMG_PATH ($DMG_SIZE)${NC}"
    else
        echo -e "${RED}Error: DMG creation failed${NC}"
        exit 1
    fi
fi

# Summary
echo ""
echo -e "${GREEN}============================================${NC}"
echo -e "${GREEN}  Build Complete!${NC}"
echo -e "${GREEN}============================================${NC}"
echo ""
echo -e "  App:     ${BLUE}$DIST_DIR/$APP_NAME.app${NC}"
if [ -f "$DMG_PATH" ]; then
    echo -e "  DMG:     ${BLUE}$DMG_PATH${NC}"
fi
echo ""
echo -e "  To test: ${YELLOW}open $DIST_DIR/$APP_NAME.app${NC}"
echo ""

# Optional code signing reminder
echo -e "${YELLOW}Note: For distribution, you should code sign the app:${NC}"
echo -e "  codesign --force --deep --sign \"Developer ID Application: YOUR NAME\" $DIST_DIR/$APP_NAME.app"
echo ""
