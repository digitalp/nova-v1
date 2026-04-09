# Nova V2 Implementation Progress

This document tracks implementation status against the milestone tickets in [NOVA_V2_IMPLEMENTATION_PLAN.md](/opt/avatar-server/docs/NOVA_V2_IMPLEMENTATION_PLAN.md).

Status legend:

- `not_started`
- `in_progress`
- `completed`
- `partial`

## Tracker Guardrails

Update this tracker against the implementation plan, not against local momentum alone.

- Treat compatibility-first groundwork as partial credit unless the planned module, API, or persistence layer from `NOVA_V2_IMPLEMENTATION_PLAN.md` is actually present.
- Recompute `Overall` from the six milestone rows every time this table changes. Do not hand-edit it independently.
- Do not raise milestone percentages past plan-based caps until the corresponding planned artifacts exist:
- `Milestone 1` above `25%` requires `avatar_backend/services/event_bus.py`, `avatar_backend/services/event_store.py`, and `avatar_backend/models/events.py`.
- `Milestone 2` above `20%` requires `avatar_backend/services/camera_event_service.py`.
- `Milestone 3` above `60%` requires `avatar_backend/models/surface_messages.py`.
- `Milestone 5` above `70%` requires `avatar_backend/routers/actions.py` plus persisted `event_actions`.
- `Milestone 6` above `50%` requires the planned conversation-session persistence and related productization data model, not just admin UI progress.
- Before committing tracker changes, run `python3 /opt/avatar-server/scripts/check_v2_tracker.py`.

## Milestone Completion

| Milestone | Scope | Completion | Basis |
| --- | --- | ---: | --- |
| `Milestone 1` | Shared event model | `25%` | Strictly against the implementation plan, this milestone is still early but no longer just scaffolding: the canonical event schema, in-process event bus, persistent event store tables, and `EventStoreService` wiring now exist. What is still missing is broader service adoption and the planned event-first backbone across remaining producers and consumers. |
| `Milestone 2` | Camera event unification | `18%` | A first real `CameraEventService` now exists and owns shared camera resolution, snapshot analysis, delivery parsing, package-event shaping, and canonical camera-event metadata for doorbell, direct motion, proactive driveway flows, and package delivery alerts. What is still missing is fuller route consolidation and a broader shared camera-event pipeline across remaining producers. |
| `Milestone 3` | Surface state and event delivery | `48%` | The avatar now has active and recent event surfaces with status-aware controls, and admin/event-history reads now include canonical `events` rows from the event store instead of depending only on compatibility history. The plan still calls for surface state derived directly from the canonical event model plus a richer shared websocket protocol, so the current implementation remains transitional. |
| `Milestone 4` | Conversation and realtime voice | `80%` | This is the strongest milestone relative to plan: `ConversationService` and `RealtimeVoiceService` are real, text and event follow-up flows work, interruption and streamed transport foundations are in place, and `/ws/voice` remains compatible. The main remaining gaps are deeper conversation integration and fuller provider-native realtime backends. |
| `Milestone 5` | Actions and open loops | `68%` | `ActionService`, open-loop metadata, reminder and escalation workflow evaluation, and admin-triggered follow-up actions are all real, and action outcomes now have a first canonical persistence seam through `event_actions`. The plan still calls for dedicated action APIs, broader execute/cancel flows, and deeper event-store-first adoption across the action layer. |
| `Milestone 6` | Admin, metrics, and productization | `47%` | The admin history and filtering work is substantial, runtime/bootstrap groundwork exists, and the planned persistence model now includes canonical event records, action rows, media rows, and conversation-session summaries. The admin event feed also now reads canonical `events` rows directly, but the broader dedicated timeline UX, fuller severity/room-oriented filtering, and V2-specific installer outputs are still incomplete. |
| `Overall` | Weighted V2 roadmap progress | `48%` | Strict plan-based estimate derived from the milestone completion figures in this table. Compatibility-first groundwork is strong, the shared-event persistence backbone has moved forward, camera-event unification is landing across multiple producers, and canonical reads are now starting to show up in admin paths, but broader canonical adoption and productization scope are still ahead. |

## Milestone Status

### Milestone 1: Shared Event Model

