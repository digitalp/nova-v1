#!/bin/bash
# Setup Fully Kiosk Browser for Nova avatar tablet
# Usage: ./setup_fkb.sh <tablet_ip> <room_id> [fkb_password]
#   e.g. ./setup_fkb.sh 192.168.0.117 living_room linkstar

set -e
IP="${1:?Usage: $0 <tablet_ip> <room_id> [fkb_password]}"
ROOM="${2:?Usage: $0 <tablet_ip> <room_id> [fkb_password]}"
PW="${3:-linkstar}"
API_KEY=$(grep "^API_KEY=" /opt/avatar-server/.env | cut -d= -f2)
BASE="http://${IP}:2323"

fkb_set_bool() {
  echo "  $1 = $2"
  curl -s --max-time 10 "${BASE}/?cmd=setBooleanSetting&key=$1&value=$2&password=${PW}&type=json"
  echo
}

fkb_set_str() {
  echo "  $1 = $2"
  curl -s --max-time 10 --get "${BASE}/" \
    --data-urlencode "cmd=setStringSetting" \
    --data-urlencode "key=$1" \
    --data-urlencode "value=$2" \
    --data-urlencode "password=${PW}" \
    --data-urlencode "type=json"
  echo
}

echo "=== Testing connection to ${IP} ==="
INFO=$(curl -s --max-time 10 "${BASE}/?cmd=deviceInfo&type=json&password=${PW}")
if [ -z "$INFO" ]; then
  echo "ERROR: Cannot reach tablet at ${IP}:2323"
  exit 1
fi
echo "$INFO" | python3 -c "
import sys,json
d=json.load(sys.stdin)
print('  Model:', d.get('deviceModel','?'))
print('  Licensed:', d.get('isLicensed','?'))
print('  Version:', d.get('appVersionName','?'))
"

echo "=== Enabling JavaScript Interface ==="
fkb_set_bool jsInterface true

echo "=== Enabling Camera (Camera2 API, front camera) ==="
fkb_set_str motionCameraApi 1
fkb_set_str motionCameraId 0
fkb_set_bool motionDetection true
fkb_set_str motionSensitivity 0

echo "=== Enabling Microphone + Webcam Access ==="
fkb_set_bool microphoneAccess true
fkb_set_bool webcamAccess true
fkb_set_bool autoGrantPermissions true

echo "=== Setting Start URL ==="
URL="https://192.168.0.249:8443/avatar?room=${ROOM}&api_key=${API_KEY}"
fkb_set_str startURL "${URL}"

echo "=== Restarting app ==="
curl -s --max-time 10 "${BASE}/?cmd=restartApp&password=${PW}&type=json"
echo
sleep 5

echo "=== Verifying ==="
curl -s --max-time 10 "${BASE}/?cmd=deviceInfo&type=json&password=${PW}" | python3 -c "
import sys,json
d=json.load(sys.stdin)
print('  Current page:', d.get('currentPage','?')[:80])
"

echo
echo "Done. Tablet configured for room: ${ROOM}"
echo "URL: ${URL}"
