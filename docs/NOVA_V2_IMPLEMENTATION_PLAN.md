# Nova V2 Technical Implementation Plan

This document turns the V2 product roadmap into an engineering plan anchored in the current Nova codebase.

The central design decision is to evolve Nova from a chat-first backend with separate proactive and announce paths into an event-driven multimodal system. The current code already has the core ingredients:

- request or reply orchestration in `avatar_backend/services/chat_service.py`
- short-lived session state in `avatar_backend/services/session_manager.py`
- proactive event ingestion in `avatar_backend/services/proactive_service.py`
- camera and HA tool access in `avatar_backend/services/ha_proxy.py`
- voice transport in `avatar_backend/routers/voice.py`
- visual event delivery in `avatar_backend/routers/announce.py`
- websocket fanout in `avatar_backend/services/ws_manager.py`
- persistence and observability in `avatar_backend/services/decision_log.py`, `avatar_backend/services/persistent_memory.py`, and `avatar_backend/services/metrics_db.py`

V2 should build on those pieces instead of replacing them wholesale.

## Current Architecture Assessment

### Strengths

- `main.py` already wires services through `app.state`, which makes incremental refactors practical.
- `announce.py` already supports reusable `visual_event` transport for both camera and static-card flows.
- `proactive_service.py` already contains the right concept of event filtering, urgency, cooldowns, and camera-specific handling.
- `persistent_memory.py` and `decision_log.py` provide a foundation for structured memory and event history.
- Existing tests cover chat, announce, TTS, STT, and the current voice websocket.

### Constraints

- `session_manager.py` is conversation-oriented and in-memory only.
- `voice.py` is turn-based, not realtime or interruptible.
- `proactive_service.py` combines ingestion, filtering, reasoning, and announcing in one service.
- `announce.py` is transport-focused but not yet an event orchestration layer.
- Surface state is split between `/ws/avatar`, `/ws/voice`, and ad hoc message types.
- There is no canonical event schema shared across proactive, camera, voice, and UI paths.

## Target V2 Backend Model

V2 should be organized around five backend layers:

1. Event ingestion
2. Event reasoning
3. Event memory and persistence
4. Surface delivery
5. Action and follow-up execution

That leads to the following proposed services.

## Proposed Services

### 1. `EventBusService`

Purpose:

- provide a canonical in-process event pipeline
- decouple producers from consumers
- make proactive, camera, voice, and admin paths speak the same event language

Responsibilities:

- publish normalized event envelopes
- support subscriber registration per event type
- support sync fanout for low-latency UI paths
- optionally persist selected events to the event store

Proposed file:

- `avatar_backend/services/event_bus.py`

Dependencies:

- `decision_log.py`
- `metrics_db.py`
- `ws_manager.py`

### 2. `EventStoreService`

Purpose:

- persist recent household events as first-class records
- back camera history, event search, and open-loop tracking

Responsibilities:

- insert event records
- query by time range, camera, room, source, severity, and status
- mark events acknowledged, resolved, dismissed, or escalated
- expose short recent timeline for UI surfaces

Proposed files:

- `avatar_backend/services/event_store.py`
- schema additions in `avatar_backend/services/metrics_db.py`

### 3. `CameraEventService`

Purpose:

- unify doorbell, package, outdoor motion, driveway vehicle, and alarm-adjacent camera handling

Responsibilities:

- map event source to camera source
- fetch snapshot or stream metadata from `ha_proxy`
- call image description or classification paths in `llm_service`
- determine event summary, confidence, and escalation
- emit a normalized `camera_event`

Proposed file:

- `avatar_backend/services/camera_event_service.py`

Existing code to absorb or reuse:

- camera-specific logic from `routers/announce.py`
- motion camera map and image prompt logic from `services/proactive_service.py`
- alias resolution from `services/ha_proxy.py`
- runtime config from `services/home_runtime.py`

### 4. `ConversationService`

Purpose:

- replace the current chat-only orchestration with a shared multimodal conversation coordinator

Responsibilities:

- manage conversation turns across text, voice, and event follow-ups
- inject structured event memory and household context
- manage follow-up prompts and pending action requests
- support normal text chat and future realtime voice paths