| Ticket | Status | Notes |
| --- | --- | --- |
| `V2-001` | `in_progress` | `EventService` now exists as a compatibility-first canonical event layer used by multiple producers, and the shared visual-event publish path is now centralized there, but there is still no full event bus or broad backend adoption. |
| `V2-002` | `in_progress` | `metrics_db.py` now includes first-class `events`, `event_actions`, `event_media`, `conversation_sessions`, and `conversation_turn_summaries` tables, `EventStoreService` now exists and is wired in `main.py`, visual-event publication now persists into the canonical event store, and admin lifecycle actions now update canonical event status plus action records. What is still missing is broader service adoption and query usage outside the current compatibility paths. |

### Milestone 2: Camera Event Unification

| Ticket | Status | Notes |
| --- | --- | --- |
| `V2-010` | `in_progress` | `CameraEventService` now exists and doorbell, package, and direct motion handlers use it for shared camera resolution, snapshot fetch or event shaping, vision prompting, and normalized camera-event analysis. The route layer still owns some transport/orchestration details, so this is not complete yet. |
| `V2-011` | `in_progress` | Proactive driveway motion and delivery handling now also use `CameraEventService`, and the HA package automation path now has a dedicated backend `/announce/package` seam instead of posting raw visual events. Broader camera-event producers and fuller consolidation still remain. |

### Milestone 3: Surface State and Event Delivery

| Ticket | Status | Notes |
| --- | --- | --- |
| `V2-020` | `in_progress` | `SurfaceStateService` now exists as a compatibility-first registry for `avatar_state`, `active_event`, and `recent_events`, avatar/voice clients now receive `surface_state` snapshots alongside existing websocket payloads, and the V2 avatar client can restore the active popup plus a reopenable recent-events strip from those snapshots after reconnect. The avatar surface also supports server-backed dismiss/reactivate actions through `avatar_ws`, recent entries now carry explicit `active`/`dismissed`/`acknowledged`/`resolved` status, and surface-state events now preserve a small `open_loop_note` so unresolved items can explain why they still matter. Admin event-history reads now also include canonical `events` rows from the new store, but surface state itself is still not derived directly from that canonical model. |
| `V2-021` | `partial` | `static/avatar.html` now supports camera popups, gallery cards, turn-aware voice interruption handling, a lightweight recent-events strip backed by `surface_state`, server-backed close/reopen behavior for the active/recent event stack, visible status chips for active, dismissed, acknowledged, and resolved events, explicit popup acknowledge/resolve actions, per-entry acknowledge/dismiss/resolve controls in the recent-events strip, and unresolved-first ordering with relative timestamps. The UI now also shows backend-provided `open_loop_note` hints in the popup and recent-events strip so unresolved items carry a visible reason. It is still not a full event console with prioritization, richer action controls, and broader recent-event interaction patterns. |

### Milestone 4: Conversation and Realtime Voice

| Ticket | Status | Notes |
| --- | --- | --- |
| `V2-030` | `in_progress` | `ConversationService` now exists as a compatibility-first coordinator, chat and voice are wired through it, it has a structured context builder plus an event-follow-up entrypoint, visual events persist `event_id` context for `/chat/followup-event`, the avatar voice websocket can carry active `event_id` context into the next spoken turn, the avatar popup and recent-event list now both expose an explicit “Ask about this” follow-up action, and the coordinator now persists sanitized per-session home context with incremental merge plus explicit clear semantics while allowing event follow-up context to stay active for one additional turn across chat and voice surfaces. Nested dict/list context is now flattened into stable dotted keys, but broader coordinator validation and richer context semantics are still incomplete. |
| `V2-031` | `in_progress` | `RealtimeVoiceService` exists and V2 now supports per-session turn state, interruption, `turn_started`, `turn_finished`, `turn_interrupted`, `audio_start`, turn-aware client playback handling, optional streamed audio input buffering with explicit start/commit/cancel frames, negotiated output formats for streamed audio, progressive PCM playback on both the fallback audio path and the main head-backed avatar path, and a settings-wired provider-adapter factory with concrete `openai_chat_compat`, `google_chat_compat`, and `anthropic_chat_compat` adapters plus per-adapter capability enforcement for streamed input, streamed output, turn-context support, negotiated output formats, and real websocket milestone coverage for combined context carry-over and provider-adapter routing. Remaining work: richer lipsync on streamed chunks, deeper conversation integration, and fuller provider-native backends on top of the new adapter seam. |

