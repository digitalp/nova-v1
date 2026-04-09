# Nova V2 Implementation Progress

This document tracks implementation status against the milestone tickets in [NOVA_V2_IMPLEMENTATION_PLAN.md](/opt/avatar-server/docs/NOVA_V2_IMPLEMENTATION_PLAN.md).

Status legend:

- `not_started`
- `in_progress`
- `completed`
- `partial`

## Milestone Completion

| Milestone | Scope | Completion | Basis |
| --- | --- | ---: | --- |
| `Milestone 1` | Shared event model | `38%` | A canonical event normalizer now exists, multiple producers publish through it, visual-event publication plus recent-event context registration are centralized in the canonical layer, and direct motion archives now persist canonical event metadata, but there is still no broad event bus, persistent event store, or full cross-service adoption. |
| `Milestone 2` | Camera event unification | `20%` | V2 routes real camera traffic and related-camera actions exist, but camera events still do not run through one canonical backend service. |
| `Milestone 3` | Surface state and event delivery | `63%` | Surface snapshots, recent-event recovery, statuses, action acks, related-camera opens, and snooze all work, but this is still compatibility-first rather than canonical. |
| `Milestone 4` | Conversation and realtime voice | `77%` | Conversation and realtime voice foundations are real and event-linked, the websocket voice path now supports optional chunked input and output transport, the main avatar now accepts progressive PCM segments instead of waiting for full-turn WAV buffering, event-linked follow-up is reachable from both the active popup and recent-event controls, and the coordinator now persists, incrementally merges, and explicitly clears sanitized per-session home context across later text and voice turns, with nested dict/list context flattened into stable dotted keys and validated through real `/chat` and `/ws/voice` paths, but broader coordinator validation outside the current mixed harness and realtime-provider work are still pending. |
| `Milestone 5` | Actions and open loops | `42%` | Suggested actions, confirmations, follow-up prompts, camera hops, and snooze are live, but there is no dedicated ActionService or richer policy engine yet. |
| `Milestone 6` | Admin, metrics, and productization | `64%` | Parallel runtime, runtime-path work, and installer groundwork exist, and the V2 admin now has a durable cross-event history feed with direct filtering, free-text search, saved presets, status-aware incident slicing, grouped history sections, real review paths for persisted and surface events, drill-down actions back into the archive filters, admin-side acknowledge/resolve/reopen actions, persisted admin notes on incident transitions, inline note visibility in the Event History list, and at-a-glance history metrics, but the broader admin event timeline and productization work are still mostly ahead. |
| `Overall` | Weighted V2 roadmap progress | `76%` | Strong foundation and interaction model, with major architecture and productization milestones still incomplete. |

## Milestone Status

### Milestone 1: Shared Event Model

| Ticket | Status | Notes |
| --- | --- | --- |
| `V2-001` | `in_progress` | `EventService` now exists as a compatibility-first canonical event layer used by multiple producers, and the shared visual-event publish path is now centralized there, but there is still no full event bus or broad backend adoption. |
| `V2-002` | `not_started` | Persistent event store not started. |

### Milestone 2: Camera Event Unification

| Ticket | Status | Notes |
| --- | --- | --- |
| `V2-010` | `not_started` | Doorbell still routes through `announce.py`; shared `CameraEventService` not extracted yet. |
| `V2-011` | `not_started` | Package, outdoor motion, and driveway vehicle are routed to V2, but they do not yet share a canonical camera-event backend path. |

### Milestone 3: Surface State and Event Delivery

| Ticket | Status | Notes |
| --- | --- | --- |
| `V2-020` | `in_progress` | `SurfaceStateService` now exists as a compatibility-first registry for `avatar_state`, `active_event`, and `recent_events`, avatar/voice clients now receive `surface_state` snapshots alongside existing websocket payloads, and the V2 avatar client can restore the active popup plus a reopenable recent-events strip from those snapshots after reconnect. The avatar surface also supports server-backed dismiss/reactivate actions through `avatar_ws`, recent entries now carry explicit `active`/`dismissed`/`acknowledged`/`resolved` status, and surface-state events now preserve a small `open_loop_note` so unresolved items can explain why they still matter. A broader canonical event model and richer surface protocols are still missing. |
| `V2-021` | `partial` | `static/avatar.html` now supports camera popups, gallery cards, turn-aware voice interruption handling, a lightweight recent-events strip backed by `surface_state`, server-backed close/reopen behavior for the active/recent event stack, visible status chips for active, dismissed, acknowledged, and resolved events, explicit popup acknowledge/resolve actions, per-entry acknowledge/dismiss/resolve controls in the recent-events strip, and unresolved-first ordering with relative timestamps. The UI now also shows backend-provided `open_loop_note` hints in the popup and recent-events strip so unresolved items carry a visible reason. It is still not a full event console with prioritization, richer action controls, and broader recent-event interaction patterns. |

