# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repo state

Spec-only workspace. No code yet — only `PLAN.md` (source of truth) and `.env.example`. Implementation follows the 17 sprints in `PLAN.md` §17, starting from a fork of the ZOI Agno base.

When asked to "implement sprint N", read `PLAN.md` §17 for scope and the referenced cross-section (e.g. Sprint 2 → §5 inventory tools, §6 schemas). Do not invent features outside PLAN.md without confirming.

## Product

WhatsApp agent "Patricia" for AMC Veículos (used-car dealer, Joinville/SC) on top of GoHighLevel (GHL). Single-tenant, PIT auth. Portuguese (pt-BR), persona rules in §2 are strict — banned phrases list matters.

## Architecture (when code lands)

2-LLM pipeline per inbound turn:
1. **Updater** (`agent/updater.py`) — structured output → `StateUpdate` (§6). Extracts collected fields, intent, sentiment, handoff flags.
2. **Responder** (`agent/responder.py`) — generates multi-bubble text (`|||` separator, max 3, 0.6–1.2s sleeps). Last bubble always = next funnel question.

Orchestrator (`orchestrator.py`) holds per-`contactId` `asyncio.Task` table for preemption; send phase wrapped in `asyncio.shield`. No `messageId` dedup — relies on preemption + GHL 12s debounce.

State storage: Postgres (Agno auto-schema). `session_state` JSONB shape in §6. Audio transcripts are ephemeral — never persisted to state.

GHL is the source of truth for:
- Inventory: Custom Value JSON `cqH4Ba3hcS0Xuzvy4izA` (~36 vehicles, 5min cache)
- FAQ: Custom Value YAML `iD172rYRHqf0aLdtGz0H` (5min cache)
- History: `GET /conversations/{id}/messages?limit=100` each turn (no local mirror)
- Tag gate: `agente-ia` — no tag → webhook 200s with no action. Removing tag = opt-out/handoff.

Outbound WhatsApp uses `POST /conversations/messages` with `type:"SMS"`.

## Endpoints (§8)

- `POST /sessions/{contactId}/greet?secret=` — sync, idempotent via `state.greeted` + `saudao_prvendas=SIM` custom field
- `POST /webhook/inbound?secret=` — tag-gated, audio→Whisper, preempts running task
- `POST /sessions/{contactId}/abandon?secret=` — closes session only, no note/workflow
- `GET /metrics` — Prometheus

All secret-protected endpoints use HMAC-compare on `?secret=` query param.

## Tools the responder can call (§5)

`search_inventory`, `get_vehicle_details`, `get_faq`, `buscar_veiculo_interesse_origem`, `registrar_nota_atendimento`, `encaminhar_para_vendedor`, `propose_slots`, `book_appointment`. Photo sending is direct GHL calls (parallel `asyncio.gather` under shield), not a tool.

## Handoff / terminal states (§10, §12)

Four terminal reasons that create note + fire workflow `b759fd01-2867-45b9-a8c8-74490793e261`: `qualificado_agendado`, `qualificado_sem_agenda`, `handoff_solicitado`, `handoff_erro`. `abandonado` is CRM-side only — no note, no workflow.

Calm human request: insist once, handoff on 2nd mention. Explicit opt-out / irritation: immediate handoff. AI-identity question: evade 1st, admit 2nd.

## Conventions

- Timezone: `America/Sao_Paulo` everywhere; ISO8601 with SP offset for appointments
- Retry: `tenacity` 3 attempts, exponential 1/2/4s (§13)
- Logging: `structlog` JSON
- Lexical preference in agent output: "veículo" (never "carro" in persona text unless quoting lead)

## Testing

Manual scenario script in §16 (C1–C24). Each sprint smoke-tests its referenced Cxx. No automated test framework chosen yet — when adding tests, ask before introducing pytest/etc.

## Pendências (§18)

- `scripts/inspect_webhook.py` must come first in Sprint 9 — real GHL inbound payload not yet mapped.
- Confirm `appointmentStatus="confirmed"` accepted by GHL on first live test.
- Abandon window (default 24h) pending client confirmation.
