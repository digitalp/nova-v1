#!/usr/bin/env bash
cd "$(dirname "$0")"
source .venv/bin/activate 2>/dev/null || true

echo ""
echo "=== Ollama (Docker) ==="
docker ps --filter "name=avatar_ollama" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" 2>/dev/null || echo "Docker not available"

echo ""
echo "=== Backend Process ==="
PID_FILE="/tmp/avatar-backend.pid"
if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "Running (PID $(cat "$PID_FILE"))"
else
    echo "Not running (or started in foreground)"
fi

echo ""
echo "=== Health Endpoint ==="
API_KEY=$(grep '^API_KEY=' .env 2>/dev/null | cut -d= -f2)
if [[ -n "$API_KEY" ]]; then
    curl -s -H "X-API-Key: ${API_KEY}" http://localhost:8000/health \
        | python3 -m json.tool 2>/dev/null || echo "Backend not responding"
else
    echo ".env not found or API_KEY not set"
fi
echo ""
