#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Nova AI Avatar — One-touch installer
# Supports Ubuntu 22.04/24.04, Debian 12+  (x86_64)
#
# Usage:
#   ./install.sh            — full interactive install
#   ./install.sh --update   — update source + static files, restart service
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

INSTALL_DIR="/opt/avatar-server"
SERVICE_NAME="avatar-backend"
SERVICE_USER="${SUDO_USER:-$(whoami)}"
PIPER_RELEASE_URL="https://github.com/rhasspy/piper/releases/download/2023.11.14-2/piper_linux_x86_64.tar.gz"
HF_VOICES_BASE="https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0"

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${CYAN}▶ $*${RESET}"; }
success() { echo -e "${GREEN}✔ $*${RESET}"; }
warn()    { echo -e "${YELLOW}⚠ $*${RESET}"; }
error()   { echo -e "${RED}✘ $*${RESET}" >&2; exit 1; }
header()  { echo -e "\n${BOLD}━━━  $*  ━━━${RESET}\n"; }
ask()     { echo -en "${BOLD}$*${RESET} "; }

default_timezone() {
  timedatectl show --property=Timezone --value 2>/dev/null || cat /etc/timezone 2>/dev/null || echo "UTC"
}

# ── Update-only mode ──────────────────────────────────────────────────────────
UPDATE_ONLY=false
[[ "${1:-}" == "--update" ]] && UPDATE_ONLY=true

# ── Root check ────────────────────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
  error "Run as root: sudo ./install.sh"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ─────────────────────────────────────────────────────────────────────────────
#  UPDATE MODE — just sync files and restart
# ─────────────────────────────────────────────────────────────────────────────
if $UPDATE_ONLY; then
  header "Nova — Update"
  info "Syncing source files to ${INSTALL_DIR}…"
  rsync -a --exclude='.env' --exclude='.venv' --exclude='__pycache__' \
    --exclude='*.pyc' --exclude='config/piper_voices' --exclude='piper' \
    "${SCRIPT_DIR}/avatar_backend" "${SCRIPT_DIR}/static" \
    "${SCRIPT_DIR}/intron_afro_tts_sidecar" \
    "${SCRIPT_DIR}/requirements.txt" \
    "${INSTALL_DIR}/"
  info "Installing/updating Python dependencies…"
  "${INSTALL_DIR}/.venv/bin/pip" install -q -r "${INSTALL_DIR}/requirements.txt"
  info "Ensuring gemma2:9b is available (sensor watch + fallback)…"
  docker exec avatar_ollama ollama pull gemma2:9b 2>/dev/null || true
  info "Restarting service…"
  systemctl restart "${SERVICE_NAME}"
  success "Nova updated and restarted."
  exit 0
fi