### Milestone 4: Conversation and Realtime Voice

| Ticket | Status | Notes |
| --- | --- | --- |
| `V2-030` | `in_progress` | `ConversationService` now exists as a compatibility-first coordinator, chat and voice are wired through it, it has a structured context builder plus an event-follow-up entrypoint, visual events persist `event_id` context for `/chat/followup-event`, the avatar voice websocket can carry active `event_id` context into the next spoken turn, the avatar popup and recent-event list now both expose an explicit “Ask about this” follow-up action, and the coordinator now persists sanitized per-session home context with incremental merge plus explicit clear semantics alongside one-shot pending event context before later turns execute. Nested dict/list context is now flattened into stable dotted keys, but broader coordinator validation and richer context semantics are still incomplete. |
| `V2-031` | `in_progress` | `RealtimeVoiceService` exists and V2 now supports per-session turn state, interruption, `turn_started`, `turn_finished`, `turn_interrupted`, `audio_start`, turn-aware client playback handling, optional streamed audio input buffering with explicit start/commit/cancel frames, negotiated output formats for streamed audio, and progressive PCM playback on both the fallback audio path and the main head-backed avatar path. Remaining work: richer lipsync on streamed chunks, deeper conversation integration, and provider-native realtime adapters. |

### Milestone 5: Actions and Open Loops

| Ticket | Status | Notes |
| --- | --- | --- |
| `V2-040` | `in_progress` | `SurfaceStateService` now supplies backend-defined `suggested_actions` for active and recent events, the V2 avatar renders those actions instead of relying only on hard-coded buttons, state-changing actions now use a confirmation step before they are sent over `avatar_ws`, and event follow-up actions can now carry prompt seeds through the voice path so `ask about the vehicle` and similar actions are meaningfully distinct. A dedicated `ActionService`, richer cross-surface action APIs, and non-surface action execution are still missing. |
| `V2-041` | `not_started` | Open-loop tracking not started. |

### Milestone 6: Admin, Metrics, and Productization

| Ticket | Status | Notes |
| --- | --- | --- |
| `V2-050` | `in_progress` | The V2 admin now has a durable cross-event history feed that merges persisted canonical events, archived canonical motion events, and live surface-state context, supports direct filtering plus basic paging/time-window controls, adds status-aware incident slicing and grouped history sections, can open persisted/surface events in a dedicated review modal, supports admin-side acknowledge/resolve/reopen actions, and now persists admin notes on those incident transitions, but it still lacks a broader dedicated event timeline UX. |
| `V2-051` | `partial` | Installer/runtime groundwork exists from runtime mapping and bootstrap work, but the V2-specific structured installer outputs in the plan are not complete. |

## Completed or Landed Work

### Parallel V2 Runtime

Landed outside the milestone tickets but required for safe V2 development:

- isolated app root at `/opt/nova-v2`
- `nova-v2.service` on port `8011`
- separate HTTPS proxy on `8444`
- separate Home Assistant `nova_v2_*` REST commands
- selective automation cutover to V2

### `V2-050` Current Evidence

Current landed pieces:

