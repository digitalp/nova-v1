# Nova V1 — User Manual

> Your AI home assistant that listens, sees, thinks, and speaks.

---

## What is Nova?

Nova is an AI assistant that lives on a small server in your home. She connects to your Home Assistant smart home system and can:

- **Talk to you** — say "Nova" and ask anything about your home
- **Control your home** — "turn off the living room lights", "set heating to 20 degrees"
- **Watch your cameras** — describes who's at the door, detects deliveries, archives motion clips
- **Alert you proactively** — "the back door has been open for 10 minutes", "bin collection tomorrow"
- **Show a 3D avatar** — a lip-synced character that moves when Nova speaks

Everything runs on YOUR hardware. Your conversations never leave your network unless you choose to use a cloud AI provider.

---

## Getting Started

### First-Time Setup

After installation, open your browser and go to:

```
http://<your-server-ip>:8001/admin
```

You'll be asked to create an admin account. This is your login for the admin panel — pick a strong password.

### The Admin Panel

The admin panel is your control centre. It has these sections:

| Section | What it does |
|---------|-------------|
| **Dashboard** | System health, CPU/RAM/GPU usage, Ollama status |
| **AI Decisions** | Live feed of every decision Nova makes — triage, announcements, heating |
| **AI Vision** | Search and browse archived camera motion clips |
| **LLM Costs** | Track token usage and costs if using cloud AI |
| **Server Logs** | Searchable log stream for debugging |
| **Settings** | Change AI provider, voice, speakers, background |
| **System Prompt** | Edit Nova's personality and knowledge |
| **ACL** | Control which Home Assistant devices Nova can access |
| **Memory** | View and manage facts Nova remembers about your household |
| **Music** | Search and play music on your speakers |

### The Avatar Page

Open the avatar in a browser or embed it in a Home Assistant dashboard:

```
http://<your-server-ip>:8001/avatar
```

The first time, you'll need to add `?api_key=YOUR_KEY` to the URL. After that, it's saved in a secure cookie — you won't need it again.

---

## Talking to Nova

### Voice (Hands-Free)

1. Open the avatar page in a browser on any device
2. Say **"Nova"** — the avatar will start listening (blue glow)
3. Ask your question or give a command
4. Nova thinks (purple glow), then speaks the answer

**Tips:**
- Speak naturally — "what's the weather like?" works as well as "get weather forecast"
- You can interrupt Nova while she's speaking — just say "Nova" again
- Nova remembers your conversation for the session — you can ask follow-up questions

### Text (Admin Panel)

1. Open the admin panel → **Chat**
2. Type your message and press Send
3. Nova's response appears as a chat bubble
4. Tool calls (device lookups, service calls) are shown inline
5. Conversation persists for the session

You can also send text via Home Assistant automations using the `ai_avatar.chat` service, or via the REST API:

```bash
curl -X POST http://<server-ip>:8001/chat \
  -H "X-API-Key: YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{"text": "What is the temperature outside?", "session_id": "my-session"}'
```

---

## What Can Nova Do?

### Control Devices

Just ask naturally:

- "Turn on the kitchen lights"
- "Set the living room to 22 degrees"
- "Close the garage door"
- "Play some jazz in the living room" (requires Music Assistant)

**What Nova CAN'T do (by design):**
- Unlock doors or disarm alarms (blocked by ACL for safety)
- Run shell commands or scripts
- Stop or restart Home Assistant

### Answer Questions

- "What's the temperature in the bedroom?"
- "Is the car locked?"
- "How much power are we using?"
- "When's the next train to Manchester?"
- "What's the weather forecast for tomorrow?"

### Camera & Security

Nova watches your cameras for motion and can:

- **Describe what she sees** — "A person in a dark jacket approaching the front door"
- **Detect deliveries** — "DELIVERY: Amazon" triggers special handling
- **Archive video clips** — searchable in AI Vision
- **Alert you** — announces on speakers and/or sends phone notifications

**How it works:**
1. Camera motion sensor triggers
2. Coral Edge TPU pre-filters (is it a person/vehicle or just wind?)
3. AI vision model describes the scene
4. If noteworthy → announces on speakers and archives clip
5. If nothing interesting → clip is discarded

### Proactive Alerts

Nova monitors your home and speaks up when something needs attention:

- **Bin collection** — "Don't forget, the black bin goes out tomorrow"
- **Weather changes** — "It's started raining — you may want to close the windows"
- **Daily forecast** — 7 AM weather briefing
- **Sensor alerts** — low car fuel, fridge fault, unusual temperatures
- **Door/window left open** — alerts during night hours
- **Home Assistant updates** — "Updates available for 3 integrations"

### Long-Term Memory

Nova remembers facts about your household across restarts:

- "The cat's name is Luna"
- "Penn is allergic to nuts"
- "The WiFi password is on the fridge"

You can view, pin, or delete memories in the admin panel under **Memory**.

