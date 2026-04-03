#!/usr/bin/env bash
# Download Piper TTS voice model files to /opt/avatar-server/config/piper_voices/
set -euo pipefail

VOICES_DIR="/opt/avatar-server/config/piper_voices"
VOICE="${1:-en_US-lessac-medium}"
BASE_URL="https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0"

# Map voice name to path components: language / voice / quality
# en_US-lessac-medium → en/en_US/lessac/medium
IFS='-' read -ra PARTS <<< "$VOICE"
LANG_CODE="${PARTS[0]}"          # en_US
SPEAKER="${PARTS[1]}"            # lessac
QUALITY="${PARTS[2]}"            # medium
LANG_SHORT="${LANG_CODE%%_*}"    # en

VOICE_DIR_PATH="${LANG_SHORT}/${LANG_CODE}/${SPEAKER}/${QUALITY}"

mkdir -p "$VOICES_DIR"

echo "Downloading Piper voice: ${VOICE}"
echo "Source: ${BASE_URL}/${VOICE_DIR_PATH}/${VOICE}.onnx"

wget -q --show-progress -O "${VOICES_DIR}/${VOICE}.onnx" \
    "${BASE_URL}/${VOICE_DIR_PATH}/${VOICE}.onnx"

wget -q --show-progress -O "${VOICES_DIR}/${VOICE}.onnx.json" \
    "${BASE_URL}/${VOICE_DIR_PATH}/${VOICE}.onnx.json"

echo "Voice downloaded to ${VOICES_DIR}:"
ls -lh "${VOICES_DIR}/${VOICE}"*