Proposed file:

- `avatar_backend/services/conversation_service.py`

Existing code to absorb or reuse:

- `services/chat_service.py`
- `services/session_manager.py`
- `services/persistent_memory.py`

### 5. `RealtimeVoiceService`

Purpose:

- own streaming voice sessions, interruptions, output cancellation, and future provider-specific realtime adapters

Responsibilities:

- manage per-client voice sessions
- stream or chunk audio input and output
- detect interruption and cancel output cleanly
- coordinate with `ConversationService`
- broadcast structured speech and timing events to surfaces

Proposed file:

- `avatar_backend/services/realtime_voice_service.py`

Existing code to absorb or reuse:

- `routers/voice.py`
- `services/stt_service.py`
- `services/tts_service.py`
- `services/ws_manager.py`

### 6. `SurfaceStateService`

Purpose:

- provide one place to manage what each surface should currently show

Responsibilities:

- maintain current avatar state, active visual card, recent alerts, and follow-up prompts
- support multiple client types: avatar display, mobile client, TV overlay, admin live event panel
- generate surface-specific payloads from normalized events

Proposed file:

- `avatar_backend/services/surface_state_service.py`

Existing code to absorb or reuse:

- `services/ws_manager.py`
- `routers/announce.py`
- `static/avatar.html`

### 7. `ActionService`

Purpose:

- manage suggested actions and confirmation-required automations

Responsibilities:

- register action suggestions from event or conversation flows
- issue confirmation prompts
- execute validated HA tool calls or automation hooks
- persist action outcomes

Proposed file:

- `avatar_backend/services/action_service.py`

Dependencies:

- `services/ha_proxy.py`
- `services/conversation_service.py`
- `services/event_store.py`

## Proposed File and Module Changes

### Core Application Wiring

Update [main.py](/opt/avatar-server/avatar_backend/main.py) to instantiate and wire:

- `EventBusService`
- `EventStoreService`
- `ConversationService`
- `CameraEventService`
- `RealtimeVoiceService`
- `SurfaceStateService`
- `ActionService`

Expected changes:

- reduce direct service-to-service coupling in `main.py`
- register event subscribers instead of hardcoding direct callbacks
- shift proactive announce callback wiring into event publication

### Conversation and Memory

#### Existing files to change

- [chat_service.py](/opt/avatar-server/avatar_backend/services/chat_service.py)
- [session_manager.py](/opt/avatar-server/avatar_backend/services/session_manager.py)
- [persistent_memory.py](/opt/avatar-server/avatar_backend/services/persistent_memory.py)
- [routers/chat.py](/opt/avatar-server/avatar_backend/routers/chat.py)

#### Planned changes

- keep `run_chat` as a compatibility wrapper in early V2
- introduce `ConversationService.handle_text_turn()` and `ConversationService.handle_event_followup()`
- split `SessionManager` into:
- short-term conversation state
- event-linked follow-up state
- persist conversation summaries rather than raw full transcripts
- extend memory retrieval to consider:
- current room or surface
- recent unresolved events
- resident profile

#### New modules

- `avatar_backend/services/conversation_service.py`
- `avatar_backend/services/context_builder.py`
- `avatar_backend/services/event_memory.py`

### Event Ingestion and Camera Handling

#### Existing files to change

- [proactive_service.py](/opt/avatar-server/avatar_backend/services/proactive_service.py)
- [announce.py](/opt/avatar-server/avatar_backend/routers/announce.py)
- [ha_proxy.py](/opt/avatar-server/avatar_backend/services/ha_proxy.py)
- [home_runtime.py](/opt/avatar-server/avatar_backend/services/home_runtime.py)

#### Planned changes

- refactor `ProactiveService` into an event ingester and coarse filter
- move camera-specific reasoning into `CameraEventService`
- reduce `announce.py` to a delivery API rather than owning doorbell logic directly
- replace one-off `/announce/doorbell` behavior with a shared camera event pipeline
- preserve `/announce/doorbell` as a compatibility route that publishes a `camera_event`

#### New modules

