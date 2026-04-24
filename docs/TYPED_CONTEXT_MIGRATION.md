# Typed Context Migration

## Goal

Move durable household data out of `config/system_prompt.txt` and into typed per-install storage without breaking Nova's current admin flows.

## Current State

Nova already has a useful discovery workflow:

- `/admin/sync-prompt/preview` discovers Home Assistant entities not yet represented in the prompt
- the admin UI lets the user review entities and override areas
- `/admin/sync-prompt/apply` uses the LLM to splice those entities into the long-form system prompt

That works, but it keeps the durable source of truth in prose. The migration keeps the same UX and changes the persistence model underneath it.

## Target Model

Split household context into:

1. Platform schema
- typed models and services in the repo
- generic resource, person, policy, schedule, and override concepts

2. Installation state
- `config/family_state.json`
- `config/home_runtime.json`
- `.env`

3. Generated runtime context
- compact snippets assembled per request
- derived from typed state plus live Home Assistant state

## Migration Rules

- The system prompt keeps personality, safety, and behavior guidance.
- Typed storage owns durable household facts.
- Discovery does not auto-create sensitive policy facts.
- Existing system-prompt sync remains functional during migration.

## Phase Plan

### Phase 1: Resource Foundation

- Add `FamilyContextService`
- Add typed `FamilyState` and `FamilyResource` models
- Write confirmed sync-prompt discoveries into `config/family_state.json`
- Keep the legacy prompt update path in place

### Phase 2: Read Path

- Read known entities from typed storage during preview
- Expose admin read APIs for typed state
- Add install-safe example config

### Phase 3: Context Assembly

- Update context building to pull relevant typed resources
- Generate compact context snippets instead of relying on large prompt prose

### Phase 4: Family/Parental Facts

- Add typed people, guardians, schedules, and policies
- Move sensitive enforcement logic into deterministic code
- Keep the LLM for explanation and natural-language interaction

### Phase 5: UI Migration

- Move discovery/resource editing out of the "System Prompt" conceptual model
- Add dedicated Family/Resources pages
- Add explainability for why a fact or policy was used

## Portability Requirements

- The repo must not hardcode real household entities.
- Real per-home state should live in local config files or DB rows.
- Policies should reference stable internal IDs, not raw HA entity IDs.
- Provider-specific IDs belong in bindings/resolvers.

## First Implementation Slice

This patch set does the following:

- adds `config/family_state.example.json`
- adds typed family/resource models and storage service
- teaches sync-prompt preview/apply to use typed storage
- preserves the existing prompt sync behavior for backward compatibility
