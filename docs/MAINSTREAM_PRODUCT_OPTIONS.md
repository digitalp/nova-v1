# Nova Mainstream Product Options

Date saved: April 8, 2026
Branch: `codex/nova-ha-integration-doc`

Context:
- Nova currently rates strongest as a prosumer / self-hosted smart-home AI product.
- Approximate positioning:
  - prosumer: `8/10`
  - mainstream consumer: `5/10`
  - technical foundation / differentiation: `8.5/10`
- Goal of this note:
  - capture the highest-leverage options to move Nova from `8/10 prosumer` toward `7.5/10 mainstream`

## Option 1: Nail Onboarding First

Build a true "works in 15 minutes" path:
- one install flow
- HA discovery
- room/entity review
- test voice
- test speaker
- test first automation
- health check with clear fixes

Why:
- mainstream users churn on setup friction before they ever evaluate intelligence
- this is one of the biggest gaps between Nova and Alexa / Google / Homey

## Option 2: Build A Reliability Layer

Add a trust/safety layer around proactive and autonomous behavior:
- explain why Nova acted
- show which entities were used
- one-tap undo for reversible actions
- confidence labels
- cooldown editor
- safe-mode toggle that converts autonomous actions into suggestions

Why:
- mainstream users forgive limited intelligence more than surprising behavior
- reliability and trust matter more than “agentic” ambition

## Option 3: Ship A Guided Defaults Pack

Offer opinionated setup presets:
- apartment
- family home
- elderly support
- security-focused
- energy-saving
- privacy-first local only

Each preset should configure:
- prompts
- proactive rules
- quiet hours
- escalation
- notification style

Why:
- mainstream users do not want to author a household operating model
- they want to choose a home type, tweak a few things, and be done

## Option 4: Make Voice The Hero Product

Improve the voice loop specifically:
- wake-word to response latency targets
- interruption handling
- follow-up context
- better barge-in
- room-aware responses
- “speak here / notify there” logic
- simple hardware recommendation path

Why:
- mainstream buyers judge Nova against Alexa / Google first through voice
- if voice feels brittle, the whole product feels unfinished

## Option 5: Productize Camera And Security Flows

Turn the strongest “wow” flows into packaged features:
- doorbell narration
- driveway/package detection
- suspicious activity summary
- missed-events digest
- privacy zones and “never announce” zones

Why:
- this is one of Nova’s clearest differentiation areas
- users can understand the value quickly without deep setup knowledge

## Option 6: Add A Consumer-Grade Diagnostics Console

Create one place that answers “why isn’t it working?”:
- HA connection status
- missing entities
- broken speaker route
- TTS/STT readiness
- model availability
- automations calling wrong endpoint
- suggested fixes in plain English

Why:
- mainstream products win because failure states are legible
- support burden drops when the product explains what is wrong

## Recommended Order

1. Onboarding
2. Reliability layer
3. Guided defaults
4. Voice quality
5. Diagnostics console
6. Camera/security packs

## Recommendation

If the goal is the fastest move toward mainstream:
- prioritize `Option 1 + Option 2` in the next release

Reason:
- these likely improve mainstream adoption more than adding another AI capability
