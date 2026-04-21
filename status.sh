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
    echo "NOT RUNNING"
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
echo "=== Background Loops ==="
if [[ -n "$API_KEY" ]]; then
    curl -sf -H "X-API-Key: ${API_KEY}" http://localhost:8001/health \
        | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    loops = d.get('background_loops', {})
    if not loops:
        print('  (loop status not in health response)')
    else:
        for name, state in loops.items():
            print(f'  {name}: {state}')
except Exception:
    print('  (could not parse health response)')
" 2>/dev/null || true
    # Fallback: check via /health/loops if it exists
    LOOPS=$(curl -sf -H "X-API-Key: ${API_KEY}" http://localhost:8001/health/loops 2>/dev/null)
    if [[ -n "$LOOPS" ]]; then
        echo "$LOOPS" | python3 -m json.tool 2>/dev/null
    fi
else
    echo "API_KEY not set — skipping loop check"
fi

echo ""
echo "=== Afro TTS ==="
curl -sf --max-time 3 http://localhost:8021/health 2>/dev/null \
    | python3 -m json.tool 2>/dev/null \
    || echo "unreachable (http://localhost:8021)"

echo ""
