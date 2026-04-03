#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
source .venv/bin/activate

LOG_FILE="/tmp/avatar-backend.log"
PID_FILE="/tmp/avatar-backend.pid"

if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "Backend already running (PID $(cat "$PID_FILE"))"
    exit 0
fi

nohup uvicorn avatar_backend.main:app \
    --host 0.0.0.0 \
    --port 8000 \
    --log-level info \
    > "$LOG_FILE" 2>&1 &

echo $! > "$PID_FILE"
echo "Avatar backend started in background (PID $(cat "$PID_FILE"))"
echo "Logs: tail -f ${LOG_FILE}"
echo "Stop: bash stop.sh"