### Milestone 5: Actions and Open Loops

| Ticket | Status | Notes |
| --- | --- | --- |
| `V2-040` | `in_progress` | A first dedicated `ActionService` now owns backend-defined `suggested_actions` for active and recent events, executes surface actions for `avatar_ws`, handles admin-side incident action execution so persisted incident updates and live surface sync no longer live in separate paths, exposes executable admin workflow actions for reminder and escalation follow-up on unresolved incidents, and now also runs admin-side domain follow-up actions through the conversation layer so event-history items can trigger real event questions instead of only lifecycle updates. Those admin lifecycle actions now also write through the canonical `EventStoreService` and record auditable `event_actions` rows. The V2 avatar renders those actions instead of relying only on hard-coded buttons, state-changing actions still use a confirmation step before websocket execution, and event follow-up actions can carry prompt seeds through the voice path so `ask about the vehicle` and similar actions are meaningfully distinct. Broader domain action execution is still missing. |
| `V2-041` | `in_progress` | Open-loop tracking now has a first-class metadata layer: durable `event_history` rows persist explicit open-loop state and timestamps, admin event history exposes and filters those fields directly, live surface events carry aligned open-loop lifecycle fields instead of relying only on free-text notes, the admin feed derives stale-loop and priority classification from unresolved-loop age, reminder plus escalation policy is persisted and exposed for long-lived unresolved incidents, those due workflow actions can now be executed from the admin history with durable policy updates plus live surface sync, a backend open-loop workflow evaluator now both summarizes and bulk-executes due reminder or escalation actions for persisted incidents, and a bounded background automation loop now runs those due actions on an interval with visible last-run status. What is still missing is richer client/admin UX and broader action-domain automation on top of that lifecycle state. |

### Milestone 6: Admin, Metrics, and Productization

| Ticket | Status | Notes |
| --- | --- | --- |
| `V2-050` | `in_progress` | The V2 admin now has a durable cross-event history feed that merges canonical event-store rows, compatibility history rows, archived canonical motion events, and live surface-state context, supports direct filtering plus basic paging/time-window controls, adds status-aware incident slicing and grouped history sections, can open persisted/surface events in a dedicated review modal, supports admin-side acknowledge/resolve/reopen actions, and now persists admin notes on those incident transitions. The underlying persistence model now also has canonical action rows and conversation-session summary tables available for broader admin auditability, but it still lacks a broader dedicated event timeline UX. |
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
- [avatar.html](/opt/avatar-server/static/avatar.html) now also coalesces adjacent streamed PCM chunks into roughly `180ms` head segments before calling `speakAudio`, and flushes any tail audio at `output_audio_end`, which reduces visible lipsync resets at chunk boundaries on the 3D head path
- [realtime_voice_service.py](/opt/avatar-server/avatar_backend/services/realtime_voice_service.py) now includes a settings-driven adapter factory, resolves app-provided realtime voice adapters, preserves the default STT/conversation/TTS orchestration path behind compatibility adapters, and lets each adapter advertise and enforce its own streamed-input, streamed-output, turn-context, and output-format capabilities
- [main.py](/opt/avatar-server/avatar_backend/main.py) now wires a concrete realtime voice adapter into `app.state` from settings at startup
- [test_realtime_voice_service.py](/opt/avatar-server/tests/test_realtime_voice_service.py) covers happy-path interruption behavior, turn metadata, streamed-input commit/cancel behavior, and the PCM streaming metadata path
- [test_realtime_voice_service.py](/opt/avatar-server/tests/test_realtime_voice_service.py) now also verifies adapter factory selection for OpenAI, Google, and Anthropic, `voice_capabilities` adapter metadata, streamed-input rejection, output-format fallback, and that an app-provided realtime voice adapter can replace the default STT/conversation/TTS pipeline
- [test_voice_milestone.py](/opt/avatar-server/tests/test_voice_milestone.py) now exercises websocket capability negotiation, adapter metadata, streamed output metadata, streamed input commit plus cancel semantics, same-socket interruption takeover, streamed-output interruption behavior, combined home-context plus event-overlay carry-over, custom or provider-selected realtime adapter routing, and real `/ws/voice` enforcement of adapter-specific streaming and format limits through the live route using `TestClient`