---

## Configuration

### Changing the AI Provider

Nova supports multiple AI backends. Change in **Settings** or `.env`:

| Provider | Set `LLM_PROVIDER` to | Needs | Best for |
|----------|----------------------|-------|----------|
| **Ollama** (default) | `ollama` | Local GPU | Privacy, zero cost |
| **Google Gemini** | `google` | `GOOGLE_API_KEY` | Best quality |
| **OpenAI GPT** | `openai` | `OPENAI_API_KEY` | Good quality |
| **Anthropic Claude** | `anthropic` | `ANTHROPIC_API_KEY` | Good quality |

Background tasks (sensor monitoring, heating evaluation) always use local Ollama — no cloud cost for ambient intelligence.

### Changing the Voice

Nova supports three voice engines:

| Engine | Set `TTS_PROVIDER` to | Description |
|--------|----------------------|-------------|
| **Piper** (default) | `piper` | Local neural TTS, fast, good quality |
| **Intron Afro TTS** | `intron_afro_tts` | GPU-accelerated voice cloning sidecar |
| **ElevenLabs** | `elevenlabs` | Cloud TTS, highest quality, costs money |

Change in **Settings** → TTS Provider.

### Configuring Speakers

Nova can speak through any combination of:
- **Amazon Echo** devices (via Alexa Media Player integration)
- **Sonos** speakers
- **Google Cast** devices
- **Any HA media_player** entity

Set speakers in **Settings** or `.env`:
```
SPEAKERS=media_player.living_room_echo,media_player.kitchen_sonos
```

Echo devices are auto-detected by name. Force Alexa mode with `alexa:` prefix:
```
SPEAKERS=alexa:media_player.my_echo,media_player.sonos_bedroom
```

### Editing the System Prompt

The system prompt defines Nova's personality, knowledge, and rules. Edit it in the admin panel under **System Prompt**.

This is where you tell Nova:
- Your household members' names
- Room layouts and device locations
- Special rules ("never change the fridge temperature")
- Preferred response style

### Access Control (ACL)

The ACL controls which Home Assistant domains Nova can access. Edit in the admin panel under **ACL**.

**Default allowed:** lights, switches, climate, covers, fans, media players, sensors, cameras, automations, and more.

**Default denied:** locks, alarm panels, scripts, shell commands.

To allow Nova to control locks (at your own risk), add:
```yaml
- domain: "lock"
  entities: "*"
  services: ["lock", "unlock"]
```

### Home Runtime Config

`config/home_runtime.json` contains installation-specific entity mappings. This is auto-generated by the installer but you can edit it:

```json
{
  "weather_entity": "weather.forecast_home",
  "phone_notify_services": ["notify/mobile_app_my_phone"],
  "sensor_shortcuts": {
    "Living room temp": "sensor.living_room_temperature",
    "Total power": "sensor.total_power_consumption"
  },
  "sensor_threshold_rules": {
    "sensor.car_fuel_level": {
      "min": 15.0,
      "label": "Car fuel level",
      "unit": "%",
      "min_msg": "Car fuel is low at {value}%."
    }
  },
  "camera_vision_prompts": {
    "camera.front_door": "Describe who is at the front door..."
  },
  "energy_summary_entities": {
    "total_power": "sensor.total_power_consumption"
  }
}
```

Restart the service after editing: `sudo systemctl restart avatar-backend`

---

## The Avatar

### Customising Appearance

In **Settings**:
- **Skin tone** — presets applied to the 3D model
- **Background** — solid colour picker or image URL
- **Avatar model** — upload `.glb` files in the Avatar Library

### Embedding in Home Assistant

Add this to a Lovelace dashboard as a webpage card:

```yaml
type: iframe
url: http://<server-ip>:8001/avatar?api_key=YOUR_KEY&session_id=ha-dashboard
aspect_ratio: 16:9
```

Or use the custom card (`static/nova-avatar-card.js`) for a native HA card.

---

## Troubleshooting

### Nova isn't responding to voice

1. Check the avatar page is open and microphone is allowed
2. Check the health endpoint: `http://<server-ip>:8001/health`
3. Look for `whisper: loading` — the speech model takes ~30s to load on first start
4. Check logs: `sudo journalctl -u avatar-backend -f`

### Nova says "I couldn't analyze the camera image"

- GPU memory is full — check with `nvidia-smi`
- Vision model not pulled: `docker exec avatar_ollama ollama pull llama3.2-vision:11b`
- If using Gemini: check for 429 rate limit errors in logs

### Speakers aren't playing audio

1. Check **Settings** → Speakers are configured
2. Echo devices need Alexa Media Player integration in HA
3. Sonos needs the server to be reachable on the LAN
4. Check `PUBLIC_URL` is set for Echo SSML audio

### Motion clips look mangled / low FPS

