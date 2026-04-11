# Nova — AI Avatar for Home Assistant

> A fully local, privacy-first AI home assistant with voice, 3D lip-synced avatar, and deep Home Assistant integration.

![Nova Avatar](static/avatars/brunette.glb)

---

## Overview

Nova is an AI-powered smart home assistant that runs entirely on your local network. She listens to your voice, reasons using a local or cloud LLM, controls your Home Assistant devices, and responds through a 3D lip-synced avatar and/or your physical speakers (Amazon Echo, Sonos, Google Home, etc.).

No cloud required by default — everything runs on your own hardware.

---

## Features

- **Voice conversation** — push-to-talk or always-on wake word ("Nova") with VAD
- **3D lip-synced avatar** — powered by [TalkingHead](https://github.com/met4citizen/TalkingHead) with real-time phoneme lip sync
- **Local LLM** — Ollama (Llama 3.1 8B) running in Docker, with optional cloud fallback
- **Multi-provider LLM** — switch between Ollama, OpenAI, Google Gemini, or Anthropic Claude via a single env var
- **Home Assistant control** — turn on lights, adjust climate, play media, read sensor states — all via natural language
- **ACL-gated HA access** — fine-grained entity access control so Nova only touches what you allow
- **Proactive announcements** — HA automations can push alerts to Nova ("Someone is at the door")
- **Sensor monitoring** — dedicated local Ollama LLM (gemma2:9b) watches all `sensor.*` entities; announces battery failures, extreme temperatures, fridge power loss, low fuel, bin collection reminders, and abnormal energy usage — without spending cloud LLM quota
- **Ollama failover** — cloud providers (Gemini, GPT, Claude) automatically fall back to local Ollama `gemma2:9b` when unavailable
- **Speaker broadcast** — plays responses on Amazon Echo and/or Sonos/Cast speakers simultaneously
- **Admin panel** — full web UI to manage config, system prompt, ACL rules, live logs, and sessions — no SSH needed
- **Skin tone customisation** — 5 skin tone presets applied directly to Three.js materials (skin only, not hair/clothing)
- **One-touch installer** — single `install.sh` script sets up everything on a fresh Ubuntu/Debian machine

---

## Architecture

```
Browser / HA Dashboard
        │
        ├── avatar.html (TalkingHead 3D avatar + voice)  ←→  ws://<server>/ws/voice
        └── admin.html  (admin panel)                    ←→  REST /admin/*

Home Assistant
        ├── custom_component: ai_avatar                  ←→  POST /announce, /chat
        └── automation triggers → ai_avatar.announce

AI Server
        ├── FastAPI backend (port 8001)
        │   ├── faster-whisper    (STT)
        │   ├── Piper TTS         (speech synthesis + word timings)
        │   ├── LLM service       (Ollama / OpenAI / Gemini / Anthropic + Ollama fallback)
        │   ├── ProactiveService  (HA WS monitor → cloud LLM triage → announce)
        │   └── SensorWatchService (HA WS sensor.* monitor → local Ollama only → announce)
        └── Ollama                (local LLM, Docker, GPU-accelerated)
            ├── Primary model     (llama3.1:8b or configured model)
            └── gemma2:9b         (always-on: sensor watch + cloud fallback)
```

---

## Requirements

### AI Server
- Ubuntu 22.04 / 24.04 or Debian 12+ (x86_64)
- Python 3.10+
- Docker (for local Ollama LLM — not required when using a cloud provider)
- NVIDIA GPU recommended for Ollama (runs on CPU too, but slower)
- The one-touch installer now configures NVIDIA Container Toolkit automatically when a compatible GPU is present, so the Docker Ollama container can use the GPU
- ~8 GB RAM minimum, 16 GB recommended

### Home Assistant
- Home Assistant 2023.6+
- The `ai_avatar` custom component (included in `ha/custom_components/`)

---

## Quick Start

### 1. Clone and install

```bash
git clone https://github.com/digitalp/nova-avatar.git
cd nova-avatar
sudo ./install.sh
```

The installer will:
- Prompt for your API key, HA URL/token, LLM provider, voice, and speakers
- Download Piper TTS binary and voice model
- Pre-download the Whisper STT model
- Configure Docker's NVIDIA runtime automatically when a GPU is present
- Start the GPU-backed Ollama Docker container and pull the LLM model (if using local LLM)
- Create a Python virtualenv and install dependencies
- Install and start a `systemd` service

### 2. Open the avatar

```
http://<server-ip>:8001/avatar?api_key=<your-api-key>
```

### 3. Open the admin panel

```
http://<server-ip>:8001/admin
```

## Development Guardrails

To prevent `docs/NOVA_V2_IMPLEMENTATION_PROGRESS.md` from drifting away from the implementation plan, install the local pre-commit hook:

```bash
/opt/avatar-server/scripts/install_v2_tracker_hook.sh
```

The hook runs:

```bash
python3 /opt/avatar-server/scripts/check_v2_tracker.py
```

Run that command manually before commits if you are editing the V2 tracker or milestone percentages.

---

## Configuration

All settings live in `/opt/avatar-server/.env`. Edit via the admin panel or directly:

| Variable | Description | Default |
|---|---|---|
| `API_KEY` | Shared secret for all API requests | *(required)* |
| `HA_URL` | Home Assistant base URL | `http://homeassistant.local:8123` |
| `HA_TOKEN` | HA Long-Lived Access Token | *(required)* |
| `LLM_PROVIDER` | `ollama` / `openai` / `google` / `anthropic` | `ollama` |
| `OLLAMA_MODEL` | Ollama model name | `llama3.1:8b-instruct-q4_K_M` |
| `CLOUD_MODEL` | Cloud model name (when not using Ollama) | *(e.g. `gemini-2.0-flash`)* |
| `OPENAI_API_KEY` | OpenAI API key | *(optional)* |
| `GOOGLE_API_KEY` | Google API key | *(optional)* |
| `ANTHROPIC_API_KEY` | Anthropic API key | *(optional)* |
| `WHISPER_MODEL` | STT model size: `tiny` / `base` / `small` / `medium` / `large` | `small` |
| `PIPER_VOICE` | Piper TTS voice name | `en_US-lessac-medium` |
| `SPEAKERS` | Comma-separated HA `media_player` entity IDs | *(optional)* |
| `TTS_ENGINE` | HA TTS engine for non-Echo speakers | `tts.google_translate_en_com` |
| `PORT` | Backend port | `8001` |
| `LOG_LEVEL` | `DEBUG` / `INFO` / `WARNING` | `INFO` |

### Speaker config

```env
# Amazon Echo devices (Alexa Media Player integration required)
SPEAKERS=alexa:media_player.living_room_echo,media_player.bedroom_echo_dot

# Sonos / Google Cast
SPEAKERS=media_player.sonos_kitchen,media_player.living_room_cast

# Mix of both
SPEAKERS=alexa:media_player.living_room_3,media_player.penn_s_2nd_echo_dot
```

Prefix Echo devices with `alexa:` to force Alexa notify mode. Devices with "echo", "alexa", or "amazon" in their entity ID are auto-detected.

---

## Home Assistant Integration

For a high-level explanation of how Nova fits into a real Home Assistant deployment, see [docs/HA_INTEGRATION_OVERVIEW.md](docs/HA_INTEGRATION_OVERVIEW.md).

### 1. Copy the custom component

```bash
cp -r ha/custom_components/ai_avatar /config/custom_components/
```

### 2. Add to `configuration.yaml`

```yaml
ai_avatar:
  ai_server_url: http://<server-ip>:8001
  api_key: !secret ai_avatar_api_key
```

### 3. Add to `secrets.yaml`

```yaml
ai_avatar_api_key: <your-api-key>
```

### 4. Restart Home Assistant

### Available HA Services

| Service | Description |
|---|---|
| `ai_avatar.announce` | Speak a message on all configured speakers + avatar |
| `ai_avatar.chat` | Send text to Nova, fire result as `ai_avatar_chat_response` event |
| `ai_avatar.test_connection` | Fire a health check result event |

### Example automation

```yaml
automation:
  - alias: "Doorbell announcement"
    trigger:
      - platform: state
        entity_id: binary_sensor.doorbell
        to: "on"
    action:
      - service: ai_avatar.announce
        data:
          message: "Someone is at the front door."
          priority: alert
```

---

## Proactive Intelligence

Nova monitors your home autonomously via two independent services:

### ProactiveService (cloud LLM triage)
Watches structural state changes — locks, covers, alarms, binary sensors, climate — and batches them every 60 seconds. Asks the active LLM (Gemini / GPT / Claude / Ollama) to decide if anything warrants a spoken announcement. Also handles:
- **Camera motion** — fetches a snapshot and describes what it sees (delivery detection, driveway alerts)
- **Weather changes** — announces significant condition changes (rain, lightning, fog)
- **Daily forecast** — morning weather briefing at 7 AM
- **Heating control** — evaluates room temperatures and presence every 30 min, adjusts Hive boiler via HA tool calls

### SensorWatchService (always-local Ollama)
Watches `sensor.*` entities using **only the local Ollama `gemma2:9b` model** — never the active cloud LLM. Zero cloud cost.

**Immediate threshold announcements:**

| Sensor | Condition | Message |
|--------|-----------|---------|
| Any battery sensor | < 10% | Low battery alert |
| Room temperature | > 32°C or < 10°C | Temperature warning |
| Fridge compressor power | < 5 W (stopped) or > 400 W | Fridge fault alert |
| Car fuel level | < 15% | Low fuel reminder |
| Bin collection days | = 1 (tomorrow) | Bin reminder |

**Periodic snapshot review (every 30 min):** Ollama receives a snapshot of all temperature, humidity, power, battery, energy, and monetary sensors and decides if anything is noteworthy — high daily energy cost, poor humidity, low batteries not yet caught by the immediate path, etc.

Cooldowns: 2 h per entity, 15 min global, 1 h between snapshot-review announcements.

---

## API Reference

All endpoints (except `/health/public` and `/avatar`, `/admin`) require the `X-API-Key` header.

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/health/public` | Liveness probe — no auth |
| `GET` | `/health` | Full component status (ollama, whisper, piper, HA) |
| `POST` | `/chat` | Text chat — returns JSON response |
| `WS` | `/ws/voice` | Voice pipeline: WAV in → transcript + LLM + TTS audio out |
| `WS` | `/ws/avatar` | State broadcast (idle / listening / thinking / speaking) |
| `POST` | `/announce` | HA-triggered TTS announcement |
| `POST` | `/stt/wake` | Wake word check via Whisper tiny model |
| `GET` | `/avatar` | 3D avatar page |
| `GET` | `/admin` | Admin panel |

---

## Voice Pipeline

```
Browser mic → WAV → /ws/voice
  ├─ faster-whisper STT          → transcript
  ├─ LLM (Ollama / cloud)        → response text  [+ HA tool calls]
  │   ├─ get_entities(domain)    → discover entity IDs
  │   ├─ get_entity_state(id)    → read sensor/device value
  │   └─ call_ha_service(...)    → ACL check → HA REST API
  ├─ Piper TTS                   → WAV + word timings
  ├─ WS: {"type":"word_timings"} → sent to browser first
  ├─ WS: <WAV bytes>             → sent to browser
  └─ SpeakerService              → Echo/Sonos concurrent playback

Browser: head.speakAudio({audio, words, wtimes, wdurations})
       → TalkingHead phoneme lip sync
```

---

## Entity Access Control (ACL)

Edit `config/acl.yaml` (or use the admin panel) to restrict which HA entities and services Nova can access:

```yaml
# Full unrestricted access (default)
version: 1
rules:
  - domain: "*"
    entities: "*"
    services: "*"

# Restricted example — lights and media only
version: 1
rules:
  - domain: light
    entities: "*"
    services: [turn_on, turn_off, toggle]
  - domain: media_player
    entities: [media_player.living_room, media_player.bedroom]
    services: [media_play, media_pause, volume_set]
```

---

## Avatar Customisation

### Skin tone

Select in the admin panel under **Avatar** → skin tone swatches. Five presets:

| Index | Name | Hex |
|---|---|---|
| 0 | Porcelain | `#fdebd0` |
| 1 | Light | `#f1c27d` |
| 2 | Medium | `#c68642` |
| 3 | Dark | `#8d5524` |
| 4 | Deep | `#4a2c0a` |

Applied directly to Three.js `MeshStandardMaterial.color` on skin meshes only (`Wolf3D_Skin`, `Wolf3D_Body`) — hair, eyes, and clothing are unaffected.

### Custom avatar model

Set a custom `.glb` URL in the admin panel under **Avatar** → Avatar URL. Any [Ready Player Me](https://readyplayer.me/) avatar URL works.

### System prompt

Edit Nova's personality and capabilities under **Admin → Prompt**. Changes take effect on the next conversation (no restart needed).

---

## Project Structure

```
nova-avatar/
├── install.sh                    # One-touch installer
├── package.sh                    # Creates distributable archive
├── docker-compose.yml            # Ollama container
├── requirements.txt
│
├── avatar_backend/               # FastAPI application
│   ├── main.py                   # App factory + lifespan
│   ├── config.py                 # Pydantic-settings
│   ├── routers/
│   │   ├── admin.py              # Admin panel API
│   │   ├── announce.py           # POST /announce
│   │   ├── avatar_ws.py          # WS /ws/avatar
│   │   ├── chat.py               # POST /chat
│   │   ├── health.py             # GET /health
│   │   └── voice.py              # WS /ws/voice
│   ├── services/
│   │   ├── chat_service.py         # LLM + tool call loop
│   │   ├── ha_proxy.py             # HA REST client + ACL
│   │   ├── llm_service.py          # Multi-provider LLM (Ollama/OpenAI/Gemini/Anthropic + fallback)
│   │   ├── proactive_service.py    # HA state monitor → cloud LLM triage → announce
│   │   ├── sensor_watch_service.py # sensor.* monitor → local Ollama only → announce
│   │   ├── session_manager.py      # Conversation history
│   │   ├── speaker_service.py      # Echo + Sonos playback
│   │   ├── stt_service.py          # faster-whisper STT
│   │   ├── tts_service.py          # Piper TTS + word timings
│   │   └── ws_manager.py           # WebSocket connection registry
│   ├── models/
│   │   ├── acl.py                # ACL rule models
│   │   ├── messages.py           # Pydantic message schemas
│   │   └── tool_result.py
│   └── middleware/
│       └── auth.py               # API key validation
│
├── static/
│   ├── avatar.html               # TalkingHead 3D avatar page
│   ├── admin.html                # Admin panel UI
│   ├── avatars/
│   │   └── brunette.glb          # Default 3D avatar model
│   └── nova_ha_package.yaml      # HA package template
│
├── config/
│   ├── system_prompt.txt         # Nova's personality
│   └── acl.yaml                  # Entity access control
│
├── ha/
│   └── custom_components/
│       └── ai_avatar/            # HA custom component
│
├── scripts/
│   ├── download_piper.sh
│   └── download_piper_voice.sh
│
└── tests/
```

---

## Updating

```bash
# Pull latest changes
git -C /opt/avatar-server pull

# Apply update (resyncs Docker compose, refreshes Ollama / Intron sidecars, restarts service automatically)
sudo /opt/avatar-server/install.sh --update
```

---

## Useful Commands

```bash
# Live logs
journalctl -u avatar-backend -f

# Restart service
systemctl restart avatar-backend

# Check status
systemctl status avatar-backend

# Start Ollama (if stopped)
docker compose -f /opt/avatar-server/docker-compose.yml up -d

# Pull a different LLM model
docker exec avatar_ollama ollama pull mistral:7b
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Avatar page blank | Check browser console — likely API key missing in URL (`?api_key=...`) |
| "Mic Blocked" on avatar page | Allow microphone in browser site settings |
| No audio from speakers | Check `SPEAKERS` in `.env` and that Alexa Media Player integration is installed |
| LLM not responding | Check Ollama container: `docker ps` / `docker logs avatar_ollama` |
| HA service calls failing | Check `config/acl.yaml` — entity may be restricted |
| Whisper transcription empty | Audio may be too quiet — check mic levels |

---

## Credits

- [TalkingHead](https://github.com/met4citizen/TalkingHead) — 3D avatar lip sync
- [Piper TTS](https://github.com/rhasspy/piper) — local neural text-to-speech
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) — fast local speech recognition
- [Ollama](https://ollama.com) — local LLM inference
- [Home Assistant](https://www.home-assistant.io) — smart home platform

---

## License

MIT