Still required before `V2-031` can be marked `completed`:

- further tuning lipsync continuity across streamed chunk boundaries on the main head-backed avatar path
- richer conversation-state integration beyond the current pending-event-context handoff
- fuller provider-native realtime backends built on top of the new adapter seam
- broader end-to-end validation beyond the current focused websocket transport, interruption, streamed-output, and coordinator slices

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

- new [action_service.py](/opt/avatar-server/avatar_backend/services/action_service.py)
- [surface_state_service.py](/opt/avatar-server/avatar_backend/services/surface_state_service.py) now delegates backend-defined `suggested_actions` to `ActionService` instead of owning the action catalog directly
- [avatar_ws.py](/opt/avatar-server/avatar_backend/routers/avatar_ws.py) now routes surface action execution through `ActionService`, including state transitions and related-camera opens, instead of hard-coding per-action branches in the router
- [admin.py](/opt/avatar-server/avatar_backend/routers/admin.py) now delegates `/admin/event-history/action` through `ActionService`, so persisted event-history updates, reminder/escalation policy writes, and live surface-state sync use one execution seam
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
- [test_action_service.py](/opt/avatar-server/tests/test_action_service.py) now verifies that `ActionService` can execute admin-style incident actions across both durable event history and live surface-state synchronization

Still required before `V2-040` can be marked `completed`:

- broader action execution beyond surface-state transitions and admin incident lifecycle updates
- action suggestions tied to concrete domain workflows such as `show driveway too` or `acknowledge package`

### `V2-041` Current Evidence

Current landed pieces:

- new [open_loop_service.py](/opt/avatar-server/avatar_backend/services/open_loop_service.py)
- [metrics_db.py](/opt/avatar-server/avatar_backend/services/metrics_db.py) now persists explicit open-loop lifecycle metadata alongside `event_history`, including `open_loop_state`, `open_loop_active`, started/updated timestamps, and resolved timestamp on closure
- [metrics_db.py](/opt/avatar-server/avatar_backend/services/metrics_db.py) now updates that metadata consistently on admin-side incident status changes instead of only mutating free-text notes
- [admin.py](/opt/avatar-server/avatar_backend/routers/admin.py) now exposes first-class open-loop fields on `/admin/event-history` items and supports direct `open_loop_state` plus `open_loop_only` filtering
- [open_loop_service.py](/opt/avatar-server/avatar_backend/services/open_loop_service.py) now derives `open_loop_age_s`, `open_loop_stale`, and an operator-facing `open_loop_priority` from unresolved-loop age and state
- [admin.py](/opt/avatar-server/avatar_backend/routers/admin.py) now exposes those stale-loop and priority fields on `/admin/event-history` and supports direct `open_loop_stale_only` and `open_loop_priority` filtering
- [open_loop_service.py](/opt/avatar-server/avatar_backend/services/open_loop_service.py) now also derives reminder-due, reminder-state, escalation-level, and escalation-due metadata from unresolved-loop age plus prior operator follow-up
- [metrics_db.py](/opt/avatar-server/avatar_backend/services/metrics_db.py) now persists reminder and escalation updates for existing `event_history` incidents so open-loop follow-up is durable rather than inferred only from current age
- [admin.py](/opt/avatar-server/avatar_backend/routers/admin.py) now exposes reminder and escalation fields on `/admin/event-history`, supports `open_loop_reminder_due_only` and `open_loop_escalation_due_only` filtering, and accepts admin-side reminder/escalation updates through `/admin/event-history/action`
- [surface_state_service.py](/opt/avatar-server/avatar_backend/services/surface_state_service.py) now carries aligned open-loop lifecycle fields on live surface events so active, acknowledged, dismissed, snoozed, and resolved states share one explicit model across live and durable views
- [test_admin_motion.py](/opt/avatar-server/tests/test_admin_motion.py) now covers open-loop filtering on `/admin/event-history`, stale-loop and priority filtering, reminder/escalation due filtering, plus durable open-loop persistence, reminder/escalation policy persistence, and resolution transitions in `MetricsDB`
- [test_surface_state_service.py](/opt/avatar-server/tests/test_surface_state_service.py) now verifies that live surface events expose explicit open-loop state and resolved metadata rather than relying only on `open_loop_note`

