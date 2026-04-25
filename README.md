# Nova V1 — AI Home Assistant

> A fully local, privacy-first AI home assistant with wake-word voice control, 3D lip-synced avatar, proactive home monitoring, family chore scoreboard, parental controls, and deep Home Assistant integration.

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
- **Media shortcut path** — TV channel commands bypass the full prompt for lower latency

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
- When a known face is detected (confidence >= 0.65), Nova greets them by name with full lip sync
- Greeting is time-aware: *"Good morning/afternoon/evening, Jason!"*
- Automatically arms the mic after greeting so Nova listens for a reply
- Per-person 30-minute cooldown — never repetitive
- Device-specific: plays only on the device whose webcam saw the face
- Register faces from the admin panel (file upload or live webcam snap)
- Optional **DeepFace preprocessing** — aligns and crops training images before CPAI registration

### LLM — Multi-Provider with Local Fallback
- Primary: Ollama (local, GPU-accelerated) — default and always-available
- Cloud opt-in: Google Gemini, OpenAI GPT, Anthropic Claude — switch via a single env var
- Automatic cloud -> local Ollama fallback when cloud is unavailable or rate-limited
- **Gemini key pool** — round-robin rotation across multiple API keys for vision and operational tasks
- Background tasks always route to local Ollama — zero cloud cost for ambient intelligence

### Home Assistant Integration
- Controls lights, climate, locks, covers, media players via natural language
- ACL-gated access — fine-grained per-entity, per-service allow/deny rules
- Reads sensor states, forecasts, energy usage, presence, car status
- HA automations can push events to Nova (`ai_avatar.announce`, `ai_avatar.chat`)
- Heating autonomous control — evaluates room temperatures and schedules boiler via Hive
- **Live TV channel control** — tunes Channels DVR on Shield TVs by voice ("put on Sky Sports Premier League")

### Proactive Intelligence (Zero Input)
**ProactiveService** — monitors structural HA state changes every 60 s via WebSocket. Uses the active cloud LLM (or Ollama fallback) to decide if anything warrants a spoken announcement. Handles:
- Camera motion with AI vision description (delivery detection, driveway alerts)
- Doorbell visitor triage with live camera popup on avatar page
- Weather change alerts and daily 7 AM forecast
- Autonomous heating evaluation and boiler control (every 30 min)
- House attention summary (open doors, active alarms, etc.)

**SensorWatchService** — watches all `sensor.*` entities via HA WebSocket using **only local Ollama** — never the cloud LLM:
- Immediate threshold alerts: battery < 10%, room temp extremes, fridge fault, low car fuel
- Periodic 30-min snapshot review of all temperature, humidity, power, battery, energy sensors
- Per-entity 2 h cooldown, 15 min global cooldown

**Background Automation Loops** — always-on tasks requiring no user input:

| Loop | Schedule | Description |
|------|----------|-------------|
| Chore reminders | Per-task schedule | Announces if a due task hasn't been logged yet today |
| Kitchen watch | 15:30–19:30 daily | Checks kitchen camera for overflowing bin; reminds assigned member |
| Living room sweep | Weekdays 15:00–20:00 | Blue Iris PTZ sweep; LLM tidiness check; announces if untidy |
| Blind check | 20:00–21:00 daily | Checks camera; reminds household to close blinds every 5 min until closed |
| Morning digest | 07:30 daily | Personalised briefing per child: chores due, school day, allowance status |
| Daily chore summary | 20:00 daily | LLM summary of today's chores + weekly standings |
| Homework gate | Every 5 min (afternoon) | Restricts devices via MDM until homework chore is completed |
| Bedtime enforcement | Every 2 min (evening) | Soft warning then MDM device lock at configured bedtime |

**Heating Shadow** — a local-only Ollama pass mirroring every cloud heating evaluation. Logged to the AI Decisions panel as SUCCEEDED / FAILED for auditability.

### Family Chore Scoreboard *(optional — enabled by default)*
Gamified chore tracking with voice integration, camera verification, and a live leaderboard widget.

- **Voice logging** — "Nova, I emptied the kitchen bin" -> CPAI camera verification -> points awarded
- **Camera verification** — LLM inspects the relevant camera before awarding points
- **Cooldowns** — per-person cooldown prevents gaming (configurable per task)
- **Weekly leaderboard** — ranked scoreboard widget with face photos, medals, and points
- **Admin panel** — full task management: add/edit/delete tasks, manual awards, log history
- **Live scoreboard query** — ask Nova "Who's winning?" for a spoken answer
- **Point deductions** — penalise bad behaviour via admin UI or voice
- **Configurable penalties** — 7 default penalty types, all editable
- **Disable entirely** — set `SCOREBOARD_ENABLED=false` in `.env`