- [metrics_db.py](/opt/avatar-server/avatar_backend/services/metrics_db.py) now exposes `canonical_event_id`, `canonical_event_type`, and `canonical_event` when reading motion clips
- [metrics_db.py](/opt/avatar-server/avatar_backend/services/metrics_db.py) now also persists canonical visual/surface events into `event_history` and can read them back as durable admin timeline records
- [admin.py](/opt/avatar-server/avatar_backend/routers/admin.py) now carries canonical event id, type, and source through the motion clip API serializer
- [motion_clip_service.py](/opt/avatar-server/avatar_backend/services/motion_clip_service.py), [metrics_db.py](/opt/avatar-server/avatar_backend/services/metrics_db.py), and [admin.py](/opt/avatar-server/avatar_backend/routers/admin.py) now support filtering the motion archive by canonical event type
- [event_service.py](/opt/avatar-server/avatar_backend/services/event_service.py) now persists canonical visual-event publications into the durable `event_history` store at publish time
- [admin.py](/opt/avatar-server/avatar_backend/routers/admin.py) now also exposes `/admin/event-history`, a first cross-event admin feed that merges persisted canonical events, archived canonical motion events, and recent surface-state events, and it supports direct filtering by kind, event type, and source plus `window` and `before_ts` controls
- [admin.py](/opt/avatar-server/avatar_backend/routers/admin.py) now includes durable event detail payloads on Event History rows, including persisted event `data`, motion-derived canonical metadata, and open-loop notes where available
- [admin.html](/opt/avatar-server/static/admin.html) now shows canonical event type chips on motion cards, event id/type/source metadata in the review modal, a dedicated event-type filter in the archive controls, a `Group By` toggle that can pivot the archive between day-based review and event-type/source/status triage, visible summary counts for event type/source/status above the archive, and a compact event-history list fed by `/admin/event-history` with its own kind/type/source filters plus time-window and older/newer controls
- [admin.html](/opt/avatar-server/static/admin.html) now opens non-clip Event History entries in a dedicated review modal with structured event metadata and a no-video event stage instead of leaving them as dead rows
- [admin.html](/opt/avatar-server/static/admin.html) now adds drill-down actions in the review modal so event history entries can jump straight into archive filtering by event type, camera, or source instead of forcing manual re-entry of those filters
- [admin.py](/opt/avatar-server/avatar_backend/routers/admin.py) now supports direct `status` filtering on `/admin/event-history`
- [admin.html](/opt/avatar-server/static/admin.html) now adds Event History `Status` and `Group By` controls so the cross-event feed can be sliced like incidents instead of staying a flat recent list
- [metrics_db.py](/opt/avatar-server/avatar_backend/services/metrics_db.py) now supports durable event-history status updates by `event_id`
- [admin.py](/opt/avatar-server/avatar_backend/routers/admin.py) now exposes `/admin/event-history/action` so the admin review surface can acknowledge, resolve, or reopen incidents while also nudging live surface state when the event is still active
- [admin.html](/opt/avatar-server/static/admin.html) now adds `Acknowledge`, `Resolve`, and `Reopen` controls to cross-event review items and refreshes the timeline after those incident actions
- [metrics_db.py](/opt/avatar-server/avatar_backend/services/metrics_db.py) now also persists `admin_note` and `admin_note_ts` on incident status changes
- [admin.py](/opt/avatar-server/avatar_backend/routers/admin.py) now accepts `admin_note` on `/admin/event-history/action`
- [admin.html](/opt/avatar-server/static/admin.html) now includes an `Incident Note` field in the review modal so admin-side incident actions can capture why the status changed
- [admin.html](/opt/avatar-server/static/admin.html) now surfaces saved `admin_note` directly in the Event History list, with open-loop context shown inline as a fallback when no admin note exists
- [admin.py](/opt/avatar-server/avatar_backend/routers/admin.py) now accepts a free-text `query` on `/admin/event-history` and matches it across event title, summary, event type, source, open-loop note, and saved admin note
- [admin.html](/opt/avatar-server/static/admin.html) now adds an Event History search field so operators can find incidents by natural text instead of only dropdown slices
- [admin.html](/opt/avatar-server/static/admin.html) now adds saved Event History presets such as `Needs Attention`, `Deliveries`, `Door Events`, and `Motion Review` so operators can jump into common incident views without rebuilding filters manually
- [admin.html](/opt/avatar-server/static/admin.html) now renders a compact Event History metrics strip for the currently visible incident set, showing status mix, event-type mix, and source mix at a glance
- [test_admin_motion.py](/opt/avatar-server/tests/test_admin_motion.py) now covers canonical motion-event serialization, cross-event history composition, direct event-history filtering, and `before_ts` windowed history reads
- [test_event_service.py](/opt/avatar-server/tests/test_event_service.py) now covers durable event-history persistence on canonical visual-event publication
- [test_admin_motion.py](/opt/avatar-server/tests/test_admin_motion.py) covers the admin serializer exposure of canonical motion-event metadata
- [realtime_voice_service.py](/opt/avatar-server/avatar_backend/services/realtime_voice_service.py) and [voice.py](/opt/avatar-server/avatar_backend/routers/voice.py) were re-aligned so the V2 voice websocket regression around `send_pong_if_needed` is cleared in the live service logs again