Still required before `V2-041` can be marked `completed`:

- richer loop workflows beyond status metadata, such as follow-up deadlines or owner assignment
- broader client/admin UX that highlights stale, reminder-due, and escalation-due loops instead of only exposing filterable metadata
- automated reminder or escalation execution beyond admin-side persistence and filterable policy state

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
- [conversation_service.py](/opt/avatar-server/avatar_backend/services/conversation_service.py) now keeps consumed event follow-up context active for one additional turn, so a `/chat/followup-event` turn can naturally continue into the next related chat or voice follow-up before the event focus drops away
- [context_builder.py](/opt/avatar-server/avatar_backend/services/context_builder.py) now flattens nested dictionaries and lists into stable dotted keys such as `climate.target` or `lights.0`, so richer structured context survives coordinator carry-over instead of collapsing into opaque string blobs
- [chat.py](/opt/avatar-server/avatar_backend/routers/chat.py) and [realtime_voice_service.py](/opt/avatar-server/avatar_backend/services/realtime_voice_service.py) now register pending event context with `ConversationService` before the next ordinary turn runs, which makes the coordinator own the event-linked state boundary
- [conversation_service.py](/opt/avatar-server/avatar_backend/services/conversation_service.py) now also owns `clear_session_state()`, so `/chat/{session_id}`, admin session clears, and voice corrupt-session recovery clear pending event context alongside the underlying session history
- [main.py](/opt/avatar-server/avatar_backend/main.py) wires `conversation_service` into `app.state`
- [announce.py](/opt/avatar-server/avatar_backend/routers/announce.py) now assigns `event_id` to visual events and stores recent follow-up context for camera and visual flows
- [avatar.html](/opt/avatar-server/static/avatar.html) now remembers the active visual-event `event_id`, sends it before the next recorded voice turn, exposes an explicit “Ask about this” action on the popup, and lets recent-event cards initiate the same event-linked follow-up flow without reopening the event first
- [test_conversation_service.py](/opt/avatar-server/tests/test_conversation_service.py) covers text context injection, nested dict/list flattening, raw voice-turn pass-through, event-follow-up context shaping, one-additional-turn event carry, persistence of sanitized home context across later text and voice turns, incremental context merges, and explicit clear-on-empty behavior
- [test_announce.py](/opt/avatar-server/tests/test_announce.py), [test_chat.py](/opt/avatar-server/tests/test_chat.py), and [test_realtime_voice_service.py](/opt/avatar-server/tests/test_realtime_voice_service.py) cover stored event context, `/chat/followup-event`, event-linked voice follow-up routing, and real `/chat` context merge-plus-clear behavior including nested flattening
- [test_voice_milestone.py](/opt/avatar-server/tests/test_voice_milestone.py) now also proves that a context-bearing `/chat` turn can persist sanitized home context into the real `/ws/voice` path on the next transcribed turn when both surfaces share the same `session_id`, that the same carried context composes correctly with a one-shot event follow-up overlay from `turn_context`, that an explicit empty context on `/chat` prevents stale home context from leaking into the next `/ws/voice` turn, and that a `/chat/followup-event` turn can continue naturally into the next related `/ws/voice` turn on the same session
- [proactive_service.py](/opt/avatar-server/avatar_backend/services/proactive_service.py) now expands aggregate `binary_sensor.house_needs_attention` events with the live `sensor.house_attention_summary` text before LLM triage, so generic household anomaly alerts can carry a concrete cause such as `back door open`

Still required before `V2-030` can be marked `completed`:

- richer structured context semantics beyond flattened dotted-key prompt shaping
- broader end-to-end validation of chat and voice through the new coordinator beyond the current focused milestone slices and mixed mocked-service harness

## Next Recommended Ticket

Highest-signal next build step:

1. Continue `V2-040` by expanding `ActionService` from lifecycle actions into concrete domain actions and reusable cross-surface execution, or
2. Continue `V2-041` by adding automated reminder or escalation execution on top of the new durable policy metadata

The better architectural move is still to continue `V2-040` next, because the action layer now spans both websocket and admin execution but still stops short of broader domain-level operations.