### Parental Controls *(optional — requires Headwind MDM)*
Full device management for children's Android phones and tablets:

- **App blocking/unblocking** — voice or admin: "Nova, block YouTube on Jason's phone"
- **Homework gate** — MDM device restriction triggered automatically when homework isn't done
- **Bedtime enforcement** — device lock at configured bedtimes; soft warning 10 minutes before
- **Exception/override queue** — child requests extra time; parent approves/denies from admin UI
- **Per-child state machine** — allowed / warned / grace_period / restricted / overridden
- **Real-time device location** — map view with address reverse-geocoding
- **Device alerts** — low battery, offline, location anomaly announced by Nova
- **LLM tools** — `get_enrolled_devices`, `block_app`, `unblock_app`, `send_device_message`, `get_device_location`
- **Enrollment** — QR code or APK download, both proxied through Nova

### Family Management
- **Typed family model** — people, devices, policies, and bedtime rules in `config/family_state.json`
- **Household forecast** — "What will happen to Joel's iPad at 8:30 PM?" (`get_household_forecast` tool)
- **Timeline view** — per-person restrictions, overrides, and chore events in admin UI
- **Audit log** — every MDM action logged with timestamp and reason

### TTS — Four Engines

| Engine | Description |
|--------|-------------|
| **Piper** | Local neural TTS with native word-timing data for lip sync. Fully offline. |
| **Intron Afro TTS** | On-device neural voice sidecar — high quality, low latency, GPU-accelerated |
| **ElevenLabs** | Cloud neural TTS — highest quality, requires API key |
| **Echo (SSML)** | Native Alexa SSML playback through Alexa Media Player. Zero extra hardware. |

### Speaker Routing
- Broadcasts to any mix of Amazon Echo, Sonos, and Google Cast speakers simultaneously
- Area-aware routing — route audio to specific rooms or zones
- Echo devices auto-detected by entity ID or forced with `alexa:` prefix

### Admin Panel
Full web UI — no SSH needed for day-to-day management:
- **Dashboard** — live system metrics, Ollama status, health indicators, entity discovery
- **AI Decisions log** — every triage, tool call, chat response, sensor event, and heating shadow evaluation
- **AI Vision** — natural language search over archived motion clips; UniFi-style evidence console
- **LLM Cost log** — per-model token and cost tracking with daily/monthly charts
- **Server Logs** — searchable structured log stream
- **System Prompt** — edit Nova's personality live
- **ACL editor** — entity access rules
- **Memory editor** — view, pin, search, and delete long-term memories; stale/expired memory management
- **Avatar library** — upload and switch 3D models; per-room assignment
- **Scoreboard** — tasks, awards, deductions, penalties, notifications
- **Faces** — register faces, view unknown queue, DeepFace settings
- **Parental** — enrolled devices, app blocking, location map, exception queue, per-child state
- **Energy** — HA energy dashboard integration
- **Settings** — TTS, LLM, speakers, Gemini key pool, environment config
- **Help & Tips** — auto-generated docs updated on each commit
- **Light / Dark mode**

---

## Architecture

