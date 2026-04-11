#!/usr/bin/env bash
# Fix boot ordering: avatar-backend waits for cloudflared-nova
set -euo pipefail

echo "Creating systemd override for avatar-backend..."
mkdir -p /etc/systemd/system/avatar-backend.service.d
cat > /etc/systemd/system/avatar-backend.service.d/tunnel.conf << 'EOF'
[Unit]
After=cloudflared-nova.service
Wants=cloudflared-nova.service
EOF

echo "Creating systemd override for nova-v2..."
mkdir -p /etc/systemd/system/nova-v2.service.d
cat > /etc/systemd/system/nova-v2.service.d/tunnel.conf << 'EOF'
[Unit]
After=cloudflared-nova.service
Wants=cloudflared-nova.service
EOF

systemctl daemon-reload
echo "Done. Both services will now start after cloudflared-nova on boot."