- `avatar_backend/services/event_bus.py`
- `avatar_backend/services/event_store.py`
- `avatar_backend/services/camera_event_service.py`
- `avatar_backend/models/events.py`

### Voice and Surface Delivery

#### Existing files to change

- [voice.py](/opt/avatar-server/avatar_backend/routers/voice.py)
- [ws_manager.py](/opt/avatar-server/avatar_backend/services/ws_manager.py)
- [avatar_ws.py](/opt/avatar-server/avatar_backend/routers/avatar_ws.py)
- [static/avatar.html](/opt/avatar-server/static/avatar.html)
- [static/nova-avatar-card.js](/opt/avatar-server/static/nova-avatar-card.js)

#### Planned changes

- move session and audio-loop logic out of `voice.py` into `RealtimeVoiceService`
- extend websocket message types to support:
- `event_created`
- `event_updated`
- `event_resolved`
- `action_request`
- `action_result`
- `conversation_followup`
- add interrupt and cancel semantics for voice output
- give avatar clients a recent-alert stack instead of only a single transient popup
- define per-surface capabilities so mobile and TV clients can receive the same event model but render differently

#### New modules

- `avatar_backend/services/realtime_voice_service.py`
- `avatar_backend/services/surface_state_service.py`
- `avatar_backend/models/surface_messages.py`

### Observability and Persistence

#### Existing files to change

- [decision_log.py](/opt/avatar-server/avatar_backend/services/decision_log.py)
- [metrics_db.py](/opt/avatar-server/avatar_backend/services/metrics_db.py)
- [log_store.py](/opt/avatar-server/avatar_backend/services/log_store.py)
- [admin.py](/opt/avatar-server/avatar_backend/routers/admin.py)
- [static/admin.html](/opt/avatar-server/static/admin.html)

#### Planned changes

- distinguish AI decisions from household events
- store event lifecycle transitions in SQLite
- add event history APIs to admin
- expose confidence, acknowledged state, action results, and latency
- add event-type and camera filters to admin UI

### Configuration and Bootstrap

#### Existing files to change

- [install.sh](/opt/avatar-server/install.sh)
- [prompt_bootstrap.py](/opt/avatar-server/avatar_backend/services/prompt_bootstrap.py)
- [home_runtime.py](/opt/avatar-server/avatar_backend/services/home_runtime.py)
- [system_prompt_template.txt](/opt/avatar-server/config/system_prompt_template.txt)

#### Planned changes

- expand installer outputs to include:
- area mappings
- surface profiles
- camera-event profiles
- resident roles
- action safety policy defaults
- reduce house-specific prompt burden by moving more runtime facts into structured config

## Data Model Changes

### New Event Schema

Add `avatar_backend/models/events.py` with a canonical event envelope:

- `event_id`
- `event_type`
- `source`
- `room`
- `camera_entity_id`
- `severity`
- `summary`
- `details`
- `confidence`
- `status`
- `created_at`
- `expires_at`
- `action_suggestions`
- `linked_session_id`
- `linked_media`

### New SQLite Tables

Add to `metrics_db.py`:

- `events`
- `event_actions`
- `event_media`
- `conversation_sessions`
- `conversation_turn_summaries`

Keep `decision_log` as a separate concern, but link event IDs when applicable.

## API Plan

### Keep for compatibility

- `POST /chat`
- `DELETE /chat/{session_id}`
- `POST /announce`
- `POST /announce/visual`
- `POST /announce/doorbell`
- `GET /ws/token`
- `WS /ws/voice`

### Add in V2

- `GET /events`
- `GET /events/{event_id}`
- `POST /events/{event_id}/ack`
- `POST /events/{event_id}/resolve`
- `POST /events/{event_id}/dismiss`
- `POST /actions/{action_id}/execute`
- `POST /actions/{action_id}/cancel`
- `GET /surfaces/state`
- `WS /ws/events`

### Planned router additions

- `avatar_backend/routers/events.py`
- `avatar_backend/routers/actions.py`
- `avatar_backend/routers/surfaces.py`

## UI Implementation Plan

### Avatar Page