```
Browser / HA Dashboard
        |
        +-- avatar.html  (TalkingHead 3D + voice WS client)  <-> /ws/voice, /ws/avatar
        |     +-- webcam -> /face/recognize -> /face/greet (per-device greeting)
        +-- admin.html   (admin panel)                       <-> REST /admin/*

Home Assistant
        +-- custom_component: ai_avatar                      <-> POST /announce, /chat
        +-- automation triggers -> rest_command.nova_*

AI Server  (FastAPI, port 8001)
        +-- STT:              faster-whisper (local, GPU or CPU)
        +-- TTS:              Piper / Intron Afro TTS / ElevenLabs / Echo SSML
        +-- LLM:              Ollama (local) + Gemini / GPT / Claude (opt-in cloud)
        |   +-- llm_service.py    -- provider routing, fallback, vision dispatch
        |   +-- llm_backends.py   -- _OllamaBackend, _GeminiBackend, _AnthropicBackend, ...
        |   +-- llm_vision.py     -- Gemini/Ollama/OpenAI image description helpers
        +-- ProactiveService      -- HA WS monitor -> cloud LLM triage -> announce
        |   +-- proactive_batch.py    mixin: batch triage, LLM fields, heating shadow loop
        |   +-- proactive_motion.py   mixin: motion event handling, phone push notifications
        |   +-- proactive_weather.py  mixin: weather alerts, daily forecast announcements
        +-- SensorWatchService    -- sensor.* monitor -> local Ollama only -> announce
        |   +-- sensor_snapshot.py    mixin: periodic LLM snapshot review
        +-- HAProxy               -- HA REST client, ACL enforcement, tool dispatch
        |   +-- ha_state_mixin.py     mixin: entity reads, call_service, state cache
        +-- HeatingController     -- autonomous boiler control (cloud LLM + local shadow)
        +-- ScoreboardService     -- chore tracking, points, penalties, leaderboard
        +-- FamilyService         -- typed family model, bedtime rules, MDM policy enforcement
        +-- MDMClient             -- Headwind MDM JWT auth, device/app control
        +-- FaceRecognitionService -- CPAI face ID, ALPR, YOLOv5 object detection
        +-- BlueIrisService       -- NVR snapshot fallback + PTZ control
        +-- Background loops      -- chore reminders, kitchen/LR watch, blind check,
        |                            morning digest, homework gate, bedtime enforcement
        +-- MotionClipService     -- ffmpeg snapshot capture -> AI description -> archive
        |   +-- clip_capture_mixin.py mixin: ffmpeg/polling capture, phash dedup, validity
        |   +-- clip_manage_mixin.py  mixin: search, ranking, cleanup, thumbnail backfill
        +-- MetricsDB             -- SQLite: LLM costs, motion clips, memories, events
        |   +-- metrics/              sub-package split by domain (llm_costs, events, logs…)
        +-- PersistentMemory      -- long-term household memory (embeddings + SQLite)
        +-- SpeakerService        -- Echo SSML + HA TTS concurrent broadcast
        +-- _shared_http.py       -- singleton httpx pool (max 20 conns, keep-alive 30s)

Ollama  (local, Docker, GPU-accelerated)
        +-- Primary chat model  (qwen2.5:7b / llama3.1:8b / configured)
        +-- Vision model        (llama3.2-vision:11b for camera descriptions)
        +-- Background model    (gemma2:9b -- sensor watch, shadow, fallback)

Headwind MDM  (optional, Docker on same host)
        +-- Android agent APK -- device enrollment via QR
        +-- REST API          -- app block/unblock, device push, location
        +-- PostgreSQL 15     -- device registry, config, app lists

CodeProject.AI  (optional, local Docker)
        +-- Face recognition    -- /v1/vision/face/recognize, /register, /list
        +-- YOLOv5 detection    -- /v1/vision/detection
        +-- ALPR                -- /v1/image/alpr

Blue Iris NVR  (optional, local)
        +-- Snapshot fallback   -- /image/{camera} (no auth from LAN)
        +-- PTZ control         -- JSON API cmd:ptz
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
- Whether to enable the family chore scoreboard

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
| `OLLAMA_LOCAL_TEXT_MODEL` | Override local model for text/triage paths | *(auto-select)* |
| `PROACTIVE_OLLAMA_MODEL` | Override local model for proactive/background paths | *(optional)* |
| `SENSOR_WATCH_OLLAMA_MODEL` | Override local model for sensor watch review | *(optional)* |
| `TTS_PROVIDER` | `piper` / `afrotts` / `elevenlabs` | `piper` |
| `AFROTTS_VOICE` | AfroTTS voice ID | `af_heart` |
| `ELEVENLABS_API_KEY` | ElevenLabs API key | *(optional)* |
| `ELEVENLABS_VOICE_ID` | ElevenLabs voice ID | *(optional)* |
| `WHISPER_MODEL` | STT model size | `small` |
| `SPEAKERS` | Comma-separated HA `media_player` entity IDs | *(optional)* |
| `PUBLIC_URL` | Public HTTPS URL for SSML audio delivery | *(optional)* |
| `PORT` | Backend port | `8001` |
| `SCOREBOARD_ENABLED` | Enable family chore scoreboard | `true` |
| `MOTION_VISION_PROVIDER` | `gemini` / `ollama` / `ollama_remote` | `gemini` |
| `HEATING_LLM_PROVIDER` | Heating evaluator provider | `gemini` |
| `GEMINI_API_KEYS` | Comma-separated Gemini key pool | *(optional)* |
| `BLUEIRIS_URL` | Blue Iris NVR base URL | *(optional)* |
| `BLUEIRIS_USER` | Blue Iris username | *(optional)* |
| `BLUEIRIS_PASSWORD` | Blue Iris password | *(optional)* |
| `CODEPROJECT_AI_URL` | CodeProject.AI base URL | *(optional)* |
| `INTRON_AFRO_TTS_URL` | Intron Afro TTS sidecar base URL | `http://127.0.0.1:8021` |
| `HMDM_URL` | Headwind MDM base URL | *(optional)* |
| `HMDM_LOGIN` | Headwind MDM admin username | `admin` |
| `HMDM_PASSWORD` | Headwind MDM admin password | *(optional)* |

