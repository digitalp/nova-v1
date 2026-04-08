# Nova V2 Roadmap

Nova V2 should evolve from a talking avatar with automations into a local-first, multimodal home concierge that can see, speak, listen, explain, and act across the whole house.

## Product Direction

### 1. Nova Realtime Core

Build a single low-latency conversation engine with interruption, turn-taking, image grounding, and tool calling. The goal is to replace the current stitched STT -> LLM -> TTS feel with a natural spoken loop.

### 2. Nova Events

Create one event model for doorbell, package, driveway vehicle, outdoor motion, alarm, appliance anomalies, and reminders. Every event should support:

- spoken summary
- visual card or live feed
- urgency level
- suggested actions
- short-lived memory for follow-up questions

### 3. Nova Surfaces

Treat the avatar page as one client, not the whole product. Add:

- hallway or kitchen wall display
- mobile companion summary
- TV overlay for urgent camera events
- area-aware views tied to Home Assistant areas

### 4. Nova Memory

Move from raw session history to structured household context:

- resident preferences
- room and device aliases
- recent event timeline
- quiet-hours and interruption policy
- open loops such as package still outside, washer still damp, or alarm unresolved

### 5. Nova Platform

Make runtime mappings, model backends, prompts, and tools modular. The goal is installer-grade deployment for new homes without house-specific code edits.

## Architecture Roadmap

### Phase 1: Foundation (0-30 days)

- Replace chat-centric orchestration with a unified event bus for `announce`, `visual_event`, `camera_event`, `decision_log`, and `action_request`.
- Introduce a structured memory store for recent events and household state summaries.
- Normalize camera and runtime mappings so doorbell, outdoor, and driveway all use the same visual-event path.
- Formalize automation metadata: event type, urgency, camera, room, and allowed actions.

Deliverables:

- shared event schema
- persisted recent-event store
- reusable visual-card helpers for camera and gallery events
- runtime-config cleanup so new homes are bootstrap-able

### Phase 2: Multimodal UX (30-60 days)

- Upgrade the voice loop to realtime streaming with interruption support.
- Add follow-up prompts after important events, such as "Want me to show the driveway too?"
- Expand the avatar page into an event console with stacked recent alerts, action buttons, richer camera cards, and event status badges.
- Add a mobile-friendly event summary surface.

Deliverables:

- realtime voice session manager
- follow-up action framework
- richer avatar event panel
- mobile summary view

### Phase 3: Camera Intelligence (60-90 days)

- Generalize the doorbell flow into camera intelligence flows for package delivery, package follow-up, outdoor motion, driveway vehicle, and alarm-adjacent camera triggers.
- Add searchable camera event history with AI summaries.
- Add confidence labels and escalation rules.

Deliverables:

- `camera_event_service`
- visual and spoken handling for the top five camera automations
- event history UI
- search and filter by time, camera, and event type

### Phase 4: Household Assistant (90-120 days)

- Add planning and routine assistance for departure readiness, evening home check, package and weather risk reminders, and appliance follow-through.
- Support confirmation-based actions for close, lock, and check flows, scene switching, and arm or disarm flows with guardrails.
- Add personalized briefings by resident, time, and location.

Deliverables:

- household briefing engine
- action confirmation flow
- role-aware and person-aware prompt context
- configurable proactive policies

### Phase 5: Productization (120+ days)

- Add an installer flow for new homes with bootstrap questions and automatic mapping generation.
- Introduce a plugin and tool architecture for external integrations and MCP-style tool access.
- Add a model and provider policy engine with local handling for control and routine summaries, and cloud handling only for hard vision and planning tasks.
- Add analytics for latency, false alerts, ignored prompts, and accepted actions.

Deliverables:

- installer-grade setup
- backend and provider abstraction
- observability dashboard
- multi-home deployment path

## Feature Priorities

Highest-value V2 features:

- realtime interruptible conversation
- camera event expansion beyond doorbell
- structured event memory
- actionable event cards
- area-aware surfaces
- installer-ready runtime config

Second-wave features:

- personalized briefings
- searchable event timeline
- action suggestions and confirmations
- mobile companion UI
- TV overlay unification

## Suggested Release Shape

### V2.0

- unified event system
- realtime voice core
- package, outdoor, and driveway visual flows
- event memory

### V2.1

- area-aware UI surfaces
- event history and search
- action suggestions

### V2.2

- personalized briefings
- richer planner and routine logic
- installer and multi-home hardening

## Success Metrics

Track these from the start:

- time from trigger to visual display
- time from trigger to spoken response
- percent of alerts with follow-up interaction
- false-positive dismissal rate
- accepted action rate
- cloud escalation rate versus local resolution
- setup time for a new home install

## Recommended First Implementation Slice

The highest-leverage first V2 slice is:

1. unified event model
2. structured recent-event memory
3. camera-event generalization for package, outdoor motion, and driveway vehicle
4. realtime interruptible voice loop

That sequence creates a real V2 backbone instead of a loose collection of features.

## Notes

This roadmap is informed by current platform direction across Home Assistant, modern realtime multimodal assistants, and smart-home UX trends, with a bias toward local-first operation and practical productization.