Still required before `V2-050` can be marked `completed`:

- richer dedicated cross-event timeline UX beyond the current merged feed
- broader admin filtering and grouping beyond the current kind/type/source/window controls
- cross-event history beyond the current visual/surface + motion mix

### `V2-001` Current Evidence

Current landed pieces:

- new [event_service.py](/opt/avatar-server/avatar_backend/services/event_service.py) provides a compatibility-first canonical event record for visual events
- [main.py](/opt/avatar-server/avatar_backend/main.py) now wires `EventService` into `app.state`
- [event_service.py](/opt/avatar-server/avatar_backend/services/event_service.py) now also owns the shared `publish_visual_event()` and recent-event-context registration helpers so routers stop stitching visual-event publication manually
- [announce.py](/opt/avatar-server/avatar_backend/routers/announce.py) now routes visual-event publication through the shared canonical publish helper instead of duplicating payload, context, and surface-state logic
- [avatar_ws.py](/opt/avatar-server/avatar_backend/routers/avatar_ws.py) now also routes `show_related_camera` through the same shared canonical publish helper, giving the canonical event model a second real producer on V2
- [proactive_service.py](/opt/avatar-server/avatar_backend/services/proactive_service.py) now also uses `EventService` to normalize motion and delivery camera events before clip archiving, giving the canonical event model its first non-router producer on V2
- [announce.py](/opt/avatar-server/avatar_backend/routers/announce.py) now also attaches canonical `motion_detected` metadata to direct `/announce/motion` clip archives so both motion paths persist normalized event context
- [metrics_db.py](/opt/avatar-server/avatar_backend/services/metrics_db.py) now exposes `canonical_event_id`, `canonical_event_type`, and `canonical_event` fields when reading motion clips, creating a small persistence bridge between the canonical event model and archived motion evidence
- [test_event_service.py](/opt/avatar-server/tests/test_event_service.py) covers canonical event construction plus the centralized publish-and-context path
- [test_announce.py](/opt/avatar-server/tests/test_announce.py) now covers canonical event metadata on direct motion archive scheduling

Still required before `V2-001` can be marked `completed`:

- expand canonical event usage beyond the current visual and camera-focused paths
- replace ad hoc event payload creation in remaining proactive and action flows
- introduce an actual event bus and persistent event store

### `V2-031` Current Evidence

Current landed pieces:

- new [realtime_voice_service.py](/opt/avatar-server/avatar_backend/services/realtime_voice_service.py)
- [voice.py](/opt/avatar-server/avatar_backend/routers/voice.py) delegates turn orchestration to `RealtimeVoiceService`
- per-websocket session keys and current-turn tracking
- explicit interruption via `turn_interrupted`
- explicit lifecycle events via `turn_started` and `turn_finished`
- explicit turn correlation via `turn_id`
- explicit audio boundary via `audio_start`
- optional streamed audio input via `input_audio_start`, `input_audio_commit`, and `input_audio_cancel`, while keeping the legacy one-blob turn path working
- websocket capability advertisement via `voice_capabilities`, so newer clients can detect streamed transport support before switching protocols
- opt-in chunked output transport via `client_capabilities`, `output_audio_start`, and `output_audio_end`, with progressive PCM playback available for both fallback audio and the head-backed avatar client
- [avatar.html](/opt/avatar-server/static/avatar.html) consumes interruption and turn-aware playback metadata
- [avatar.html](/opt/avatar-server/static/avatar.html) now requests streamed PCM whenever the browser can schedule audio locally, hands chunked PCM segments to the 3D head progressively, and slices server word timings into segment-local timings before each `speakAudio` call
- [test_realtime_voice_service.py](/opt/avatar-server/tests/test_realtime_voice_service.py) covers happy-path interruption behavior, turn metadata, streamed-input commit/cancel behavior, and the PCM streaming metadata path
- [test_voice_milestone.py](/opt/avatar-server/tests/test_voice_milestone.py) now exercises websocket capability negotiation and streamed output metadata through the real `/ws/voice` route using `TestClient`