Runtime camera/entity mappings live in `config/home_runtime.json` and take effect without a restart.
Family policies live in `config/family_state.json`.

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
| `GET` | `/health/ready` | Readiness — checks Ollama, HA, websocket state (no auth) |
| `GET` | `/health` | Full component status — requires `X-API-Key` |
| `GET` | `/ambient` | Time/weather payload for ambient display (no auth) |
| `POST` | `/chat` | Text chat — JSON response |
| `WS` | `/ws/voice` | Voice pipeline (WAV in -> transcript + LLM + TTS audio out) |
| `WS` | `/ws/avatar` | State broadcast (idle / listening / thinking / speaking) |
| `POST` | `/announce` | TTS announcement from HA or external caller |
| `POST` | `/announce/doorbell` | Doorbell flow with camera snapshot |
| `POST` | `/announce/motion` | Motion event with AI clip archiving |
| `POST` | `/face/recognize` | Submit JPEG frame; returns face names + confidence |
| `POST` | `/face/greet` | Submit `{name}`; returns `{wav_b64, word_timings, message}` |
| `GET` | `/admin` | Admin panel |
| `GET` | `/avatar` | 3D avatar page |

### Scoreboard endpoints
| Method | Path | Description |
|---|---|---|
| `GET` | `/admin/scoreboard` | Weekly leaderboard + recent logs + config |
| `GET/POST` | `/admin/scoreboard/config` | View / save scoreboard config |
| `POST/DELETE` | `/admin/scoreboard/tasks` | Add / remove tasks |
| `POST` | `/admin/scoreboard/log` | Manual point award |
| `GET/POST/PATCH/DELETE` | `/admin/scoreboard/penalties` | Manage penalty types |
| `POST` | `/admin/scoreboard/penalty` | Issue a point deduction |

### Parental control endpoints
| Method | Path | Description |
|---|---|---|
| `GET` | `/admin/parental/devices` | List enrolled MDM devices with status |
| `POST` | `/admin/parental/block-app` | Block an app by package name |
| `POST` | `/admin/parental/unblock-app` | Unblock an app |
| `GET` | `/admin/parental/locations` | Real-time device locations |
| `GET` | `/admin/parental/overrides` | Pending exception requests |
| `POST` | `/admin/parental/overrides/{id}/approve` | Approve exception |
| `POST` | `/admin/parental/overrides/{id}/deny` | Deny exception |
| `GET` | `/admin/parental/apk` | Proxy MDM agent APK download |
| `GET` | `/admin/parental/apk-qr` | Enrollment QR code PNG |

---

## Family Chore Scoreboard

### How points work
- Tasks have configurable points and cooldowns (e.g. Make Bed: 5pts, 16h cooldown)
- **Honour tasks** — awarded immediately on voice claim
- **Camera tasks** — Nova checks the relevant camera before awarding; rejected if not done
- Deductions stored as negative points; weekly leaderboard sums both

### Voice examples
```
"Nova, I made my bed"                        -> +5pts
"Nova, I emptied the kitchen bin"            -> camera check -> +10pts if verified
"Who is winning this week?"                  -> Nova reads the ranked leaderboard
"Nova, deduct points from Jason for lying"   -> -15pts
```

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

---

## Parental Controls

