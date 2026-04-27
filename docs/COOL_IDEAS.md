# Nova V2 — Multi-Family Architecture Roadmap

## Vision
Nova becomes a privacy-aware digital concierge that knows exactly what information belongs to whom. Each family gets their own isolated experience while sharing the same hardware.

---

## 1. Permission-Gated Semantic Memory (Vector RAG)
Every family gets an encrypted vector partition (Chroma collection per tenant).
- Memory queries are strictly limited to the active tenant namespace
- Nova remembers Family A prefers 22C, Family B prefers 19C — never cross-contaminates
- Inside jokes, past events, preferences all isolated per family

## 2. Identity-Aware Inner Monologue (CoT)
Before processing, prepend identity context: "Detected User: Penn (Family A). Location: Shared Hallway."
- Nova becomes a mediator in shared spaces
- If Penn asks to turn up heat in shared space and Family B prefers cooler, suggest compromise
- Social dynamics awareness, not just hardware control

## 3. Visual Intent via Zoning
Define Public, Shared, and Private zones in the vision system.
- Shared hallway: observant but silent unless specific person identified
- Private zones: full proactivity for that family only
- Strangers/delivery: limited interaction unless authorized

## 4. Dynamic Interruption Mapping
Voice fingerprinting (speaker diarization) to identify who is speaking.
- If explaining something to Family A and Family B child runs past screaming, ignore as noise
- Only listen to interruptions from the currently engaged family
- Requires pyannote.audio or similar speaker ID model

## 5. Profile-Based Model Routing
Each family chooses their privacy level.
- Family A: cloud (Gemini) for max intelligence
- Family B: local-only (Ollama) for privacy — no data leaves the house
- Router checks active user privacy profile in real-time
- Guests automatically get local-only mode

---

## Implementation Priority
1. Add `tenant_id` and `privacy_profile` to Person model
2. Namespace all memory writes with tenant_id
3. Add Chroma vector DB with per-tenant collections
4. Speaker diarization for voice identification
5. Zone mapping for cameras
6. Profile-based model routing (already have multi-provider)

---

# Nova V1 — Cool Ideas & Near-Term Features

## Context-Aware Greeting System (Enhanced)
**Current:** After 30-min cooldown, Nova says "Hey [name], need help with anything?"

**Proposed Enhancement:**
When Nova sees you again after the cooldown:
1. Check what room you are in (via which camera detected you)
2. Check if media is playing:
   - If Plex/Channels DVR is playing in that room, announce a fun fact or tell a joke
3. If no media playing:
   - Check for important house updates (doors left open, high energy usage, calendar events)
   - If updates exist, share the most important one
   - If no updates, just ask "Need help with anything?"
4. Vary responses to avoid repetition

## UniFi Network Integration
- Bedtime automation: block TikTok/Snapchat at set time, unblock in morning
- Network status tool: summarise connected devices, bandwidth, firewall state
- "Who is on the network?" voice command

## Barge-in / Interruption Handling
- VAD stays active during TTS playback
- If user speaks, immediately stop TTS, listen, adjust
- Creates fluid human conversation rhythm
- Requires chunked TTS streaming + WebSocket cancel

## Groq Tool Call Fix
- Parse Llama 3.3 XML-style tool calls into proper JSON format
- Test once rate limits clear
- Enable Groq as fast primary for simple commands
