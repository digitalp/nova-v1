# Nova AI and Home Assistant Integration Overview

## Purpose

Nova is the conversational and reasoning layer for the home.

Home Assistant remains the source of truth for entities, schedules, helpers, device control, and deterministic automations. Nova sits beside it and adds:

- natural-language interaction
- contextual announcements
- image- and event-aware summaries
- proactive reasoning over Home Assistant state
- a web avatar and admin surface

In practice, Home Assistant decides **what happened**, while Nova decides **what it means** and **how to say it**.

## The System Prompt: Nova's Brain

The single most important control surface in Nova is the system prompt in `config/system_prompt.txt`.

This is effectively Nova's operating manual and household brain. It defines:

- Nova's identity and operating style
- the priority order for decisions
- the safety and privacy rules
- the household profile
- the device and room model
- action boundaries
- how Nova should reason about comfort, security, energy, and convenience
- what kinds of proactive action are allowed

Without the system prompt, Nova is just a generic model with access to Home Assistant. With the system prompt, Nova becomes a household-specific agent that understands:

- who lives in the house
- which rooms and devices matter
- which actions are safe
- which automations should be conservative
- which signals are sensitive
- how to speak in a way that fits the home

That is why the system prompt should be treated as core application logic, not a cosmetic prompt tweak.

## High-Level Architecture

The integration is split into two parts:

1. Home Assistant
   - owns entities, integrations, automations, helper booleans, sensors, schedules, and service calls
   - exposes device and sensor state to Nova
   - triggers Nova through REST commands and the `ai_avatar` integration

2. Nova backend
   - runs as a FastAPI service on port `8001`
   - connects back to Home Assistant using REST and websocket APIs
   - provides chat, announce, motion, doorbell, avatar, and admin endpoints
   - performs reasoning with local and/or cloud LLMs
   - applies the system prompt to keep decisions aligned with the home model
   - returns spoken or text responses for Home Assistant to route

## Integration Model

There are three main ways Nova integrates into this Home Assistant instance.

### 1. HA-triggered announcements

Home Assistant calls Nova directly through `rest_command` entries in `configuration.yaml` on the Home Assistant host.

Current commands include:

- `nova_announce`
- `nova_chat`
- `nova_doorbell`
- `nova_motion_driveway`
- `nova_motion_outdoor`
- `nova_sync_prompt`

This is the main pattern used for household automations. HA detects an event, passes context to Nova, and Nova returns a spoken or textual response.

### 2. Nova-driven proactive monitoring

Nova independently watches Home Assistant state through websocket subscriptions.

There are two major services:

- `ProactiveService`
  - handles contextual state changes such as doorbell, cameras, weather, alarms, doors, climate, and general “should I say something?” reasoning
- `SensorWatchService`
  - handles sensor-heavy monitoring such as fridge faults, batteries, temperature extremes, energy anomalies, humidity, and other threshold or snapshot-review cases

This allows Nova to speak even when a traditional HA automation does not fire a direct REST request.

### 2.5. System-prompt-guided reasoning

Nova does not reason from raw entity state alone.

The system prompt is injected into the main conversational path and into proactive reasoning paths so that Nova evaluates events using household-specific context instead of generic model behavior.

That means the prompt influences:

- whether Nova should speak at all
- whether Nova should act, recommend, or defer
- how heating decisions are framed
- how camera and motion events are interpreted
- how sensitive topics are handled
- how assertive or conservative Nova should be
- how household members, devices, and areas are referred to

### 3. Conversational HA control

Nova can inspect entity state and call Home Assistant services as part of a voice or text conversation.

Typical examples:

- asking Nova for house status
- requesting device control by voice
- asking for summaries of train, weather, media, or security state

ACL rules in Nova limit what it can read or control.

## What Home Assistant Owns

Home Assistant should continue to own:

- device integrations
- helper entities such as `input_boolean.*`
- schedules and timers
- latching or cycle-tracking logic
- safety-critical rules
- exact thresholds and elapsed-time checks
- notification routing where deterministic behavior is required

Examples in this setup:

- washing machine cycle tracking
- fridge fault truth sensor
- AWTRIX display maintenance
- alarm arming/disarming flows
- quiet hours / do-not-disturb / guest mode helpers
- travel mode and commute timing triggers

## What Nova Owns

Nova is the better place for:

- contextual wording
- prioritization
- anomaly summaries
- camera and doorbell narration
- escalation tone
- daily briefings
- “what matters right now?” style reasoning
- natural language responses for media, travel, weather, and household state
- household-specific interpretation encoded in the system prompt

Examples in this setup:

- doorbell visitor narration
- driveway and outdoor motion interpretation
- parcel follow-up reminders
- morning briefings
- bedtime house check
- school morning mode
- visitor triage notes
- energy coaching suggestions
- fault escalation phrasing

## Current HA Patterns in This Setup

The current Home Assistant instance uses a hybrid pattern:

### Helper and state layer

HA defines household mode helpers such as:

- `input_boolean.nova_quiet_hours`
- `input_boolean.nova_do_not_disturb`
- `input_boolean.nova_guest_mode`
- `input_boolean.nova_travel_mode`
- `input_boolean.washing_machine_cycle_active`

These are used to control whether Nova should speak, notify phones, or stay quiet.

### Summary sensor layer

