#!/usr/bin/env bash
# Nova AI Avatar — Package builder
# Creates a distributable archive from the current installation.
# Run from /opt/avatar-server/ or pass install dir as arg.
#
# Usage: ./package.sh [output-dir]
set -euo pipefail

INSTALL_DIR="/opt/avatar-server"
OUTPUT_DIR="${1:-${HOME}}"
VERSION=$(date +"%Y.%m.%d")
PKG_NAME="nova-avatar-${VERSION}"
STAGING="/tmp/${PKG_NAME}"

RED='\033[0;31m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'
info()    { echo -e "${CYAN}▶ $*${RESET}"; }
success() { echo -e "${GREEN}✔ $*${RESET}"; }
error()   { echo -e "${RED}✘ $*${RESET}" >&2; exit 1; }

[[ $EUID -ne 0 ]] && error "Run as root: sudo ./package.sh"
[[ ! -d "$INSTALL_DIR" ]] && error "Install dir not found: ${INSTALL_DIR}"

echo -e "${BOLD}Nova AI Avatar — Package Builder${RESET}"
echo "  Version:  ${VERSION}"
echo "  Output:   ${OUTPUT_DIR}/${PKG_NAME}.tar.gz"
echo ""

# ── Stage files ───────────────────────────────────────────────────────────────
info "Staging files…"
rm -rf "$STAGING"
mkdir -p "${STAGING}"/{ha/custom_components,config/piper_voices,scripts}

# Core source
rsync -a --exclude='__pycache__' --exclude='*.pyc' --exclude='.venv' \
  "${INSTALL_DIR}/avatar_backend" \
  "${INSTALL_DIR}/static" \
  "${INSTALL_DIR}/requirements.txt" \
  "${INSTALL_DIR}/docker-compose.yml" \
  "${STAGING}/"

# Scripts
cp "${INSTALL_DIR}/scripts/"*.sh "${STAGING}/scripts/" 2>/dev/null || true

# Default config (no secrets)
cp "${INSTALL_DIR}/config/system_prompt.txt" "${STAGING}/config/" 2>/dev/null || true
cp "${INSTALL_DIR}/config/acl.yaml"          "${STAGING}/config/" 2>/dev/null || true

# Avatar settings (skin tone — no secrets)
cp "${INSTALL_DIR}/config/avatar_settings.json" "${STAGING}/config/" 2>/dev/null || true

# HA custom component
HA_COMP_SRC=""
for p in /homeassistant/custom_components/ai_avatar /config/custom_components/ai_avatar; do
  [[ -d "$p" ]] && { HA_COMP_SRC="$p"; break; }
done
if [[ -n "$HA_COMP_SRC" ]]; then
  cp -r "$HA_COMP_SRC" "${STAGING}/ha/custom_components/ai_avatar"
  info "Included HA custom component from ${HA_COMP_SRC}"
else
  echo "  (HA component not found — skipping)"
fi

# Install script
cp /opt/avatar-server/install.sh "${STAGING}/install.sh" 2>/dev/null || \
  echo "  Warning: install.sh not found in ${INSTALL_DIR}"
chmod +x "${STAGING}/install.sh"

# Readme
cat > "${STAGING}/README.txt" << README
Nova AI Avatar — ${VERSION}
═══════════════════════════════════════

QUICK START
  sudo ./install.sh

WHAT IT DOES
  • Installs the FastAPI backend to /opt/avatar-server/
  • Downloads Piper TTS binary + voice model
  • Optionally sets up Ollama (local LLM) via Docker
  • Creates and enables a systemd service
  • Guides you through HA component setup

UPDATE AN EXISTING INSTALL
  sudo ./install.sh --update

REQUIREMENTS
  • Ubuntu 22.04/24.04 or Debian 12+ (x86_64)
  • Python 3.10+
  • Docker (for local Ollama LLM — not needed for cloud LLM)

AFTER INSTALL
  Avatar:       http://<server-ip>:<port>/avatar?api_key=<key>
  Admin panel:  http://<server-ip>:<port>/admin
  Health check: http://<server-ip>:<port>/health/public

README

# ── Archive ───────────────────────────────────────────────────────────────────
info "Creating archive…"
mkdir -p "$OUTPUT_DIR"
tar -czf "${OUTPUT_DIR}/${PKG_NAME}.tar.gz" -C /tmp "$PKG_NAME"
rm -rf "$STAGING"

SIZE=$(du -sh "${OUTPUT_DIR}/${PKG_NAME}.tar.gz" | cut -f1)
success "Package created: ${OUTPUT_DIR}/${PKG_NAME}.tar.gz (${SIZE})"
echo ""
echo "  Install on a new machine:"
echo "    scp ${OUTPUT_DIR}/${PKG_NAME}.tar.gz user@newhost:~/"
echo "    ssh user@newhost"
echo "    tar xzf ${PKG_NAME}.tar.gz && cd ${PKG_NAME}"
echo "    sudo ./install.sh"
