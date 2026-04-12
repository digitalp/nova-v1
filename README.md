# Nova V1 — AI Home Assistant

> A fully local, privacy-first AI home assistant with wake-word voice control, 3D lip-synced avatar, proactive home monitoring, and deep Home Assistant integration.

---

## What is Nova?

Nova is a self-hosted AI assistant that runs entirely on your local network. She listens for her name, understands natural language, controls your Home Assistant devices, and responds through a 3D lip-synced avatar and/or your physical speakers (Amazon Echo, Sonos, Google Home).

No mandatory cloud. Everything can run on your own hardware using local Ollama models. Cloud LLM providers (Gemini, OpenAI, Anthropic) are supported as opt-in upgrades.

---

## Key Features

### Voice & Conversation
- Wake word ("Nova") with VAD — always-on, hands-free activation
- Push-to-talk mode as an alternative
- Full multi-turn conversation with session memory
- Interruption support — a new utterance cancels the in-flight response
- Long-term memory — Nova remembers facts about your home across restarts

### 3D Avatar
- Real-time lip-synced 3D avatar powered by [TalkingHead](https://github.com/met4citizen/TalkingHead)
- Phoneme-level lip sync using Piper TTS native word timings
- Customisable background — solid colour picker or image URL
- Avatar library — upload and switch `.glb` models from the admin panel
- Skin tone presets applied directly to Three.js materials

### LLM — Multi-Provider with Local Fallback
- Primary: Ollama (local, GPU-accelerated) — default and always-available
- Cloud opt-in: Google Gemini, OpenAI GPT, Anthropic Claude — switch via a single env var
- Automatic cloud → local Ollama fallback when cloud is unavailable or rate-limited
- Background tasks (sensor watch, heating shadow, proactive triage) always route to local Ollama — zero cloud cost for ambient intelligence

### Home Assistant Integration
- Controls lights, climate, locks, covers, media players via natural language
- ACL-gated access — fine-grained per-entity, per-service allow/deny rules
- Reads sensor states, forecasts, energy usage, presence, car status
- HA automations can push events to Nova (`ai_avatar.announce`, `ai_avatar.chat`)
- Heating autonomous control — evaluates room temperatures and schedules boiler via Hive

### Proactive Intelligence (Zero Input)
**ProactiveService** — monitors structural HA state changes every 60 s via WebSocket. Uses the active cloud LLM (or Ollama fallback) to decide if anything warrants a spoken announcement. Handles:
- Camera motion with AI vision description (delivery detection, driveway alerts)
- Doorbell visitor triage with live camera popup on avatar page
- Parcel delivery and follow-up reminders
- Weather change alerts and daily 7 AM forecast
- Autonomous heating evaluation and boiler control (every 30 min)
- House attention summary (open doors, active alarms, etc.)

**SensorWatchService** — watches all `sensor.*` entities via HA WebSocket using **only local Ollama** — never the cloud LLM:
- Immediate threshold alerts: battery < 10%, room temp extremes, fridge fault, low car fuel, bin collection day
- Periodic 30-min snapshot review: Ollama evaluates all temperature, humidity, power, battery, energy sensors and announces anything noteworthy
- Per-entity 2 h cooldown, 15 min global cooldown

**Heating Shadow** — a local-only Ollama pass that mirrors every cloud heating evaluation without executing any actions. Logged to the AI Decisions panel as SUCCEEDED / FAILED for auditability.

### TTS — Three Engines
| Engine | Description |
|--------|-------------|
| **Piper** | Local neural TTS with native word-timing data for lip sync. Fully offline. |
| **Intron Afro TTS** | On-device neural voice sidecar — high quality, low latency, GPU-accelerated |
| **Echo (SSML)** | Native Alexa SSML playback through Alexa Media Player. Zero extra hardware. |

### Speaker Routing
- Broadcasts to any mix of Amazon Echo, Sonos, and Google Cast speakers simultaneously
- Area-aware routing — route audio to specific rooms or zones
- Echo devices auto-detected by entity ID or forced with `alexa:` prefix
- Configurable audio offset for speaker sync

### Admin Panel
Full web UI — no SSH needed for day-to-day management:
- **Dashboard** — live system metrics (CPU, RAM, GPU), Ollama status, health indicators
- **AI Decisions log** — real-time stream of every triage, tool call, chat response, sensor event, and heating shadow evaluation with LLM badges
- **Find Anything** — AI-powered natural language search over archived motion clips. UniFi-style evidence console with camera rail, day/event-type grouping, keyboard navigation, lazy video loading
- **LLM Cost log** — per-model token and cost tracking with daily/monthly charts
- **Server Logs** — searchable structured log stream
- **System Prompt** — edit Nova's personality and capabilities live
- **ACL editor** — manage entity access rules
- **Memory editor** — view, pin, and delete long-term memories
- **Avatar library** — upload and switch 3D models
- **Settings** — TTS provider, voice, speakers, background, environment config
- **Light / Dark mode** — Dribbble-inspired UI with full theme support

### Motion Clip Archive
- Camera motion events archive short video clips instead of just announcing
- AI-described clips stored in SQLite with camera, timestamp, description
- Natural language search: "package at door", "person near driveway"
- Pre-set queries, camera filter dropdown, event-type filter
- Admin review modal with Previous / Next navigation and keyboard shortcuts
- Lazy-loaded video for fast initial render

---

## Architecture

```
Browser / HA Dashboard
        │
        ├── avatar.html  (TalkingHead 3D + voice WS client)  ←→  /ws/voice, /ws/avatar
        └── admin.html   (admin panel)                       ←→  REST /admin/*

Home Assistant
        ├── custom_component: ai_avatar                      ←→  POST /announce, /chat
        └── automation triggers → rest_command.nova_*

AI Server  (FastAPI, port 8001)
        ├── STT:        faster-whisper (local, GPU or CPU)
        ├── TTS:        Piper / Intron Afro TTS / Echo SSML
        ├── LLM:        Ollama (local) + Gemini / GPT / Claude (opt-in cloud)
        ├── ProactiveService   — HA WS monitor → cloud LLM triage → announce
        ├── SensorWatchService — sensor.* monitor → local Ollama only → announce
        ├── HeatingService     — autonomous boiler control (cloud LLM + local shadow)
        ├── MotionClipService  — ffmpeg snapshot capture → AI description → archive
        ├── MetricsDB          — SQLite: LLM costs, motion clips, memories, decision log
        └── SpeakerService     — Echo SSML + HA TTS concurrent broadcast

Ollama  (local, Docker, GPU-accelerated)
        ├── Primary chat model  (qwen2.5:7b / llama3.1:8b / configured)
        ├── Vision model        (llama3.2-vision:11b for camera descriptions)
        └── Background model    (gemma2:9b — sensor watch, shadow, fallback)
```

---

## What Changed in the Last 5 Days

### Admin Panel — Full Redesign
The admin panel received a complete visual and functional overhaul:
- **Dribbble-inspired UI** with cohesive dark/light mode — unified glass-effect cards, consistent typography (Dribbble sans-serif stack), refined sidebar logo
- **Historical charts** — CPU, RAM, GPU usage plotted over time; LLM cost daily bar charts
- **Terminal-style logs** — dark monospace log stream with level-coloured badges
- **Find Anything** — motion clip search now lazy-loads videos (no hang on large archives), complete light mode coverage across all components
- **Light mode** — comprehensive visibility pass across motion cards, modals, search controls, sidebar, and dashboard

### Intron Afro TTS — New Voice Engine
- New on-device neural TTS sidecar (`intron_afro_tts`) with GPU acceleration
- Voice selection exposed in admin Settings panel
- AfroTTS suppression during Echo SSML playback (no double-speak)
- Installer updated to optionally set up the AfroTTS sidecar and its reference audio

### SSML Audio for Echo / Alexa
- Echo speakers now use native Alexa SSML (`<speak>` tags) for better prosody
- SSML generation integrated into the speaker routing pipeline
- Cloudflare tunnel public URL surfaced in `.env` and config for external SSML audio delivery

### Speaker Routing — Area-Aware
- Area-aware speaker routing controls: map room/zone names to specific `media_player` entities
- Speaker playback routing bug fixed (AfroTTS audio was incorrectly broadcasting)
- Smooth browser-side audio playback restored after a regression

### Avatar Background Customisation
- Color picker for solid background colours on the avatar page
- Image URL field for custom background images
- Auto-boot path and save-state preservation fixed

### LLM Reliability
- **Resilient local fallback paths**: local Ollama retries with configurable delay before falling back to cloud; cloud → local fallback for all background tasks
- **Heating shadow evaluation**: every cloud-LLM heating decision is mirrored locally by Ollama without executing actions — visible in AI Decisions log with SUCCEEDED/FAILED badge
- **GPU-backed Ollama**: Docker Compose updated to use NVIDIA Container Toolkit; installer configures GPU runtime automatically
- Background tasks (sensor watch, heating, proactive triage) now always prefer local Ollama to avoid cloud spend
- Ollama chat history normalization fixes (tool-call format compatibility with gemma2, qwen2.5)
- Admin cost view persisted across restarts

### Proactive & Sensor Improvements
- Power alert cooldown — prevents rapid-fire fridge/appliance alerts
- Wake word VAD hardening — reduced false wakes, faster recovery after false check
- House attention summary now includes concrete issue text (e.g. "back door open")
- Guarded V1 issue auto-remediation — Nova can self-correct certain recurring issues
- Avatar clients auto-reconnect after backend startup

### Security & Stability
- HA tool routing hardened — domain/service denylist enforced, TTS calls blocked from LLM
- Warning noise reduced in sensor watch logs
- V1 chat history and spoken unit formatting fixed

---

## Requirements

**Server**
- Ubuntu 22.04 / 24.04 or Debian 12+ (x86_64)
- Python 3.10+
- Docker (for Ollama)
- NVIDIA GPU recommended — RTX 2060 or better for real-time inference; CPU also works

**Home Assistant**
- Home Assistant 2023.6+
- `ai_avatar` custom component (included in `ha/custom_components/`)
- Alexa Media Player integration (for Echo speakers)

---

## Quick Start

```bash
git clone https://github.com/digitalp/nova-v1.git
cd nova-v1
sudo ./install.sh
```

The installer prompts for your API key, HA URL/token, LLM provider, TTS engine, voice, and speakers, then:
1. Installs Python deps and downloads Piper + Whisper models
2. Configures Docker NVIDIA runtime if a GPU is present
3. Pulls the Ollama model(s)
4. Optionally sets up the Intron Afro TTS sidecar
5. Installs and starts `avatar-backend.service` (systemd)

**Open the avatar:**
```
http://<server-ip>:8001/avatar?api_key=<your-api-key>
```

**Open the admin panel:**
```
http://<server-ip>:8001/admin
```

---

## Configuration

All config lives in `/opt/avatar-server/.env`:

| Variable | Description | Default |
|---|---|---|
| `API_KEY` | Shared secret for all API requests | *(required)* |
| `HA_URL` | Home Assistant URL | *(required)* |
| `HA_TOKEN` | HA Long-Lived Access Token | *(required)* |
| `LLM_PROVIDER` | `ollama` / `google` / `openai` / `anthropic` | `ollama` |
| `OLLAMA_MODEL` | Local chat model | `qwen2.5:7b` |
| `OLLAMA_VISION_MODEL` | Vision model for camera descriptions | `llama3.2-vision:11b` |
| `CLOUD_MODEL` | Cloud model name | `gemini-2.5-flash` |
| `TTS_PROVIDER` | `piper` / `afrotts` / `elevenlabs` | `piper` |
| `AFROTTS_VOICE` | Intron Afro TTS voice ID | `am_adam` |
| `WHISPER_MODEL` | STT model size | `base` |
| `SPEAKERS` | Comma-separated HA `media_player` entity IDs | *(optional)* |
| `PUBLIC_URL` | Public HTTPS URL for SSML audio delivery (e.g. Cloudflare tunnel) | *(optional)* |
| `PORT` | Backend port | `8001` |

---

## Home Assistant Integration

### Install custom component
```bash
cp -r ha/custom_components/ai_avatar /config/custom_components/
```

### Add to `configuration.yaml`
```yaml
ai_avatar:
  ai_server_url: http://<server-ip>:8001
  api_key: !secret ai_avatar_api_key
```

### Available services
| Service | Description |
|---|---|
| `ai_avatar.announce` | Speak a message on all configured speakers + avatar |
| `ai_avatar.chat` | Send text to Nova; fires `ai_avatar_chat_response` event |
| `rest_command.nova_chat` | Direct REST alternative for automations |
| `rest_command.nova_announce` | Direct REST TTS trigger |

---

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Component status (ollama, whisper, piper, HA) |
| `POST` | `/chat` | Text chat — JSON response |
| `WS` | `/ws/voice` | Voice pipeline (WAV in → transcript + LLM + TTS audio out) |
| `WS` | `/ws/avatar` | State broadcast (idle / listening / thinking / speaking) |
| `POST` | `/announce` | TTS announcement from HA or external caller |
| `POST` | `/announce/doorbell` | Doorbell flow with camera snapshot |
| `POST` | `/announce/motion` | Motion event with AI clip archiving |
| `GET` | `/admin` | Admin panel |
| `GET` | `/avatar` | 3D avatar page |

---

## Project Layout

```
nova-v1/
├── install.sh                      # One-touch installer (Ubuntu/Debian)
├── docker-compose.yml              # Ollama GPU container
├── requirements.txt
│
├── avatar_backend/                 # FastAPI application
│   ├── main.py
│   ├── config.py
│   ├── routers/                    # admin, announce, avatar_ws, chat, health, voice
│   ├── services/
│   │   ├── llm_service.py          # Multi-provider LLM + fallback
│   │   ├── proactive_service.py    # HA state monitor + heating control
│   │   ├── sensor_watch_service.py # Local-only sensor monitoring
│   │   ├── motion_clip_service.py  # Video clip capture + AI description
│   │   ├── ha_proxy.py             # HA REST client + ACL
│   │   ├── speaker_service.py      # Echo SSML + HA TTS broadcast
│   │   ├── stt_service.py          # faster-whisper STT
│   │   ├── tts_service.py          # Piper / Intron Afro / Echo TTS
│   │   ├── metrics_db.py           # SQLite: costs, clips, memories, events
│   │   ├── persistent_memory.py    # Long-term household memory
│   │   └── session_manager.py      # Conversation history
│   └── models/
│
├── static/
│   ├── avatar.html                 # 3D avatar + voice client
│   └── admin.html                  # Admin panel
│
├── config/
│   ├── system_prompt.txt           # Nova personality
│   └── acl.yaml                    # Entity access control
│
├── ha/
│   └── custom_components/ai_avatar/
│
├── intron_afro_tts_sidecar/        # Optional on-device neural TTS
└── tests/
```

---

## Credits

- [TalkingHead](https://github.com/met4citizen/TalkingHead) — 3D avatar lip sync
- [Piper TTS](https://github.com/rhasspy/piper) — local neural text-to-speech
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) — fast local STT
- [Ollama](https://ollama.com) — local LLM inference
- [Home Assistant](https://www.home-assistant.io) — smart home platform

---

## License

MIT
