# ZOI — AMC Veículos (WhatsApp Agent)

Atendente virtual "Patricia" da AMC Veículos (seminovos, Joinville/SC) sobre GHL +
WhatsApp Plugin. Pipeline 2-LLM (updater + responder), persistência Postgres,
métricas Prometheus.

Fonte de verdade do produto: [`PLAN.md`](./PLAN.md). Para Claude Code:
[`CLAUDE.md`](./CLAUDE.md).

## Setup

```bash
# 1. Postgres local
docker compose up -d

# 2. Python venv (3.11) + deps
python3.11 -m venv .venv
.venv/bin/pip install -e ".[dev]"

# 3. .env (não commitado) — copie de .env.example e preencha
cp .env.example .env
# edite .env com OPENAI_API_KEY e GHL_PIT_TOKEN
```

## Run

```bash
.venv/bin/python -m zoi_agent.main
# server em :8000 — /health, /metrics, /sessions/{cid}/greet, /webhook/inbound, /sessions/{cid}/abandon
```

## Tests

```bash
.venv/bin/pytest tests/                       # full suite (102+)
.venv/bin/pytest tests/test_orchestrator.py   # módulo específico
.venv/bin/pytest -k "C22"                     # cenário (PLAN §16)
```

## Smokes ao vivo

Todos batem em GHL real (PIT token do `.env`).

```bash
# Cliente GHL (fetch contato)
.venv/bin/python scripts/smoke_ghl.py

# Inventário (mini-LLM extrai filtros)
.venv/bin/python scripts/smoke_inventory.py "SUV automático até 80 mil"

# FAQ (YAML do Custom Value)
.venv/bin/python scripts/smoke_faq.py

# Whisper round-trip (TTS pt-BR -> transcrição)
.venv/bin/python scripts/smoke_whisper.py

# Updater LLM (6 cenários representativos)
.venv/bin/python scripts/smoke_updater.py

# Responder LLM (5 cenários)
.venv/bin/python scripts/smoke_responder.py

# Calendário (propose_slots; --book opcional cria evento real)
.venv/bin/python scripts/smoke_calendar.py
.venv/bin/python scripts/smoke_calendar.py --book

# Inspecionar payload do GHL Workflow
.venv/bin/python scripts/inspect_webhook.py     # escuta porta 9001 (ou PORT=)
ngrok http 9001                                  # tunnel para configurar workflow GHL
```

## Endpoints

| Método | Path | Auth | Função |
|---|---|---|---|
| GET | `/health` | — | status app+db |
| GET | `/metrics` | — | Prometheus |
| POST | `/sessions/{contact_id}/greet?secret=` | HMAC | saudação síncrona, idempotente (state.greeted OU custom field SAUDAÇÃO=SIM) |
| POST | `/webhook/inbound?secret=` | HMAC | gatilho do GHL; busca conv via API, strip "Received on", transcreve áudio, dispara orquestrador |
| POST | `/sessions/{contact_id}/abandon?secret=` | HMAC | fecha sessão local (`terminal_reason=abandonado`); sem nota/workflow |

Auth: query `?secret=<WEBHOOK_SECRET>`, HMAC compare_digest.

## Métricas

Expostas em `/metrics` (Prometheus):

- `zoi_turns_total{stage, intent}` — counter
- `zoi_handoff_total{reason}` — counter (qualificado_*, handoff_*)
- `zoi_qualificados_total{com_agenda}` — counter (sim/nao)
- `zoi_llm_latency_seconds_bucket{component}` — histogram (updater, responder, inventory.*, whisper)
- `zoi_ghl_request_latency_seconds_bucket{operation}` — histogram

Dashboard Grafana: `grafana/dashboard.json` (importar via UI).

## Configuração GHL (UI)

Quatro workflows (PLAN §15). Os IDs reais já estão no `.env.example`.

1. **ZOI — Saudação**: trigger contato criado com tag `agente-ia` E `SAUDAÇÃO != "SIM"`. Action HTTP POST `https://<ngrok>/sessions/{{contact.id}}/greet?secret=...`.
2. **ZOI — Webhook Inbound**: trigger mensagem recebida com tag `agente-ia`. Action HTTP POST `https://<ngrok>/webhook/inbound?secret=...`.
3. **ZOI — Abandono por inatividade**: opcional, trigger inatividade X horas. Action HTTP POST `/sessions/{{contact.id}}/abandon`.
4. **ZOI — Handoff** (`b759fd01-…`): pré-existente. Disparado pelo agent via API.

## Arquitetura — visão rápida

```
GHL Workflow ──POST──▶ /webhook/inbound ──┐
                                          │
                                          ▼
                                  orchestrator.process_turn
                                          │
                                          ▼
                          asyncio.Task table (preempção por contactId)
                                          │
                                          ▼
                          ┌──► load state (Postgres)
                          ├──► fetch GHL history (search + messages)
                          ├──► run_updater (gpt-4o, StateUpdate)
                          ├──► merge_into_state
                          ├──► _dispatch_tools (FAQ, search, photos, slots, ...)
                          ├──► book_appointment (se chosen_slot_iso)
                          ├──► run_responder (gpt-4o, multi-bubble)
                          ├──► asyncio.shield(send: fotos paralelo + bolhas seq)
                          ├──► encaminhar_para_vendedor (se terminal)
                          └──► save state
```

Falha do updater/responder → catch no orchestrator → `terminal_reason=handoff_erro` + terminal action.

## Cenários (PLAN §16)

Todos cobertos por tests unitários ou smokes ao vivo. Veja `tests/test_inbound.py`,
`tests/test_orchestrator.py`, `tests/test_calendar.py`, `tests/test_photos.py`,
`tests/test_terminal.py`, scripts em `scripts/smoke_*.py`.

## Pendências (PLAN §18)

- Confirmar `appointmentStatus="confirmed"` aceito no calendar real (`--book` no `smoke_calendar.py`).
- Janela de abandono é tratada direto no CRM (PLAN §17 S14 simplificado).
- ngrok free: URL muda a cada restart — re-config workflows GHL ao reiniciar.