Still required before `V2-031` can be marked `completed`:

- improving lipsync continuity across streamed chunk boundaries on the main head-backed avatar path
- richer conversation-state integration beyond the current pending-event-context handoff
- provider-adapter layer for future native realtime backends
- broader end-to-end validation beyond the current websocket transport negotiation slice

### `V2-020` Current Evidence

Current landed pieces:

- new [surface_state_service.py](/opt/avatar-server/avatar_backend/services/surface_state_service.py)
- [main.py](/opt/avatar-server/avatar_backend/main.py) wires `surface_state_service` into `app.state`
- [announce.py](/opt/avatar-server/avatar_backend/routers/announce.py) now routes avatar-state changes and visual-event registration through `SurfaceStateService`
- [realtime_voice_service.py](/opt/avatar-server/avatar_backend/services/realtime_voice_service.py) now routes avatar-state changes through `SurfaceStateService`
- [avatar_ws.py](/opt/avatar-server/avatar_backend/routers/avatar_ws.py) now sends an initial `surface_state` snapshot alongside the legacy `avatar_state` message
- [avatar.html](/opt/avatar-server/static/avatar.html) now consumes `surface_state` snapshots so the active event popup can recover from backend state after reconnect
- [avatar.html](/opt/avatar-server/static/avatar.html) now renders a lightweight recent-events strip from `surface_state.recent_events`, allowing reconnect-safe reopening of recent camera and visual events
- [surface_state_service.py](/opt/avatar-server/avatar_backend/services/surface_state_service.py) now supports server-backed `dismiss_active_event` and `activate_recent_event` transitions
- [surface_state_service.py](/opt/avatar-server/avatar_backend/services/surface_state_service.py) now tags recent events with explicit `active`/`dismissed`/`acknowledged`/`resolved` status and preserves that status through dismiss/reactivate/acknowledge/resolve actions
- [avatar_ws.py](/opt/avatar-server/avatar_backend/routers/avatar_ws.py) now accepts `surface_action` websocket messages and replies with `surface_action_ack`
- [avatar.html](/opt/avatar-server/static/avatar.html) now renders status chips in the recent-events strip so active, dismissed, and acknowledged entries are visually distinct
- [avatar.html](/opt/avatar-server/static/avatar.html) now exposes an explicit `Acknowledge` popup action that persists through backend surface-state updates
- [surface_state_service.py](/opt/avatar-server/avatar_backend/services/surface_state_service.py) now supports per-entry `dismiss_recent_event` and `acknowledge_recent_event` transitions without forcing a reopen first
- [avatar_ws.py](/opt/avatar-server/avatar_backend/routers/avatar_ws.py) now accepts `dismiss_recent_event` and `acknowledge_recent_event` actions with per-event acknowledgements
- [avatar.html](/opt/avatar-server/static/avatar.html) now renders per-entry `Acknowledge` and `Dismiss` controls in the recent-events strip so recent events can be triaged without reopening them
- [surface_state_service.py](/opt/avatar-server/avatar_backend/services/surface_state_service.py) now supports `resolve_active_event` and `resolve_recent_event` so acknowledged items can be closed out explicitly
- [avatar_ws.py](/opt/avatar-server/avatar_backend/routers/avatar_ws.py) now accepts `resolve_active_event` and `resolve_recent_event` actions with acknowledgements
- [avatar.html](/opt/avatar-server/static/avatar.html) now exposes `Resolve` in both the active popup and recent-events strip, and recent status chips now distinguish resolved entries visually
- [avatar.html](/opt/avatar-server/static/avatar.html) now sorts recent events with unresolved items first and shows relative event ages so the strip behaves more like a triage queue than a raw append-only list
- [surface_state_service.py](/opt/avatar-server/avatar_backend/services/surface_state_service.py) now assigns a small `open_loop_note` to events and updates it through acknowledge, dismiss, resolve, and reactivate transitions
- [avatar.html](/opt/avatar-server/static/avatar.html) now renders `open_loop_note` in both the active popup and the recent-events strip so unresolved entries explain whether they still need attention, were hidden, were seen, or were closed out
- [test_surface_state_service.py](/opt/avatar-server/tests/test_surface_state_service.py) and [test_avatar_ws.py](/opt/avatar-server/tests/test_avatar_ws.py) cover the compatibility slice