- Camera streams use HTTPS — ensure HA's self-signed cert is trusted or the server resolves to a local IP
- Check logs for `motion_clip.capture_failed` or `poll_insufficient_frames`

### Service won't start

```bash
sudo systemctl status avatar-backend
sudo journalctl -u avatar-backend -n 50
```

Common causes:
- Missing `.env` file or `API_KEY` not set
- Python dependency missing — run `sudo ./install.sh --update`
- Port 8001 already in use

### Rolling back a bad update

```bash
./deploy.sh --rollback
```

This restores the previous deployment from the backup made before the last deploy.

---

## Updating Nova

### From the server

```bash
cd /opt/avatar-server
sudo ./install.sh --update
```

This syncs source files, installs new dependencies, refreshes Docker containers, and restarts the service.

### From a development machine

```bash
cd ~/nova-avatar
git pull origin main
./deploy.sh
```

---

## Home Assistant Integration

### Install the Custom Component

```bash
cp -r ha/custom_components/ai_avatar /config/custom_components/
```

### Add to configuration.yaml

```yaml
ai_avatar:
  ai_server_url: http://<server-ip>:8001
  api_key: !secret ai_avatar_api_key
```

### Available Services

| Service | Description |
|---------|-------------|
| `ai_avatar.announce` | Speak a message on all speakers + avatar |
| `ai_avatar.chat` | Send text to Nova; fires `ai_avatar_chat_response` event |

### Example Automation

```yaml
automation:
  - alias: "Welcome home"
    trigger:
      - platform: state
        entity_id: person.penn
        to: "home"
    action:
      - service: ai_avatar.announce
        data:
          message: "Welcome home! The heating is on and it's 21 degrees inside."
```

---

## Hardware Recommendations
## Face Recognition & License Plates

Nova can identify people and read license plates using CodeProject.AI.

### Setup

1. Install CodeProject.AI on a separate machine (e.g. your Blue Iris server)
2. Enable the **Face Processing** and **License Plate Reader** modules
3. Set `CODEPROJECT_AI_URL` in Nova's config (e.g. `http://192.168.0.33:32168`)

### How It Works

When a camera detects a person:
1. **Coral TPU** pre-filters the motion event
2. **YOLOv5** (CodeProject.AI) verifies the object with proper labels
3. **Face Recognition** (CodeProject.AI) identifies known people
4. **ALPR** (CodeProject.AI) reads license plates on vehicles
5. Descriptions are enriched: "Penn detected at front door" instead of "A person"

### Managing Faces

Go to **Admin Panel → Faces**:

- **Unknown Faces** — people detected but not recognized. Type a name and click Save to register them.
- **Known Faces** — registered people with × button to delete and re-register.

### Blue Iris Fallback

When Home Assistant is down, Nova fetches camera snapshots directly from Blue Iris:
- Set `BLUEIRIS_URL` in config (e.g. `http://192.168.0.33:81`)
- Map cameras in `home_runtime.json` under `blueiris_camera_map`

---


| Component | Minimum | Recommended |
|-----------|---------|-------------|
| **CPU** | Any modern x86_64 | Intel i5 / Ryzen 5 |
| **RAM** | 8 GB | 16 GB |
| **GPU** | None (CPU mode) | RTX 3060 12GB or RTX 4060 Ti 16GB |
| **Storage** | 20 GB | 50 GB+ (for motion clip archive) |
| **OS** | Ubuntu 22.04 / Debian 12 | Ubuntu 24.04 |

**GPU matters most.** With 6GB VRAM, chat and vision compete for memory. 12GB+ lets them run simultaneously without contention.

**Optional hardware:**
- **Coral Edge TPU** (USB) — pre-filters camera motion events, reduces false vision calls
- **Speakers** — Amazon Echo, Sonos, or any HA-compatible media player

---

## Glossary

| Term | Meaning |
|------|---------|
| **Ollama** | Local AI model server — runs LLMs on your GPU |
| **Piper** | Local text-to-speech engine |
| **Whisper** | Local speech-to-text engine |
| **Coral TPU** | Google's Edge TPU — hardware AI accelerator for object detection |
| **ACL** | Access Control List — controls what Nova can do in HA |
| **System Prompt** | The instructions that define Nova's personality and knowledge |
| **home_runtime.json** | Per-installation config for entity mappings and thresholds |
| **Proactive Service** | Background monitor that watches HA state changes |
| **Sensor Watch** | Background monitor for sensor threshold alerts |
| **TTS** | Text-to-Speech — converts text to audio |
| **STT** | Speech-to-Text — converts audio to text |
| **SSML** | Speech Synthesis Markup Language — used for Alexa audio playback |

---

## Getting Help

- **Logs:** `sudo journalctl -u avatar-backend -f`
- **Health:** `http://<server-ip>:8001/health`
- **Admin panel:** `http://<server-ip>:8001/admin`
- **GitHub:** https://github.com/digitalp/nova-v1
