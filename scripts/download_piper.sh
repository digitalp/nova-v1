#!/usr/bin/env bash
# Download the Piper TTS binary (Linux x86_64) to /opt/avatar-server/piper/
set -euo pipefail

INSTALL_DIR="/opt/avatar-server/piper"
RELEASE_URL="https://github.com/rhasspy/piper/releases/download/2023.11.14-2/piper_linux_x86_64.tar.gz"
ARCHIVE="/tmp/piper_linux_x86_64.tar.gz"

mkdir -p "$INSTALL_DIR"

echo "Downloading Piper binary..."
wget -q --show-progress -O "$ARCHIVE" "$RELEASE_URL"

echo "Extracting..."
tar -xzf "$ARCHIVE" -C "$INSTALL_DIR" --strip-components=1
rm -f "$ARCHIVE"

chmod +x "$INSTALL_DIR/piper"
echo "Piper installed at $INSTALL_DIR/piper"
"$INSTALL_DIR/piper" --version 2>/dev/null || true
