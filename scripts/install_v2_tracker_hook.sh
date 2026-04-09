#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="/opt/avatar-server"
HOOK_PATH="${REPO_ROOT}/.git/hooks/pre-commit"

cat > "${HOOK_PATH}" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

python3 /opt/avatar-server/scripts/check_v2_tracker.py
EOF

chmod +x "${HOOK_PATH}"
echo "Installed pre-commit hook at ${HOOK_PATH}"