Change [avatar.html](/opt/avatar-server/static/avatar.html) from a single-popup experience to a small event console:

- persistent recent event strip
- active event card with countdown and status
- follow-up prompt region
- action buttons for confirmation-required tasks
- camera card reuse across doorbell, package, and driveway events

### Admin Panel

Extend [admin.html](/opt/avatar-server/static/admin.html) to include:

- event timeline
- open loops
- camera event summaries
- action audit trail
- per-model latency and fallback tracking

### Optional Future Surfaces

- lightweight mobile event summary page
- TV overlay client
- area-specific hallway or kitchen view

## Milestone Tickets

The tickets below are sized as implementation milestones rather than one-commit tasks.

### Milestone 1: Shared Event Model

Ticket `V2-001`: Add canonical event schema and event bus

Scope:

- create `models/events.py`
- create `services/event_bus.py`
- define event types and payload contracts
- add unit tests for event publication and subscription

Files:

- new `avatar_backend/models/events.py`
- new `avatar_backend/services/event_bus.py`
- update `tests/test_ws_manager.py`
- add `tests/test_event_bus.py`

Acceptance criteria:

- services can publish and subscribe to typed events
- event payload format is stable and validated

Ticket `V2-002`: Add persistent event store

Scope:

- add new event tables in `metrics_db.py`
- create `services/event_store.py`
- add CRUD and list APIs for event records

Files:

- `avatar_backend/services/metrics_db.py`
- new `avatar_backend/services/event_store.py`
- add `tests/test_event_store.py`

Acceptance criteria:

- events survive restart
- recent event timeline can be queried by type and date

### Milestone 2: Camera Event Unification

Ticket `V2-010`: Extract doorbell logic into `CameraEventService`

Scope:

- move camera analysis and summary generation out of `announce.py`
- preserve compatibility route behavior

Files:

- `avatar_backend/routers/announce.py`
- new `avatar_backend/services/camera_event_service.py`
- `avatar_backend/services/ha_proxy.py`
- add `tests/test_camera_event_service.py`
- update `tests/test_announce.py`

Acceptance criteria:

- `/announce/doorbell` uses shared camera event service internally
- event output includes summary, confidence, and visual payload metadata

Ticket `V2-011`: Convert package, outdoor motion, and driveway vehicle to shared camera events

Scope:

- refactor proactive event path for those sources
- emit normalized camera events instead of direct announce calls

Files:

- `avatar_backend/services/proactive_service.py`
- `avatar_backend/services/home_runtime.py`
- `config/system_prompt.txt`
- new tests for camera event flows

Acceptance criteria:

- top three camera automations share one backend event path
- visual and spoken handling is consistent

### Milestone 3: Surface State and Event Delivery

Ticket `V2-020`: Add surface state service and event websocket protocol

Scope:

- create `surface_state_service.py`
- add richer websocket event messages
- support active event and recent event list

Files:

- new `avatar_backend/services/surface_state_service.py`
- `avatar_backend/services/ws_manager.py`
- `avatar_backend/routers/avatar_ws.py`
- new `avatar_backend/models/surface_messages.py`
- add `tests/test_surface_state_service.py`

Acceptance criteria:

- avatar clients receive active event and recent event updates
- surface state is derived from the canonical event model

Ticket `V2-021`: Upgrade avatar UI into event console

Scope:

- add active card, recent stack, follow-up region, and action controls

Files:

- `static/avatar.html`
- `static/nova-avatar-card.js`
- update announce and voice UI integration tests if added

Acceptance criteria:

- avatar page shows more than one event context
- package, outdoor, and driveway events render cleanly

### Milestone 4: Conversation and Realtime Voice

Ticket `V2-030`: Introduce `ConversationService`

Scope:

- wrap current `run_chat` flow in a higher-level coordinator
- inject event memory and structured context
- support event follow-up prompts

Files:

- new `avatar_backend/services/conversation_service.py`
- `avatar_backend/services/chat_service.py`
- `avatar_backend/services/session_manager.py`
- `avatar_backend/routers/chat.py`
- add `tests/test_conversation_service.py`

Acceptance criteria:

- text chat still works
- event follow-up conversation can be linked to an event

Ticket `V2-031`: Add `RealtimeVoiceService` and interruptible voice loop

Scope:

- move business logic out of `routers/voice.py`
- support output cancellation and later streaming expansion
- keep current websocket API working during migration

Files:

- new `avatar_backend/services/realtime_voice_service.py`
- `avatar_backend/routers/voice.py`
- `avatar_backend/services/tts_service.py`
- `avatar_backend/services/stt_service.py`
- update `tests/test_voice_milestone.py`
- add `tests/test_realtime_voice_service.py`

Acceptance criteria:

- current voice flow still passes
- output can be interrupted or cancelled cleanly

### Milestone 5: Actions and Open Loops

Ticket `V2-040`: Add action suggestion and confirmation framework

Scope:

- create `ActionService`
- add action records linked to events
- add execute and cancel APIs

Files:

- new `avatar_backend/services/action_service.py`
- new `avatar_backend/routers/actions.py`
- `avatar_backend/services/ha_proxy.py`
- add `tests/test_action_service.py`

Acceptance criteria:

- event cards can offer suggested actions
- action results are persisted and auditable

Ticket `V2-041`: Add open-loop tracking

Scope:

- track unresolved household tasks such as package outside or alarm unresolved
- expose open loops to conversation and UI layers

Files:

- `avatar_backend/services/event_store.py`
- `avatar_backend/services/conversation_service.py`
- `avatar_backend/services/persistent_memory.py`
- add `tests/test_open_loops.py`

Acceptance criteria:

- unresolved events can be queried and surfaced in follow-up prompts

### Milestone 6: Admin, Metrics, and Productization

Ticket `V2-050`: Add admin event timeline and filters

Scope:

- expose event history and event state transitions
- filter by type, room, camera, severity, and state

Files:

- `avatar_backend/routers/admin.py`
- `static/admin.html`
- `avatar_backend/services/decision_log.py`
- `avatar_backend/services/event_store.py`

Acceptance criteria:

- admin panel can inspect event lifecycle and action outcomes

Ticket `V2-051`: Expand installer/bootstrap for V2 runtime config

Scope:

- add structured runtime outputs for areas, surfaces, camera profiles, and action policy

Files:

- `install.sh`
- `avatar_backend/services/prompt_bootstrap.py`
- `avatar_backend/services/home_runtime.py`
- `config/system_prompt_template.txt`

Acceptance criteria:

- new installs can generate V2 runtime config without hand-editing code

## Test Strategy

### Unit Tests

Add:

- `tests/test_event_bus.py`
- `tests/test_event_store.py`
- `tests/test_camera_event_service.py`
- `tests/test_surface_state_service.py`
- `tests/test_conversation_service.py`
- `tests/test_realtime_voice_service.py`
- `tests/test_action_service.py`
- `tests/test_open_loops.py`

### Integration Tests

Extend:

- [test_announce.py](/opt/avatar-server/tests/test_announce.py)
- [test_voice_milestone.py](/opt/avatar-server/tests/test_voice_milestone.py)
- [test_chat.py](/opt/avatar-server/tests/test_chat.py)
- [test_ws_manager.py](/opt/avatar-server/tests/test_ws_manager.py)

### Manual End-to-End Scenarios

- doorbell visitor with live card and spoken follow-up
- package delivery with later unresolved-package follow-up
- driveway vehicle event during quiet hours
- alarm event with camera escalation
- voice interruption while Nova is speaking
- suggested action confirmation from active event card

## Recommended Delivery Order

Build in this order:

1. shared event schema and event persistence
2. camera event unification
3. surface state service and avatar event console
4. conversation service abstraction
5. realtime voice service
6. actions and open loops
7. admin and installer hardening

That order keeps the highest-value user-visible improvements early while minimizing rework in the voice and UI layers.

## First Sprint Recommendation

If work starts immediately, the first sprint should aim for:

- `V2-001` shared event model
- `V2-002` persistent event store
- `V2-010` camera event service extraction

That gives the project a stable V2 backbone before UI and voice complexity increases.
