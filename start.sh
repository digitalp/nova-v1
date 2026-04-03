#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
source .venv/bin/activate

echo "Starting avatar backend..."
echo "Press Ctrl+C to stop."
echo ""

uvicorn avatar_backend.main:app \
    --host 0.0.0.0 \
    --port 8000 \
    --reload \
    --log-level info