HA now exposes summary-style entities specifically to make Nova prompts simpler:

- `binary_sensor.nova_someone_home`
- `binary_sensor.house_needs_attention`
- `sensor.house_attention_summary`
- `sensor.next_commute_status`

These compress multiple entity states into a form Nova can reason over more reliably.

### Trigger-and-route layer

HA automations detect events, gather hard facts, and then decide the delivery channel:

- speaker announcement through `nova_announce`
- contextual generation through `nova_chat`
- camera-specific endpoints for motion and doorbell
- phone notifications during quiet hours or do-not-disturb windows
- AWTRIX updates for glanceable reminders

### Brain layer

Nova's system prompt acts as the policy and reasoning layer above all of this.

It is where the home-specific intelligence lives:

- room and device inventory
- family context
- household priorities
- privacy rules
- do-not-touch boundaries
- behavioral style
- decision preferences for when to act versus when to advise

If you want Nova to behave differently at a household level, the system prompt is usually the first place to change it.

## Current Nova-Enabled Automations

High-level categories already implemented in this HA instance include:

- camera and doorbell narration
- parcel delivery and parcel follow-up
- washing machine completion and humidity nudges
- fridge fault and fridge fault escalation
- open door energy saver and long-open escalation
- bin reminders
- train disruption speech and leave-now reminders
- arrival-home briefing
- morning home briefing
- school-night media guardian
- bedtime house check
- night security sweep
- household anomaly digest
- departure readiness briefings
- visitor triage summaries
- energy coaching

## Announcement Routing Logic

In this setup, Nova output is not always spoken.

Routing depends on Home Assistant helper state:

- `nova_quiet_hours`
  - suppresses non-urgent speaker output overnight
- `nova_do_not_disturb`
  - suppresses many non-critical reminder paths
- `nova_guest_mode`
  - can be used to avoid family-style or playful announcements
- `nova_travel_mode`
  - indicates the user is away and changes whether home-status prompts are useful

Common delivery targets are:

- whole-home spoken announcements through Nova
- phone notifications to Pixel devices
- AWTRIX display notifications

This means Nova is integrated as a multi-channel household intelligence layer, not only a speaker.

## Camera and Doorbell Flow

Camera integrations are now split cleanly:

1. Home Assistant detects motion, visitor, package, or vehicle signals.
2. HA chooses the correct Nova endpoint:
   - `/announce/doorbell`
   - `/announce/motion`
3. Nova fetches or reasons over camera context.
4. HA routes the result to speech, phone, or AWTRIX depending on household mode.

This replaced older HA-native AI camera automations and reduced duplication.

The system prompt matters here as well, because it gives Nova persistent context about the home, the household, and how cautious or selective it should be when describing events.

## Commute and Daily-Life Flow

Train and household timing automations now use Nova for phrasing, while HA still owns the schedule truth.

Examples:

- “leave now / wait / don’t rush” train guidance
- school-morning summary
- departure readiness briefings
- end-of-day anomaly digest

This keeps train and routine logic practical without burying too much logic in LLM prompts.

## Reliability Model

The current design intentionally keeps control-plane truth in Home Assistant.

If Nova is unavailable:

- Home Assistant still retains entity state and core automations
- helpers and schedules continue to work
- deterministic logic still runs
- only the AI narration and contextual summarization layer degrades

This is the correct failure model for a house system.

The main exception is behavior quality: if the system prompt is weak, stale, or missing key devices and policies, Nova may still function technically but behave like a generic assistant instead of a reliable home operations agent.

For that reason, system prompt maintenance is part of operations, not just prompt design.

## Prompt Maintenance and Admin Workflow

Nova exposes the system prompt through the admin panel and supports prompt synchronization.

Key operational points:

- the admin panel can read and update the prompt
- `/admin/sync-prompt` helps incorporate newly discovered entities into the prompt
- prompt changes immediately affect future chat sessions and proactive reasoning
- prompt quality directly affects Nova's safety, tone, usefulness, and consistency

In this setup, the prompt should be maintained whenever:

- major devices are added or renamed
- new rooms or cameras are introduced
- household policies change
- new proactive behaviors are added
- Nova starts missing obvious context or making poor assumptions

## Operational Notes

- Nova backend is served from the `/opt/avatar-server` stack on port `8001`
- nginx proxies HTTPS access for the Nova admin and avatar UI on the Nova host
- Home Assistant reaches Nova over LAN HTTP
- Nova uses the Home Assistant long-lived token configured in `.env`
- sensitive tokens and GitHub credentials should be rotated if they have been exposed during manual operations

## Recommended Long-Term Direction

The current split is strong. The next improvement should be reducing repeated routing logic in Home Assistant.

The ideal direction is:

1. HA continues to own truth, timers, and helpers
2. Nova centralizes more of the dedupe, priority, and routing policy
3. HA increasingly passes structured facts instead of large free-form prompts

That would make the integration easier to maintain as the number of proactive automations grows.

## Summary

Nova is integrated into this Home Assistant instance as an intelligence layer on top of a deterministic automation platform.

- Home Assistant owns state, safety, scheduling, and exact control
- Nova owns reasoning, summarization, interpretation, and spoken presentation
- the system prompt is the brain that makes Nova household-specific instead of generic
- together they provide a privacy-first local assistant that can talk, monitor, summarize, and react across the home without replacing the underlying HA control model