Requires [Headwind MDM](https://h-mdm.com/) running locally (Docker Compose included).

```bash
docker-compose -f docker-compose.parental.yml up -d
```

Set `HMDM_URL`, `HMDM_LOGIN`, `HMDM_PASSWORD` in `.env`. Go to **Admin -> Parental -> Enrollment** to get the device enrollment QR code.

**Homework gate** — if the "Homework" chore is not completed by 16:00 on school days, social apps are blocked via MDM until it is done.

**Bedtime enforcement** — at configured bedtime, Nova gives a 10-minute warning then locks the device. Children can request an exception ("Nova, can I have more time?") which goes into an override queue for parental approval.

---

## Face Recognition & Greeting

Requires [CodeProject.AI](https://www.codeproject.ai/) (`CODEPROJECT_AI_URL` in `.env`).

1. Go to **Admin -> Faces -> Train New Face** and upload or snap a photo
2. Any device with the avatar page will capture webcam frames every 12 seconds
3. Known faces trigger a time-aware greeting on that device only, with lip sync and auto-mic arm

---

## Project Layout

```
nova-v1/
+-- install.sh                          # One-touch installer (Ubuntu/Debian)
+-- docker-compose.yml                  # Ollama GPU container
+-- docker-compose.parental.yml         # Headwind MDM + PostgreSQL 15
+-- requirements.txt
|
+-- avatar_backend/                     # FastAPI application
|   +-- main.py
|   +-- config.py
|   +-- routers/
|   |   +-- announce.py                 # Core TTS announce + WAV/media endpoints
|   |   +-- announce_vision.py          # Doorbell, visual event, motion, package announce
|   |   +-- chat.py                     # Text chat endpoint
|   |   +-- health.py                   # Health + readiness probes
|   |   +-- voice.py                    # WebSocket voice pipeline
|   |   +-- avatar_ws.py                # Avatar state WebSocket
|   |   +-- admin/
|   |       +-- auth.py                 # Session login/logout
|   |       +-- config.py               # LLM/TTS/speaker config
|   |       +-- coral_admin.py          # Coral wake word training + Edge TPU
|   |       +-- dashboard.py            # Sessions, memory, avatar, prompt sync
|   |       +-- dashboard_data.py       # Conversations, energy, test announce, faces
|   |       +-- events.py               # AI decisions log, event history
|   |       +-- monitoring.py           # Metrics, logs, system info
|   |       +-- motion.py               # Motion clip archive + AI vision search
|   |       +-- parental.py             # MDM device management, app blocking
|   |       +-- parental_mdm_helpers.py # MDM constants + pure helper functions
|   |       +-- parental_family.py      # Override queue, family, timeline, policies
|   |       +-- scoreboard.py           # Chore scoring admin
|   |       +-- system.py               # Restart, tunnel, heating, camera discovery
|   |       +-- system_media.py         # Music, selfheal, Gemini pool, vision cameras, rooms
|   +-- services/
|   |   +-- llm_service.py              # Provider routing, fallback, vision dispatch
|   |   +-- llm_backends.py             # OllamaBackend, GeminiBackend, AnthropicBackend, ...
|   |   +-- llm_vision.py               # Gemini/Ollama/OpenAI image description helpers
|   |   +-- proactive_service.py        # HA WS monitor + orchestration (inherits 3 mixins)
|   |   +-- proactive_batch.py          # Batch triage, LLM fields, heating shadow loop
|   |   +-- proactive_motion.py         # Motion event handling, phone notifications
|   |   +-- proactive_weather.py        # Weather alerts, daily forecast announcements
|   |   +-- sensor_watch_service.py     # Real-time sensor threshold alerts (WS loop)
|   |   +-- sensor_snapshot.py          # Periodic LLM snapshot review (SensorSnapshotMixin)
|   |   +-- heating_controller.py       # Autonomous boiler control
|   |   +-- scoreboard_service.py       # Chore tracking, points, penalties, leaderboard
|   |   +-- family_service.py           # Typed family model, policies, state machine
|   |   +-- mdm_client.py               # Headwind MDM JWT auth + shared API helpers
|   |   +-- ha_parental_tools.py        # LLM tools for parental control
|   |   +-- ha_proxy.py                 # HA REST client + ACL + tool dispatch
|   |   +-- ha_state_mixin.py           # Entity reads, call_service, camera, state cache
|   |   +-- ha_tool_schemas.py          # HA tool definitions (all providers)
|   |   +-- face_recognition.py         # CPAI face ID, ALPR, YOLOv5
|   |   +-- blueiris_service.py         # NVR snapshot fallback + PTZ control
|   |   +-- persistent_memory.py        # Long-term household memory
|   |   +-- speaker_service.py          # Echo SSML + HA TTS broadcast
|   |   +-- stt_service.py              # faster-whisper STT
|   |   +-- tts_service.py              # Piper / AfroTTS / ElevenLabs / Echo TTS
|   |   +-- realtime_voice_service.py   # WebSocket voice turn orchestration
|   |   +-- voice_audio.py              # Audio send, STT streaming, PCM extraction
|   |   +-- voice_session.py            # Session lifecycle, turn context, adapter resolve
|   |   +-- voice_types.py              # Voice data-classes, Protocol, adapters, factory
|   |   +-- motion_clip_service.py      # Clip scheduling, capture_and_store (core)
|   |   +-- clip_capture_mixin.py       # FFmpeg/polling capture, validity check
|   |   +-- clip_manage_mixin.py        # Search, ranking, cleanup, thumbnail backfill
|   |   +-- prompt_bootstrap.py         # System prompt generation from HA states (public API)
|   |   +-- prompt_helpers.py           # Private rendering + HA-state analysis helpers
|   |   +-- home_runtime.py             # Runtime config dataclass
|   |   +-- conversation_service.py     # Turn handling, tool execution, memory injection
|   |   +-- music_service.py            # Music Assistant integration
|   |   +-- deepface_service.py         # DeepFace face preprocessing
|   |   +-- coral_wake_detector.py      # Coral TPU wake word detector (CPU fallback)
|   |   +-- gemini_key_pool.py          # Gemini API key rotation + per-camera pinning
|   |   +-- _shared_http.py             # Singleton httpx connection pool
|   |   +-- metrics/                    # SQLite persistence (split by domain)
|   |   |   +-- db.py                   # MetricsDB composition
|   |   |   +-- base.py                 # Connection management, schema, WAL
|   |   |   +-- llm_costs.py            # LLM invocation cost tracking
|   |   |   +-- motion_clips.py         # Motion clip archive
|   |   |   +-- memories.py             # Long-term household memories
|   |   |   +-- events.py               # Decision events log
|   |   |   +-- logs.py                 # Server log persistence
|   |   |   +-- system_samples.py       # CPU/RAM/disk/GPU metrics
|   |   |   +-- overrides.py            # Parental overrides
|   |   |   +-- child_states.py         # Child state machine
|   |   +-- metrics_db.py               # Re-export shim (backward compat)
|   |   +-- session_manager.py          # Conversation history
|   +-- bootstrap/
|       +-- background.py               # Background loops
|       +-- startup.py                  # Service wiring and container init
|       +-- shutdown.py                 # Graceful teardown
|
+-- static/
|   +-- avatar.html                     # 3D avatar + voice client + webcam face greeting
|   +-- admin.html                      # Admin panel
|
+-- config/
|   +-- system_prompt.txt               # Nova personality + capabilities
|   +-- acl.yaml                        # Entity access control rules
|   +-- family_state.json               # People, devices, bedtimes, policies (gitignored)
|   +-- home_runtime.json               # Camera/entity mappings (runtime, gitignored)
|   +-- scoreboard_config.json          # Tasks, penalties, member config
|
+-- models/
|   +-- coral/                          # Wake word TFLite models (trained via admin panel)
|
+-- ha/
|   +-- custom_components/ai_avatar/
|
+-- intron_afro_tts_sidecar/            # Optional on-device neural TTS
+-- docs/                               # Extended documentation
+-- tests/
```

---

## Requirements

**Server**
- Ubuntu 22.04 / 24.04 or Debian 12+ (x86_64)
- Python 3.10+
- Docker (for Ollama)
- NVIDIA GPU recommended — RTX 2060 or better; CPU also works

**Home Assistant**
- Home Assistant 2023.6+
- `ai_avatar` custom component (included in `ha/custom_components/`)
- Alexa Media Player integration (for Echo speakers)

**Optional**
- [CodeProject.AI](https://www.codeproject.ai/) — face recognition, object detection, ALPR
- Blue Iris NVR — higher-quality camera snapshots, PTZ control
- [Headwind MDM](https://h-mdm.com/) — Android parental controls (Docker Compose included)

---

## Credits

- [TalkingHead](https://github.com/met4citizen/TalkingHead) — 3D avatar lip sync
- [Piper TTS](https://github.com/rhasspy/piper) — local neural text-to-speech
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) — fast local STT
- [Ollama](https://ollama.com) — local LLM inference
- [Home Assistant](https://www.home-assistant.io) — smart home platform
- [CodeProject.AI](https://www.codeproject.ai/) — local face recognition and vision AI
- [Headwind MDM](https://h-mdm.com/) — open-source Android MDM

---

## License

MIT
