#!/usr/bin/env bash
PID_FILE="/tmp/avatar-backend.pid"
if [[ -f "$PID_FILE" ]]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        kill "$PID"
        rm "$PID_FILE"
        echo "Avatar backend (PID ${PID}) stopped."
    else
        echo "Process ${PID} not running. Removing stale PID file."
        rm "$PID_FILE"
    fi
else
    echo "No PID file found. Backend may not be running."
fi
