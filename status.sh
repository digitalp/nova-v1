#!/usr/bin/env bash
cd "$(dirname "$0")"

echo ""
echo "=== Ollama (Docker) ==="
docker ps --filter "name=avatar_ollama" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" 2>/dev/null || echo "Docker not available"

echo ""
echo "=== Backend Service ==="
if systemctl is-active --quiet avatar-backend 2>/dev/null; then
    echo "systemd: active"
elif [[ -f /tmp/avatar-backend.pid ]] && kill -0 "$(cat /tmp/avatar-backend.pid)" 2>/dev/null; then
    echo "Running (PID $(cat /tmp/avatar-backend.pid))"
else
    echo "Not running"
fi

echo ""
echo "=== Liveness ==="
curl -sf http://localhost:8001/health/live | python3 -m json.tool 2>/dev/null \
    || echo "FAIL — /health/live not responding"

echo ""
echo "=== Readiness ==="
curl -sf http://localhost:8001/health/ready | python3 -m json.tool 2>/dev/null \
    || echo "FAIL — /health/ready not responding"

echo ""
echo "=== Full Health ==="
API_KEY=$(grep '^API_KEY=' .env 2>/dev/null | cut -d= -f2)
if [[ -n "$API_KEY" ]]; then
    curl -sf -H "X-API-Key: ${API_KEY}" http://localhost:8001/health \
        | python3 -m json.tool 2>/dev/null || echo "Backend not responding"
else
    echo ".env not found or API_KEY not set — skipping full health check"
fi
echo ""