Still required before `V2-020` can be marked `completed`:

- canonical event-derived surface state instead of router-fed compatibility updates
- richer surface protocol for recent-event stacks and action affordances beyond the current acknowledge/dismiss/reactivate slice
- broader client adoption of `surface_state` beyond the avatar surface

### `V2-021` Visual References

Avatar surface reference captures for the current popup, confirmation, and recent-event triage states:

![Avatar active event state](/opt/avatar-server/docs/screenshots/avatar_active_event.png)
![Avatar confirmation flow](/opt/avatar-server/docs/screenshots/avatar_confirmation_flow.png)
![Avatar recent-event triage](/opt/avatar-server/docs/screenshots/avatar_recent_event_triage.png)

Notes:

- these captures are deterministic local renders of the current `avatar.html` visual states
- the browser-based screenshot path was blocked in this container, so the captures were generated from a local harness and renderer instead of a live headless browser session

### `V2-040` Current Evidence

Current landed pieces:

- [surface_state_service.py](/opt/avatar-server/avatar_backend/services/surface_state_service.py) now decorates active and recent events with backend-defined `suggested_actions`
- suggested actions are status-aware, so active, acknowledged, dismissed, and resolved events no longer expose the same control set
- [avatar.html](/opt/avatar-server/static/avatar.html) now renders popup and recent-event controls from backend-supplied `suggested_actions` instead of only hard-coded `Ask about this`, `Acknowledge`, and `Resolve` buttons
- [avatar.html](/opt/avatar-server/static/avatar.html) now requires an explicit confirmation step before state-changing actions such as acknowledge, dismiss, and resolve are sent over the websocket
- [surface_state_service.py](/opt/avatar-server/avatar_backend/services/surface_state_service.py) now emits domain-aware follow-up actions such as `Ask who is there`, `Ask about the vehicle`, and `Ask where the package is` using generic event text rather than home-specific entities
- unresolved recent events now retain those same backend-defined follow-up actions after reconnect or acknowledgement, so triage flows do not collapse back to generic controls
- [avatar.html](/opt/avatar-server/static/avatar.html), [realtime_voice_service.py](/opt/avatar-server/avatar_backend/services/realtime_voice_service.py), [conversation_service.py](/opt/avatar-server/avatar_backend/services/conversation_service.py), and [context_builder.py](/opt/avatar-server/avatar_backend/services/context_builder.py) now carry follow-up prompt seeds through `turn_context` into the event-followup voice path so those actions influence the next question without changing the client protocol
- [avatar_ws.py](/opt/avatar-server/avatar_backend/routers/avatar_ws.py) now handles a generic `show_related_camera` action by resolving camera aliases server-side, opening a new visual event, and registering related event context so follow-up questions still work on the newly opened view
- [surface_state_service.py](/opt/avatar-server/avatar_backend/services/surface_state_service.py), [avatar_ws.py](/opt/avatar-server/avatar_backend/routers/avatar_ws.py), and [avatar.html](/opt/avatar-server/static/avatar.html) now support a portable `snooze` state with `Snooze 30m` and `Unsnooze` actions so unresolved events can be deferred without being dismissed or resolved
- [test_surface_state_service.py](/opt/avatar-server/tests/test_surface_state_service.py) now checks suggested action generation for active and recent event states

Still required before `V2-040` can be marked `completed`:

- dedicated backend `ActionService` instead of folding the action model into `SurfaceStateService`
- broader action execution beyond surface-state transitions
- action suggestions tied to concrete domain workflows such as `show driveway too` or `acknowledge package`

### `V2-030` Current Evidence

Current landed pieces:

- new [conversation_service.py](/opt/avatar-server/avatar_backend/services/conversation_service.py)
- new [context_builder.py](/opt/avatar-server/avatar_backend/services/context_builder.py)
- [chat.py](/opt/avatar-server/avatar_backend/routers/chat.py) now routes text turns through `ConversationService`
- [chat.py](/opt/avatar-server/avatar_backend/routers/chat.py) now exposes `/chat/followup-event` to resolve stored visual-event context into `ConversationService`
- [realtime_voice_service.py](/opt/avatar-server/avatar_backend/services/realtime_voice_service.py) now routes voice turns through `ConversationService`
- [realtime_voice_service.py](/opt/avatar-server/avatar_backend/services/realtime_voice_service.py) now accepts `turn_context` frames so the next voice turn can resolve stored event context into `ConversationService`
- [conversation_service.py](/opt/avatar-server/avatar_backend/services/conversation_service.py) now stores pending event-followup context separately from the underlying session history and consumes it on the next text or voice turn instead of requiring routers to execute a fully separate follow-up path
- [conversation_service.py](/opt/avatar-server/avatar_backend/services/conversation_service.py) now also persists a sanitized per-session home-context snapshot with incremental merge semantics and explicit clear-on-empty behavior, so later text and voice turns can reuse or reset the latest structured home context without every caller resending the full block
- [context_builder.py](/opt/avatar-server/avatar_backend/services/context_builder.py) now flattens nested dictionaries and lists into stable dotted keys such as `climate.target` or `lights.0`, so richer structured context survives coordinator carry-over instead of collapsing into opaque string blobs
- [chat.py](/opt/avatar-server/avatar_backend/routers/chat.py) and [realtime_voice_service.py](/opt/avatar-server/avatar_backend/services/realtime_voice_service.py) now register pending event context with `ConversationService` before the next ordinary turn runs, which makes the coordinator own the event-linked state boundary
- [conversation_service.py](/opt/avatar-server/avatar_backend/services/conversation_service.py) now also owns `clear_session_state()`, so `/chat/{session_id}`, admin session clears, and voice corrupt-session recovery clear pending event context alongside the underlying session history
- [main.py](/opt/avatar-server/avatar_backend/main.py) wires `conversation_service` into `app.state`
- [announce.py](/opt/avatar-server/avatar_backend/routers/announce.py) now assigns `event_id` to visual events and stores recent follow-up context for camera and visual flows
- [avatar.html](/opt/avatar-server/static/avatar.html) now remembers the active visual-event `event_id`, sends it before the next recorded voice turn, exposes an explicit “Ask about this” action on the popup, and lets recent-event cards initiate the same event-linked follow-up flow without reopening the event first
- [test_conversation_service.py](/opt/avatar-server/tests/test_conversation_service.py) covers text context injection, nested dict/list flattening, raw voice-turn pass-through, event-follow-up context shaping, one-shot consumption of pending event context, persistence of sanitized home context across later text and voice turns, incremental context merges, and explicit clear-on-empty behavior
- [test_announce.py](/opt/avatar-server/tests/test_announce.py), [test_chat.py](/opt/avatar-server/tests/test_chat.py), and [test_realtime_voice_service.py](/opt/avatar-server/tests/test_realtime_voice_service.py) cover stored event context, `/chat/followup-event`, event-linked voice follow-up routing, and real `/chat` context merge-plus-clear behavior including nested flattening
- [test_voice_milestone.py](/opt/avatar-server/tests/test_voice_milestone.py) now also proves that a context-bearing `/chat` turn can persist sanitized home context into the real `/ws/voice` path on the next transcribed turn when both surfaces share the same `session_id`, that the same carried context composes correctly with a one-shot event follow-up overlay from `turn_context`, and that an explicit empty context on `/chat` prevents stale home context from leaking into the next `/ws/voice` turn
- [proactive_service.py](/opt/avatar-server/avatar_backend/services/proactive_service.py) now expands aggregate `binary_sensor.house_needs_attention` events with the live `sensor.house_attention_summary` text before LLM triage, so generic household anomaly alerts can carry a concrete cause such as `back door open`

Still required before `V2-030` can be marked `completed`:

- richer structured context semantics beyond flattened dotted-key prompt shaping
- broader end-to-end validation of chat and voice through the new coordinator beyond the current focused milestone slices and mixed mocked-service harness

## Next Recommended Ticket

Highest-signal next build step:

1. Continue `V2-031` until the transport is streaming-ready, or
2. Start `V2-030` by introducing `ConversationService` as the coordinator above both chat and voice

The better architectural move is `V2-030` next, because `RealtimeVoiceService` should hand turns to a conversation coordinator rather than calling `run_chat` directly.