# ─────────────────────────────────────────────────────────────────────────────
#  FULL INSTALL
# ─────────────────────────────────────────────────────────────────────────────
echo -e "${BOLD}"
cat << 'BANNER'
  _   _                 _    _    ___     _             _
 | \ | | _____   ____ _| |  / \  |_ _|  / \__   ____ _| |_ __ _ _ __
 |  \| |/ _ \ \ / / _` | | / _ \  | |  / _ \ \ / / _` | __/ _` | '__|
 | |\  | (_) \ V / (_| | |/ ___ \ | | / ___ \ V / (_| | || (_| | |
 |_| \_|\___/ \_/ \__,_|_/_/   \_\___/_/   \_\_/ \__,_|\__\__,_|_|

BANNER
echo -e "${RESET}"
echo "  Local AI smart home assistant with voice, lip sync & HA control."
echo ""

# ── Prerequisite checks ───────────────────────────────────────────────────────
header "Checking prerequisites"

# OS
if ! grep -qE "(ubuntu|debian)" /etc/os-release 2>/dev/null; then
  warn "Detected non-Ubuntu/Debian OS — install may still work but is untested."
fi

# Python 3.10+
PYTHON=""
for py in python3.12 python3.11 python3.10 python3; do
  if command -v "$py" &>/dev/null; then
    VER=$($py -c 'import sys; print(sys.version_info[:2])' 2>/dev/null)
    if $py -c 'import sys; exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
      PYTHON="$py"; break
    fi
  fi
done
[[ -z "$PYTHON" ]] && error "Python 3.10+ not found. Install with: apt install python3.12"
success "Python: $($PYTHON --version)"

# curl / wget / rsync / ffmpeg
for cmd in curl wget rsync ffmpeg; do
  command -v "$cmd" &>/dev/null || { info "Installing ${cmd}…"; apt-get install -y "$cmd" -qq; }
done

# Docker
DOCKER_OK=false
if command -v docker &>/dev/null; then
  DOCKER_OK=true
  success "Docker: $(docker --version | cut -d' ' -f3 | tr -d ',')"
else
  warn "Docker not found — needed for Ollama (local LLM). Skip if using cloud LLM."
fi

# GPU
GPU_FOUND=false
if command -v nvidia-smi &>/dev/null && nvidia-smi &>/dev/null; then
  GPU_FOUND=true
  GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1)
  success "GPU: ${GPU_NAME}"
else
  warn "No NVIDIA GPU found — Ollama will run on CPU (slower)."
fi

# ── Gather config ─────────────────────────────────────────────────────────────
header "Configuration"
echo "  Press Enter to accept defaults shown in [brackets]."
echo ""

# API Key
ask "API key for Nova (leave blank to auto-generate):"
read -r INPUT_API_KEY
if [[ -z "$INPUT_API_KEY" ]]; then
  INPUT_API_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
  echo "  Generated: ${INPUT_API_KEY}"
fi

# HA
ask "Home Assistant URL [http://homeassistant.local:8123]:"
read -r INPUT_HA_URL
INPUT_HA_URL="${INPUT_HA_URL:-http://homeassistant.local:8123}"

ask "HA Long-Lived Access Token (blank to configure later):"
read -r INPUT_HA_TOKEN
INPUT_HA_TOKEN="${INPUT_HA_TOKEN:-}"

DEFAULT_HOME_LABEL="$(hostname)"
DEFAULT_TIMEZONE="$(default_timezone)"
DEFAULT_PRIMARY_USERS="${SUDO_USER:-$(whoami)}"

ask "Home label or address [${DEFAULT_HOME_LABEL}]:"
read -r INPUT_HOME_LABEL
INPUT_HOME_LABEL="${INPUT_HOME_LABEL:-${DEFAULT_HOME_LABEL}}"

ask "Timezone [${DEFAULT_TIMEZONE}]:"
read -r INPUT_TIMEZONE
INPUT_TIMEZONE="${INPUT_TIMEZONE:-${DEFAULT_TIMEZONE}}"

ask "Primary household members (comma-separated) [${DEFAULT_PRIMARY_USERS}]:"
read -r INPUT_PRIMARY_USERS
INPUT_PRIMARY_USERS="${INPUT_PRIMARY_USERS:-${DEFAULT_PRIMARY_USERS}}"

ask "Other household members (optional; semicolon-separated as Name:role or Name:role,details):"
read -r INPUT_OTHER_MEMBERS
INPUT_OTHER_MEMBERS="${INPUT_OTHER_MEMBERS:-}"

ask "Vehicles (optional; semicolon-separated as Owner:colour make model):"
read -r INPUT_VEHICLES
INPUT_VEHICLES="${INPUT_VEHICLES:-}"

ask "Other useful household notes (optional; semicolon-separated):"
read -r INPUT_HOME_NOTES
INPUT_HOME_NOTES="${INPUT_HOME_NOTES:-}"

# LLM provider
echo ""
echo "  LLM providers:"
echo "    1) ollama   — local Llama 3.1 8B (requires Docker, 5 GB download)"
echo "    2) google   — Gemini (requires GOOGLE_API_KEY)"
echo "    3) openai   — GPT (requires OPENAI_API_KEY)"
echo "    4) anthropic — Claude (requires ANTHROPIC_API_KEY)"
ask "LLM provider [1]:"
read -r LLM_CHOICE
LLM_CHOICE="${LLM_CHOICE:-1}"

case "$LLM_CHOICE" in
  1) INPUT_LLM_PROVIDER="ollama"; INPUT_CLOUD_MODEL=""; INPUT_CLOUD_KEY_NAME=""; INPUT_CLOUD_KEY="" ;;
  2)
    INPUT_LLM_PROVIDER="google"
    ask "  Gemini model [gemini-2.0-flash]:"; read -r INPUT_CLOUD_MODEL
    INPUT_CLOUD_MODEL="${INPUT_CLOUD_MODEL:-gemini-2.0-flash}"
    INPUT_CLOUD_KEY_NAME="GOOGLE_API_KEY"
    ask "  Google API key:"; read -r INPUT_CLOUD_KEY
    ;;
  3)
    INPUT_LLM_PROVIDER="openai"
    ask "  OpenAI model [gpt-4o-mini]:"; read -r INPUT_CLOUD_MODEL
    INPUT_CLOUD_MODEL="${INPUT_CLOUD_MODEL:-gpt-4o-mini}"
    INPUT_CLOUD_KEY_NAME="OPENAI_API_KEY"
    ask "  OpenAI API key:"; read -r INPUT_CLOUD_KEY
    ;;
  4)
    INPUT_LLM_PROVIDER="anthropic"
    ask "  Anthropic model [claude-haiku-4-5-20251001]:"; read -r INPUT_CLOUD_MODEL
    INPUT_CLOUD_MODEL="${INPUT_CLOUD_MODEL:-claude-haiku-4-5-20251001}"
    INPUT_CLOUD_KEY_NAME="ANTHROPIC_API_KEY"
    ask "  Anthropic API key:"; read -r INPUT_CLOUD_KEY
    ;;
  *) error "Invalid choice." ;;
esac

# Whisper model
echo ""
echo "  Whisper STT models (larger = more accurate, slower to load):"
echo "    tiny / base / small (default) / medium / large"
ask "Whisper model [small]:"
read -r INPUT_WHISPER_MODEL
INPUT_WHISPER_MODEL="${INPUT_WHISPER_MODEL:-small}"

# Piper voice
ask "Piper TTS voice [en_US-lessac-medium]:"
read -r INPUT_PIPER_VOICE
INPUT_PIPER_VOICE="${INPUT_PIPER_VOICE:-en_US-lessac-medium}"

# Port
ask "Backend port [8001]:"
read -r INPUT_PORT
INPUT_PORT="${INPUT_PORT:-8001}"

# Speakers (optional)
ask "HA speaker entity IDs — comma separated (blank to skip):"
read -r INPUT_SPEAKERS

echo ""

# ── Create directory structure ────────────────────────────────────────────────
header "Creating directory structure"
mkdir -p "${INSTALL_DIR}"/{config/piper_voices,piper,static/avatars,scripts}
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}"
success "Created ${INSTALL_DIR}"

# ── Copy source files ─────────────────────────────────────────────────────────
header "Copying source files"
rsync -a --exclude='.env' --exclude='.venv' --exclude='__pycache__' \
  --exclude='*.pyc' \
  "${SCRIPT_DIR}/avatar_backend" \
  "${SCRIPT_DIR}/static" \
  "${SCRIPT_DIR}/intron_afro_tts_sidecar" \
  "${SCRIPT_DIR}/requirements.txt" \
  "${SCRIPT_DIR}/docker-compose.yml" \
  "${SCRIPT_DIR}/scripts" \
  "${INSTALL_DIR}/"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}"
success "Source files copied"

# ── Default config files ──────────────────────────────────────────────────────
header "Writing default config files"

# Copy template (always — useful reference even if prompt already exists)
cp "${SCRIPT_DIR}/config/system_prompt_template.txt" "${INSTALL_DIR}/config/system_prompt_template.txt" 2>/dev/null || true

GENERATE_INITIAL_PROMPT=false
if [[ ! -f "${INSTALL_DIR}/config/system_prompt.txt" ]]; then
  cp "${SCRIPT_DIR}/config/system_prompt_template.txt" "${INSTALL_DIR}/config/system_prompt.txt" 2>/dev/null ||   echo 'You are Nova, a smart home AI. Edit config/system_prompt.txt to customise.' > "${INSTALL_DIR}/config/system_prompt.txt"
  GENERATE_INITIAL_PROMPT=true
  warn "Created system_prompt.txt from template — installer will now try to personalise it from Home Assistant and your household details"
  info "Reference template kept at: ${INSTALL_DIR}/config/system_prompt_template.txt"
else
  success "system_prompt.txt already exists — skipping"
fi
if [[ ! -f "${INSTALL_DIR}/config/acl.yaml" ]]; then
  cp "${SCRIPT_DIR}/config/acl.yaml" "${INSTALL_DIR}/config/acl.yaml" 2>/dev/null || \
  cat > "${INSTALL_DIR}/config/acl.yaml" << 'ACL_EOF'
# Nova — Entity Access Control List
# Grant unrestricted access to all HA entities and services.
version: 1
rules:
  - domain: "*"
    entities: "*"
    services: "*"
ACL_EOF
  success "Created default acl.yaml"
else
  success "acl.yaml already exists — skipping"
fi

# ── Write .env ─────────────────────────────────────────────────────────────────
header "Writing .env"

if [[ -f "${INSTALL_DIR}/.env" ]]; then
  warn ".env already exists — backing up to .env.bak"
  cp "${INSTALL_DIR}/.env" "${INSTALL_DIR}/.env.bak"
fi

OLLAMA_MODEL_LINE="OLLAMA_MODEL=llama3.1:8b-instruct-q4_K_M"
OLLAMA_URL_LINE="OLLAMA_URL=http://localhost:11434"

cat > "${INSTALL_DIR}/.env" << ENV_EOF
# Nova AI Avatar — Environment Config
HOST=0.0.0.0
PORT=${INPUT_PORT}
LOG_LEVEL=INFO

# Security
API_KEY=${INPUT_API_KEY}

# Home Assistant
HA_URL=${INPUT_HA_URL}
HA_TOKEN=${INPUT_HA_TOKEN}

# LLM
LLM_PROVIDER=${INPUT_LLM_PROVIDER}
${OLLAMA_URL_LINE}
${OLLAMA_MODEL_LINE}
CLOUD_MODEL=${INPUT_CLOUD_MODEL:-}
${INPUT_CLOUD_KEY_NAME:-OPENAI_API_KEY}=${INPUT_CLOUD_KEY:-}
GOOGLE_API_KEY=${INPUT_CLOUD_KEY:-}

# STT / TTS
WHISPER_MODEL=${INPUT_WHISPER_MODEL}
TTS_PROVIDER=piper
PIPER_VOICE=${INPUT_PIPER_VOICE}

# ElevenLabs TTS (set TTS_PROVIDER=elevenlabs to use)
ELEVENLABS_API_KEY=
ELEVENLABS_VOICE_ID=21m00Tcm4TlvDq8ikWAM
ELEVENLABS_MODEL=eleven_monolingual_v1

# AfroTTS / Kokoro TTS (set TTS_PROVIDER=afrotts to use)
AFROTTS_VOICE=af_heart
AFROTTS_SPEED=1.0

# Intron Afro TTS sidecar (set TTS_PROVIDER=intron_afro_tts to use)
INTRON_AFRO_TTS_URL=http://127.0.0.1:8021
INTRON_AFRO_TTS_TIMEOUT_S=90
INTRON_AFRO_TTS_REFERENCE_WAV=
INTRON_AFRO_TTS_LANGUAGE=en

# Speakers (comma-separated HA media_player entity IDs, prefix echo devices with alexa:)
SPEAKERS=${INPUT_SPEAKERS:-}

# Public URL used to serve synthesised audio to speakers (must be reachable from HA)
PUBLIC_URL=http://$(hostname -I | awk '{print \$1}'):${INPUT_PORT}
ENV_EOF

chown "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}/.env"
chmod 600 "${INSTALL_DIR}/.env"
success ".env written"

# ── Python virtualenv ──────────────────────────────────────────────────────────
header "Setting up Python virtualenv"
if [[ ! -d "${INSTALL_DIR}/.venv" ]]; then
  sudo -u "${SERVICE_USER}" "$PYTHON" -m venv "${INSTALL_DIR}/.venv"
  success "Virtualenv created"
else
  success "Virtualenv already exists"
fi

info "Installing Python dependencies…"
sudo -u "${SERVICE_USER}" "${INSTALL_DIR}/.venv/bin/pip" install -q --upgrade pip
# Install base deps (requirements.txt excludes CUDA-specific packages)
sudo -u "${SERVICE_USER}" "${INSTALL_DIR}/.venv/bin/pip" install -q -r "${INSTALL_DIR}/requirements.txt"
# Install CUDA runtime libs only when an NVIDIA GPU is present
if $GPU_FOUND; then
  info "GPU detected — installing CUDA 12 runtime libs for faster Whisper inference…"
  sudo -u "${SERVICE_USER}" "${INSTALL_DIR}/.venv/bin/pip" install -q \
    "nvidia-cublas-cu12>=12.0" "nvidia-cuda-nvrtc-cu12>=12.0"
  success "CUDA libs installed"
fi
success "Python dependencies installed"

# ── Personalise system prompt ─────────────────────────────────────────────────
if $GENERATE_INITIAL_PROMPT; then
  header "Generating personalised system prompt"
  PROMPT_BOOTSTRAP_CMD=(
    "${INSTALL_DIR}/.venv/bin/python3"
    "${INSTALL_DIR}/scripts/bootstrap_system_prompt.py"
    --template "${INSTALL_DIR}/config/system_prompt_template.txt"
    --output "${INSTALL_DIR}/config/system_prompt.txt"
    --runtime-output "${INSTALL_DIR}/config/home_runtime.json"
    --address "${INPUT_HOME_LABEL}"
    --timezone "${INPUT_TIMEZONE}"
    --default-user "${SERVICE_USER}"
    --primary-users "${INPUT_PRIMARY_USERS}"
    --other-members "${INPUT_OTHER_MEMBERS}"
    --vehicles "${INPUT_VEHICLES}"
    --notes "${INPUT_HOME_NOTES}"
  )

  if [[ -n "${INPUT_HA_TOKEN}" ]]; then
    PROMPT_BOOTSTRAP_CMD+=(--ha-url "${INPUT_HA_URL}" --ha-token "${INPUT_HA_TOKEN}")
  else
    warn "HA token not provided — generating prompt from installer inputs only."
  fi

  if sudo -u "${SERVICE_USER}" "${PROMPT_BOOTSTRAP_CMD[@]}"; then
    success "Personalised system_prompt.txt generated"
  else
    warn "Prompt bootstrap failed — keeping the template-based system_prompt.txt so install can continue."
  fi
fi

# ── Piper TTS binary ───────────────────────────────────────────────────────────
header "Piper TTS binary"
if [[ ! -f "${INSTALL_DIR}/piper/piper" ]]; then
  info "Downloading Piper binary…"
  ARCHIVE="/tmp/piper_linux_x86_64.tar.gz"
  wget -q --show-progress -O "$ARCHIVE" "$PIPER_RELEASE_URL"
  tar -xzf "$ARCHIVE" -C "${INSTALL_DIR}/piper" --strip-components=1
  rm -f "$ARCHIVE"
  chmod +x "${INSTALL_DIR}/piper/piper"
  chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}/piper"
  success "Piper binary installed"
else
  success "Piper binary already present"
fi

# ── Piper voice model ──────────────────────────────────────────────────────────
header "Piper voice model (${INPUT_PIPER_VOICE})"
ONNX_PATH="${INSTALL_DIR}/config/piper_voices/${INPUT_PIPER_VOICE}.onnx"
if [[ ! -f "$ONNX_PATH" ]]; then
  IFS='-' read -ra PARTS <<< "$INPUT_PIPER_VOICE"
  LANG_CODE="${PARTS[0]}"
  SPEAKER="${PARTS[1]}"
  QUALITY="${PARTS[2]}"
  LANG_SHORT="${LANG_CODE%%_*}"
  VOICE_PATH="${LANG_SHORT}/${LANG_CODE}/${SPEAKER}/${QUALITY}"

  info "Downloading voice model…"
  wget -q --show-progress \
    -O "${INSTALL_DIR}/config/piper_voices/${INPUT_PIPER_VOICE}.onnx" \
    "${HF_VOICES_BASE}/${VOICE_PATH}/${INPUT_PIPER_VOICE}.onnx"
  wget -q --show-progress \
    -O "${INSTALL_DIR}/config/piper_voices/${INPUT_PIPER_VOICE}.onnx.json" \
    "${HF_VOICES_BASE}/${VOICE_PATH}/${INPUT_PIPER_VOICE}.onnx.json"
  chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}/config/piper_voices"
  success "Voice model downloaded"
else
  success "Voice model already present"
fi

# ── Ollama (Docker) ────────────────────────────────────────────────────────────
if [[ "$INPUT_LLM_PROVIDER" == "ollama" ]]; then
  header "Ollama (Docker)"
  if $DOCKER_OK; then
    # Build docker-compose command
    DC="docker compose"
    command -v docker-compose &>/dev/null && DC="docker-compose"

    info "Starting Ollama container…"
    cd "${INSTALL_DIR}"
    if $GPU_FOUND; then
      sudo -u "${SERVICE_USER}" $DC up -d ollama
    else
      # Remove GPU reservation for CPU-only
      sudo -u "${SERVICE_USER}" $DC -f <(sed '/deploy:/,/capabilities:/d' docker-compose.yml) up -d ollama
    fi

    info "Waiting for Ollama to be ready…"
    for i in $(seq 1 30); do
      curl -sf http://localhost:11434/api/tags &>/dev/null && break
      sleep 2
    done

    OLLAMA_MODEL="llama3.1:8b-instruct-q4_K_M"
    info "Pulling LLM model ${OLLAMA_MODEL} (~5 GB, this may take a while)…"
    docker exec avatar_ollama ollama pull "$OLLAMA_MODEL" || warn "Could not pull model — run manually: docker exec avatar_ollama ollama pull ${OLLAMA_MODEL}"
    info "Pulling gemma2:9b (~5 GB) — used for sensor monitoring and cloud LLM failover…"
    docker exec avatar_ollama ollama pull "gemma2:9b" || warn "Could not pull gemma2:9b — run manually: docker exec avatar_ollama ollama pull gemma2:9b"
    success "Ollama ready"
  else
    warn "Docker not available — skipping Ollama setup. Install Docker and run: docker compose up -d"
  fi
fi

# ── Intron Afro TTS sidecar (optional) ────────────────────────────────────────
if $DOCKER_OK && $GPU_FOUND; then
  echo ""
  ask "Install Intron Afro TTS sidecar? (accented voice cloning, requires GPU + Docker) [y/N]:"
  read -r INSTALL_INTRON
  if [[ "${INSTALL_INTRON,,}" == "y" ]]; then
    header "Intron Afro TTS sidecar"

    # Copy sidecar files
    rsync -a "${SCRIPT_DIR}/intron_afro_tts_sidecar" "${INSTALL_DIR}/"
    chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}/intron_afro_tts_sidecar"

    DC="docker compose"
    command -v docker-compose &>/dev/null && DC="docker-compose"

    info "Building Intron Afro TTS container (first build downloads ~4 GB)…"
    cd "${INSTALL_DIR}"

    # Pass HF_TOKEN from .env if available
    export HF_TOKEN="${HF_TOKEN:-}"
    if grep -q "^HF_TOKEN=" "${INSTALL_DIR}/.env" 2>/dev/null; then
      export HF_TOKEN="$(grep '^HF_TOKEN=' "${INSTALL_DIR}/.env" | cut -d= -f2-)"
    fi
    if [[ -z "$HF_TOKEN" ]]; then
      ask "HuggingFace token (needed to download Intron model, get from hf.co/settings/tokens):"
      read -r HF_TOKEN
      if [[ -n "$HF_TOKEN" ]]; then
        echo "HF_TOKEN=${HF_TOKEN}" >> "${INSTALL_DIR}/.env"
      fi
    fi

    sudo -u "${SERVICE_USER}" $DC build intron_afro_tts 2>&1 | tail -5
    info "Starting Intron Afro TTS sidecar…"
    sudo -u "${SERVICE_USER}" $DC up -d intron_afro_tts

    info "Waiting for sidecar to be ready (model download may take a few minutes)…"
    for i in $(seq 1 60); do
      if curl -sf http://127.0.0.1:8021/health &>/dev/null; then
        success "Intron Afro TTS sidecar is running"
        break
      fi
      sleep 5
    done

    if ! curl -sf http://127.0.0.1:8021/health &>/dev/null; then
      warn "Sidecar may still be loading the model. Check: docker compose logs -f intron_afro_tts"
    fi

    # Update .env to include intron settings if not already present
    if ! grep -q "^INTRON_AFRO_TTS_URL=" "${INSTALL_DIR}/.env" 2>/dev/null; then
      cat >> "${INSTALL_DIR}/.env" << 'INTRON_EOF'

# Intron Afro TTS sidecar
INTRON_AFRO_TTS_URL=http://127.0.0.1:8021
INTRON_AFRO_TTS_TIMEOUT_S=90
INTRON_AFRO_TTS_REFERENCE_WAV=
INTRON_AFRO_TTS_LANGUAGE=en
INTRON_EOF
    fi

    echo ""
    echo -e "  ${CYAN}To use Intron Afro TTS:${RESET}"
    echo "    Set TTS_PROVIDER=intron_afro_tts in the admin panel or .env"
    echo "    Select a reference voice in admin panel > Config"
    echo ""
  fi
fi

# ── Whisper model pre-download (optional) ─────────────────────────────────────
header "Whisper STT model"
info "Pre-downloading Whisper '${INPUT_WHISPER_MODEL}' model (happens on first start if skipped)…"
sudo -u "${SERVICE_USER}" "${INSTALL_DIR}/.venv/bin/python3" -c "
from faster_whisper import WhisperModel
print('Downloading Whisper model — please wait…')
WhisperModel('${INPUT_WHISPER_MODEL}', device='cpu', compute_type='int8')
print('Done.')
" && success "Whisper model ready" || warn "Whisper model will download on first voice request."

# ── systemd service ────────────────────────────────────────────────────────────
header "systemd service"

cat > "/etc/systemd/system/${SERVICE_NAME}.service" << SERVICE_EOF
[Unit]
Description=Nova AI Avatar — AI smart home backend
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${INSTALL_DIR}
ExecStart=${INSTALL_DIR}/.venv/bin/uvicorn avatar_backend.main:app --host 0.0.0.0 --port ${INPUT_PORT} --log-level info
Restart=on-failure
RestartSec=5
StartLimitIntervalSec=60
StartLimitBurst=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=${SERVICE_NAME}
TimeoutStartSec=120
EnvironmentFile=${INSTALL_DIR}/.env

[Install]
WantedBy=multi-user.target
SERVICE_EOF

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"

sleep 3
if systemctl is-active --quiet "${SERVICE_NAME}"; then
  success "Service ${SERVICE_NAME} is running"
else
  warn "Service may still be starting. Check: journalctl -u ${SERVICE_NAME} -f"
fi

# ── HA component ───────────────────────────────────────────────────────────────
header "Home Assistant component"

SERVER_IP=$(hostname -I | awk '{print $1}')

echo ""
echo -e "${BOLD}Add the following to your HA configuration.yaml:${RESET}"
echo ""
echo "  ai_avatar:"
echo "    ai_server_url: http://${SERVER_IP}:${INPUT_PORT}"
echo "    api_key: !secret ai_avatar_api_key"
echo ""
echo -e "${BOLD}Add to your HA secrets.yaml:${RESET}"
echo ""
echo "  ai_avatar_api_key: ${INPUT_API_KEY}"
echo ""
echo -e "${BOLD}Copy the HA custom component:${RESET}"
echo ""
echo "  cp -r ${SCRIPT_DIR}/ha/custom_components/ai_avatar /config/custom_components/"
echo ""

# Copy HA component files if present in package
if [[ -d "${SCRIPT_DIR}/ha/custom_components/ai_avatar" ]]; then
  ask "Automatically copy HA component to /config/custom_components/? [y/N]:"
  read -r COPY_HA
  if [[ "${COPY_HA,,}" == "y" ]]; then
    mkdir -p /config/custom_components
    cp -r "${SCRIPT_DIR}/ha/custom_components/ai_avatar" /config/custom_components/
    success "HA component copied to /config/custom_components/ai_avatar"
  fi
fi

# ── Done ───────────────────────────────────────────────────────────────────────
header "Installation complete"
echo ""
echo -e "  ${GREEN}Nova is running at:${RESET}"
echo -e "  ${BOLD}  Avatar:      http://${SERVER_IP}:${INPUT_PORT}/avatar?api_key=${INPUT_API_KEY}${RESET}"
echo -e "  ${BOLD}  Admin panel: http://${SERVER_IP}:${INPUT_PORT}/admin${RESET}"
echo -e "  ${BOLD}  Health:      http://${SERVER_IP}:${INPUT_PORT}/health/public${RESET}"
echo ""
echo -e "  ${CYAN}Useful commands:${RESET}"
echo "    journalctl -u ${SERVICE_NAME} -f        # live logs"
echo "    systemctl restart ${SERVICE_NAME}        # restart"
echo "    ${INSTALL_DIR}/install.sh --update       # update to new version"
echo ""
