# Nova V1 — AI Home Assistant

> A fully local, privacy-first AI home assistant with wake-word voice control, 3D lip-synced avatar, proactive home monitoring, family chore scoreboard, and deep Home Assistant integration.

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
- **Per-room avatars** — each tablet/kiosk can load a different GLB

### Face Recognition & Greeting
- Avatar page captures webcam frames every 12 seconds
- Sends to [CodeProject.AI](https://www.codeproject.ai/) for GPU-accelerated face recognition
- When a known face is detected (confidence ≥ 0.72), Nova greets them by name with full lip sync
- Greeting is time-aware: *"Good morning/afternoon/evening, Jason!"*
- Automatically arms the mic after greeting so Nova listens for a reply
- Per-person 30-minute cooldown — never repetitive
- Device-specific: plays only on the device whose webcam saw the face
- Register faces from the admin panel (file upload or live webcam snap)

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

**Background Automation Loops** — always-on background tasks that require no user input:
| Loop | Schedule | Description |
|------|----------|-------------|
| Chore reminders | Per-task schedule | Reads task reminder times; announces if nobody logged it yet today |
| Kitchen watch | 15:30–19:30 daily | Checks kitchen camera for overflowing bin; reminds assigned member |
| Living room sweep | Weekdays 15:00–20:00 | Blue Iris PTZ sweep; LLM tidiness check; announces if untidy |
| Daily chore summary | 20:00 daily | LLM generates warm summary of today's chores + weekly standings |
| Blind check | 20:00–21:00 daily | Checks living room camera; reminds household to close blinds every 5 min until closed |

**Heating Shadow** — a local-only Ollama pass that mirrors every cloud heating evaluation without executing any actions. Logged to the AI Decisions panel as SUCCEEDED / FAILED for auditability.

### Family Chore Scoreboard *(optional — enabled by default)*
Gamified household chore tracking with voice integration, camera verification, and a live leaderboard widget.

- **Voice logging** — "Nova, I emptied the kitchen bin" → CPAI camera verification → points awarded
- **Camera verification** — LLM inspects the relevant camera (kitchen, living room) before awarding points
- **Cooldowns** — per-person cooldown prevents gaming (configurable per task)
- **Assignment** — tasks can be assigned to specific household members
- **Weekly leaderboard** — ranked scoreboard widget on the avatar page with face photos, medals, and points
- **Admin panel** — full task management: add/edit/delete tasks, manual point awards, log history
- **Live scoreboard query** — ask Nova *"Who's winning?"*, *"What did Jason do today?"* for spoken answers
- **Point deductions** — penalise bad behaviour via admin UI or voice (*"Nova, deduct points from Joel for lying"*)
- **Configurable penalties** — add/remove penalty types from the admin panel; 7 defaults included
- **Blind reminder names** — configure who gets the blind-close reminder from the admin panel
- **Disable entirely** — set `SCOREBOARD_ENABLED=false` in `.env` for households without children

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
- **Avatar library** — upload and switch 3D models; per-room GLB assignment
- **Scoreboard** — manage tasks, assign members, view logs, manual awards, issue deductions, configure penalties and notifications
- **Faces** — register known faces via file upload or live webcam snap; view unknown face queue
- **Settings** — TTS provider, voice, speakers, background, environment config
- **Light / Dark mode** — Dribbble-inspired UI with full theme support

---

## Architecture

```
Browser / HA Dashboard
        │
        ├── avatar.html  (TalkingHead 3D + voice WS client)  ←→  /ws/voice, /ws/avatar
        │     └── webcam → /face/recognize → /face/greet (per-device greeting)
        └── admin.html   (admin panel)                       ←→  REST /admin/*

Home Assistant
        ├── custom_component: ai_avatar                      ←→  POST /announce, /chat
        └── automation triggers → rest_command.nova_*

AI Server  (FastAPI, port 8001)
        ├── STT:        faster-whisper (local, GPU or CPU)
        ├── TTS:        Piper / Intron Afro TTS / Echo SSML
        ├── LLM:        Ollama (local) + Gemini / GPT / Claude (opt-in cloud)
        ├── ProactiveService       — HA WS monitor → cloud LLM triage → announce
        ├── SensorWatchService     — sensor.* monitor → local Ollama only → announce
        ├── HeatingService         — autonomous boiler control (cloud LLM + local shadow)
        ├── ScoreboardService      — chore tracking, points, penalties, leaderboard (optional)
        ├── FaceRecognitionService — CPAI face ID, ALPR, YOLOv5 object detection
        ├── BlueIrisService        — NVR snapshot fallback + PTZ control
        ├── Background loops       — chore reminders, kitchen watch, LR sweep, blind check
        ├── MotionClipService      — ffmpeg snapshot capture → AI description → archive
        ├── MetricsDB              — SQLite: LLM costs, motion clips, memories, decision log
        └── SpeakerService         — Echo SSML + HA TTS concurrent broadcast

Ollama  (local, Docker, GPU-accelerated)
        ├── Primary chat model  (qwen2.5:7b / llama3.1:8b / configured)
        ├── Vision model        (llama3.2-vision:11b for camera descriptions)
        └── Background model    (gemma2:9b — sensor watch, shadow, fallback)

CodeProject.AI  (optional, local Docker)
        ├── Face recognition    — /v1/vision/face/recognize, /register, /list
        ├── YOLOv5 detection    — /v1/vision/detection
        └── ALPR                — /v1/image/alpr

Blue Iris NVR  (optional, local)
        ├── Snapshot fallback   — /image/{camera} (no auth from LAN)
        └── PTZ control         — JSON API cmd:ptz
```

---

## Quick Start

```bash
git clone https://github.com/digitalp/nova-v1.git
cd nova-v1
sudo ./install.sh
```

The installer prompts for:
- Home Assistant URL and token
- LLM provider (Ollama / Gemini / OpenAI / Anthropic)
- TTS engine and voice
- Speakers (HA media_player entity IDs)
- Whether to enable the **family chore scoreboard** (`Y/n`)

Then automatically:
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
| `OLLAMA_BACKGROUND_MODEL` | Local model used by background loops (sensor watch, heating shadow, daily summary) — never routed to cloud | `gemma2:9b` |
| `TTS_PROVIDER` | `piper` / `afrotts` / `elevenlabs` | `piper` |
| `AFROTTS_VOICE` | Intron Afro TTS voice ID | `am_adam` |
| `WHISPER_MODEL` | STT model size | `base` |
| `SPEAKERS` | Comma-separated HA `media_player` entity IDs | *(optional)* |
| `PUBLIC_URL` | Public HTTPS URL for SSML audio delivery (e.g. Cloudflare tunnel) | *(optional)* |
| `PORT` | Backend port | `8001` |
| `SCOREBOARD_ENABLED` | Enable family chore scoreboard | `true` |
| `BLUEIRIS_URL` | Blue Iris NVR base URL (e.g. `http://192.168.0.33:81`) | *(optional)* |
| `BLUEIRIS_USER` | Blue Iris username | *(optional)* |
| `BLUEIRIS_PASSWORD` | Blue Iris password | *(optional)* |
| `CODEPROJECT_AI_URL` | CodeProject.AI base URL (e.g. `http://192.168.0.33:32168`) | *(optional)* |

Runtime camera/entity mappings that change frequently live in `config/home_runtime.json` (not in `.env`) and take effect without a restart.

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
| `GET` | `/health/live` | Liveness — always 200 once the process is up (no auth) |
| `GET` | `/health/ready` | Readiness — checks Ollama reachability + HA connection (no auth) |
| `GET` | `/health` | Full component status — requires `X-API-Key` header |
| `POST` | `/chat` | Text chat — JSON response |
| `WS` | `/ws/voice` | Voice pipeline (WAV in → transcript + LLM + TTS audio out) |
| `WS` | `/ws/avatar` | State broadcast (idle / listening / thinking / speaking) |
| `POST` | `/announce` | TTS announcement from HA or external caller |
| `POST` | `/announce/doorbell` | Doorbell flow with camera snapshot |
| `POST` | `/announce/motion` | Motion event with AI clip archiving |
| `POST` | `/face/recognize` | Submit JPEG frame; returns recognized face names + confidence |
| `POST` | `/face/greet` | Submit `{name}`; returns `{wav_b64, word_timings, message}` greeting |
| `GET` | `/admin` | Admin panel |
| `GET` | `/avatar` | 3D avatar page |

### Scoreboard endpoints *(when SCOREBOARD_ENABLED=true)*
| Method | Path | Description |
|---|---|---|
| `GET` | `/admin/scoreboard` | Weekly leaderboard + recent logs + config |
| `GET/POST` | `/admin/scoreboard/config` | View / save scoreboard config |
| `PATCH` | `/admin/scoreboard/tasks/{id}` | Edit task fields |
| `POST/DELETE` | `/admin/scoreboard/tasks` | Add / remove tasks |
| `POST` | `/admin/scoreboard/log` | Manual point award (admin) |
| `DELETE` | `/admin/scoreboard/logs/{id}` | Delete a log entry |
| `GET/POST/PATCH/DELETE` | `/admin/scoreboard/penalties` | Manage penalty types |
| `POST` | `/admin/scoreboard/penalty` | Issue a point deduction |
| `GET/PATCH` | `/admin/scoreboard/notifications` | View / update blind reminder config |

---

## Family Chore Scoreboard

Enable with `SCOREBOARD_ENABLED=true` (default). Disable for households without children with `SCOREBOARD_ENABLED=false`.

### How points work
- Tasks have configurable points and cooldowns (e.g. Make Bed: 5pts, 16h cooldown)
- **Honour tasks** — awarded immediately on voice claim
- **Camera tasks** — Nova checks the relevant camera before awarding points; if the LLM says it's not done, she tells them to try again
- Deductions are stored as negative points; `SUM(points)` in the weekly leaderboard handles both automatically

### Voice examples
```
"Nova, I made my bed"              → +5pts for Make Bed (honour)
"Nova, I emptied the kitchen bin"  → camera check → +10pts if verified
"Who is winning this week?"        → Nova reads out the ranked leaderboard
"Nova, deduct points from Jason for lying"  → -15pts for Lying
```

### Configuring tasks
All tasks, points, cooldowns, camera assignments, and member assignments are editable from the **Admin Panel → Scoreboard** without restarting Nova.

### Penalty types (defaults)
| Behaviour | Deduction |
|---|---|
| Rude Behaviour | -10 pts |
| Lying | -15 pts |
| Disobedience | -10 pts |
| Fighting / Aggression | -20 pts |
| Bad Language | -10 pts |
| Disrespect to Adults | -15 pts |
| Damaging Property | -20 pts |

All configurable from **Admin Panel → Scoreboard → Penalty Types**.

---

## Face Recognition & Greeting

Requires [CodeProject.AI](https://www.codeproject.ai/) running locally (set `CODEPROJECT_AI_URL` in `.env`).

### Register faces
Go to **Admin Panel → Faces → Train New Face**. Upload a photo or snap directly from the webcam.

### How greetings work
Once faces are registered, any device with the avatar page open will:
1. Capture a webcam frame every 12 seconds
2. POST to `/face/recognize` — CPAI returns name + confidence
3. If a known face is found and 30 minutes have passed since last greeting:
   - Nova synthesizes a time-aware greeting with full word timings
   - Audio plays on **that device only** — not through house speakers
   - Avatar animates with lip sync driven by word timings
   - Mic arms automatically so Nova listens for a response

### Blue Iris integration
When `BLUEIRIS_URL` is configured, background loops (living room sweep, blind check) prefer Blue Iris snapshots for higher quality and lower latency. PTZ control is also available when `BLUEIRIS_USER` and `BLUEIRIS_PASSWORD` are set.

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
│   │   ├── llm_service.py          # Multi-provider LLM + fallback + HA tools
│   │   ├── proactive_service.py    # HA state monitor + heating control
│   │   ├── sensor_watch_service.py # Local-only sensor monitoring
│   │   ├── scoreboard_service.py   # Chore tracking, points, penalties, leaderboard
│   │   ├── face_recognition.py     # CodeProject.AI face ID, ALPR, YOLOv5
│   │   ├── blueiris_service.py     # NVR snapshot fallback + PTZ control
│   │   ├── home_runtime.py         # Runtime config dataclass (cameras, entities)
│   │   ├── motion_clip_service.py  # Video clip capture + AI description
│   │   ├── ha_proxy.py             # HA REST client + ACL + LLM tool dispatch
│   │   ├── speaker_service.py      # Echo SSML + HA TTS broadcast
│   │   ├── stt_service.py          # faster-whisper STT
│   │   ├── tts_service.py          # Piper / Intron Afro / Echo TTS
│   │   ├── metrics_db.py           # SQLite: costs, clips, memories, events
│   │   ├── persistent_memory.py    # Long-term household memory
│   │   └── session_manager.py      # Conversation history
│   └── bootstrap/
│       ├── background.py           # Background loops (reminders, camera watch, blind check)
│       └── startup.py              # Service wiring and container init
│
├── static/
│   ├── avatar.html                 # 3D avatar + voice client + webcam face greeting
│   └── admin.html                  # Admin panel
│
├── config/
│   ├── system_prompt.txt           # Nova personality
│   ├── acl.yaml                    # Entity access control
│   ├── home_runtime.json           # Camera/entity mappings (runtime, not in git)
│   └── scoreboard_config.json      # Tasks, penalties, member config
│
├── ha/
│   └── custom_components/ai_avatar/
│
├── intron_afro_tts_sidecar/        # Optional on-device neural TTS
└── tests/
```

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

**Optional**
- [CodeProject.AI](https://www.codeproject.ai/) — face recognition, object detection, ALPR
- Blue Iris NVR — higher-quality camera snapshots, PTZ control

---

## Credits

- [TalkingHead](https://github.com/met4citizen/TalkingHead) — 3D avatar lip sync
- [Piper TTS](https://github.com/rhasspy/piper) — local neural text-to-speech
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) — fast local STT
- [Ollama](https://ollama.com) — local LLM inference
- [Home Assistant](https://www.home-assistant.io) — smart home platform
- [CodeProject.AI](https://www.codeproject.ai/) — local face recognition and vision AI

---

## License

MIT
