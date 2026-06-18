# RELATÓRIO DE ADEQUAÇÃO DE TELEMETRIA — APLICAÇÕES IMPLEMENTADAS

> Agente "Patricia" (AMC Veículos) · adequação para alimentar o **ZOI Performance Hub**.
> Escopo executado: **Fase 1 (financeiro) + Fase 2 (operacional) + Correções v2 (contrato canônico)**. Fase 3 (comercial) pendente.
> Base: commit `2b02953`. Status: working tree (sem commit).
> **Correções v2 alinham ao `CONTRATO_EVENTOS_CANONICO.md` (envelope v1).** Ver §8.

---

## 1. RESUMO

| Item | Antes | Depois |
|---|---|---|
| Tokens capturados | ❌ `response.usage` descartado | ✅ Updater + EstoqueExpert + Patricia + Whisper |
| Custo USD | ❌ inexistente | ✅ calculado por chamada via tabela `pricing` |
| Persistência de telemetria | ❌ 1 tabela (`sessions`) | ✅ + `agent_events` (append-only) + `pricing` |
| conversationId | ❌ volátil (por turno) | ✅ persistido em `SessionState` |
| Eventos de negócio | ❌ só Prometheus in-memory | ✅ 7 tipos gravados em `agent_events` |
| Abandono | ❌ sem métrica | ✅ evento + contador Prometheus |

**Score de aderência ao Hub:** `4,6 → ~7,6 / 10`.

**Arquivos:** 2 novos, 13 modificados (3 de teste). ~250 linhas.

---

## 2. ARQUIVOS NOVOS

### `src/zoi_agent/usage.py`
Coletor de uso de LLM por turno. `ContextVar` (isolado por Task asyncio → seguro sob preempção e turnos concorrentes).
- `start_turn()` — inicia sink no topo de `_run_turn`.
- `record(component, model, tokens_input/output/total, audio_minutes)` — cada chamada LLM registra. No-op fora de turno (scripts).
- `drain()` — orchestrator drena no `finally`.

### `src/zoi_agent/db/events.py`
Emissão de eventos + preços.
- `emit_event(...)` — grava em `agent_events`. **Tolerante a falha** (try/except → log, nunca derruba o turno do lead). Gated por `settings.telemetry_events_enabled`.
- `compute_cost(model, tokens_input, tokens_output, audio_minutes)` — fórmula:
  ```
  cost_usd = (tokens_input/1000)*price_input_per_1k
           + (tokens_output/1000)*price_output_per_1k
           + (audio_minutes * price_audio_per_min)   # Whisper
  ```
- `seed_pricing()` — idempotente, roda no startup. Seeds (USD): gpt-4o `0.0025/0.01` por 1k; gpt-4o-mini `0.00015/0.0006`; whisper-1 `0.006`/min.
- Cache de preços em memória (TTL 300s).

---

## 3. ARQUIVOS MODIFICADOS

### Banco
**`db/models.py`** — 2 tabelas novas (mesma `Base`, criadas por `create_all`):

`agent_events`:
| Campo | Tipo |
|---|---|
| `id` | BIGSERIAL PK |
| `event_type` | String(40) |
| `agent` | String(32) — `patricia-amc` |
| `contact_id` | String(64) |
| `conversation_id` | String(64) null |
| `component` | String(32) null |
| `model` | String(40) null |
| `tokens_input/output/total` | Integer null |
| `cost_usd` | Numeric(12,6) null |
| `payload` | JSONB |
| `created_at` | timestamptz |

Índices: `(agent,created_at)`, `(contact_id,created_at)`, `(event_type,created_at)`, `(conversation_id)`.

`pricing`: `model`+`effective_from` (PK), `price_input_per_1k`, `price_output_per_1k`, `price_audio_per_min`.

**`db/sessions.py`** — `init_schema()` agora chama `seed_pricing()` após `create_all`.

### Captura de tokens
**`llm.py`** — `_record_usage(resp, component, model)` lê `resp.usage` (`prompt_tokens`/`completion_tokens`/`total_tokens`) em `parse_structured` (Updater) e `chat_text`.

**`team/runner.py`** — `_record_agno_usage(result, component, model)` lê `RunOutput.metrics` após cada `arun()` (EstoqueExpert + Patricia). Defensivo a shape int/list entre versões do Agno; `_int_metric` soma listas. `telemetry=False` não afeta métricas locais.

**`audio/whisper.py`** — `response_format` `"text"`→`"verbose_json"` p/ obter `duration`; registra `audio_minutes = duration/60`.

### Orquestração e eventos
**`orchestrator.py`**
- `_run_turn` virou **wrapper**: `start_turn()` no topo + `_flush_usage_events()` em `finally` → garante emissão em **qualquer** saída (inclusive `handoff_erro`). Lógica original movida p/ `_run_turn_inner(ctx)`.
- `_flush_usage_events` drena sink → 1 `LLM_CALL` (updater/estoque_expert/patricia) ou `WHISPER_TRANSCRIPTION` por registro, com custo + `conversation_id`.
- Persiste `state.conversation_id` após `_fetch_history` (propaga no `model_copy` do merge).
- Emite `APPOINTMENT_CREATED` após `book_appointment` OK.

**`tools/handoff.py`** — `encaminhar_para_vendedor` (chokepoint dos 4 terminais) emite: `CONVERSATION_COMPLETED` se `terminal_reason ∈ {qualificado_agendado, qualificado_sem_agenda}`, senão `HANDOFF_CREATED`.

**`endpoints/greet.py`** — emite `CONVERSATION_STARTED` após saudação.

**`endpoints/abandon.py`** — emite `CONVERSATION_ABANDONED` (3 caminhos) + incrementa `zoi_abandoned_total`.

### Schema / métricas / config
- **`agent/schemas.py`** — `SessionState.conversation_id: str | None`.
- **`metrics.py`** — counter `zoi_abandoned_total`.
- **`config.py`** — `agent_name="patricia-amc"`, `telemetry_events_enabled=True`.
- **`.env.example`** — `AGENT_NAME`, `TELEMETRY_EVENTS_ENABLED`.

### Testes
`test_handoff.py`, `test_greet.py`, `test_abandon.py` — `emit_event` mockado (`AsyncMock`) p/ evitar conexão DB em unit test.

---

## 4. EVENTOS EMITIDOS (`agent_events`)

| event_type | Origem | Tokens/Custo | Chave |
|---|---|---|---|
| `LLM_CALL` | orchestrator (flush) | ✅ | component, model |
| `WHISPER_TRANSCRIPTION` | orchestrator (flush) | ✅ (áudio) | audio_seconds (custo = ceil(min)) |
| `CONVERSATION_STARTED` | greet | — | veiculo_origem |
| `CONVERSATION_COMPLETED` | handoff | — | terminal_reason, com_agenda |
| `HANDOFF_CREATED` | handoff | — | terminal_reason, handoff_reason |
| `APPOINTMENT_CREATED` | orchestrator | — | slot_iso, appointment_id |
| `CONVERSATION_ABANDONED` | abandon | — | — |

---

## 5. CONSUMO PELO HUB

Pull SQL incremental sobre `agent_events` (cursor por `id`). `agent` discrimina tenant.

```sql
-- Custo por conversa
SELECT conversation_id, SUM(cost_usd) AS custo_usd
FROM agent_events
WHERE agent='patricia-amc' AND event_type IN ('LLM_CALL','WHISPER_TRANSCRIPTION')
GROUP BY conversation_id;

-- Funil operacional (24h)
SELECT event_type, COUNT(*)
FROM agent_events
WHERE created_at > now() - interval '24 hours'
GROUP BY event_type;

-- Ingestão incremental
SELECT * FROM agent_events WHERE id > :last_id ORDER BY id LIMIT 1000;
```

Prometheus (`/metrics`) permanece p/ dashboards operacionais near-real-time (não é fonte financeira).

---

## 6. VERIFICAÇÃO

- `py_compile`: OK em todos os arquivos.
- Suíte (`pytest`): **118 passed**.
- Falhas **pré-existentes** (provado via `git stash` em HEAD limpo, não tocadas nesta adequação):
  - `test_terminal.py` (4) — drift de wording da nota consolidada.
  - `test_question_planner.py` (1).
  - `test_inventory.py` (1, erro de coleta) — importa `InventoryFilters` inexistente.
- Telemetria à prova de falha: erro em `emit_event`/flush nunca interrompe atendimento.

---

## 7. PENDENTE — FASE 3 (comercial)

- `ghl/opportunities.py`: `POST /opportunities` + `PUT /opportunities/{id}` no handoff qualificado → **`OPPORTUNITY_CREATED` / `OPPORTUNITY_UPDATED`** (nomes canônicos — contrato §3). **Bloqueio:** `GHL_PIPELINE_ID` + stage IDs do cliente não definidos → manter gated em config.
- Follow-up: feature nova (scheduler) → `FOLLOWUP_STARTED/FINISHED`.
- (Opcional) endpoint `GET /export/events?since=&secret=` (transporte HTTP do contrato §5) — AMC é Postgres, Hub usa pull SQL direto; export HTTP só se exigido.

---

## 8. CORREÇÕES v2 — CONTRATO CANÔNICO v1

Rodada de correção pós-validação central. Alinha o agente ao envelope obrigatório de toda a frota (`CONTRATO_EVENTOS_CANONICO.md`).

### 8.1 Envelope canônico (`db/models.py`, `db/events.py`)
`agent_events` ganhou as colunas do envelope obrigatório (§2 do contrato):

| Coluna nova | Tipo | Origem |
|---|---|---|
| `event_id` | String(36) **UNIQUE** | `uuid4()` por evento — Hub deduplica por aqui |
| `schema_version` | Integer (=1) | constante `SCHEMA_VERSION` |
| `client` | String(32) (=`amc`) | `settings.client` |
| `occurred_at` | timestamptz (UTC) | `datetime.now(timezone.utc)` no emit (≠ `created_at` = insert time) |
| `reasoning_tokens` | Integer null | modelos reasoning |
| `cost_brl` | Numeric(12,6) | custo BRL |
| `usd_brl_rate` | Numeric(12,6) | câmbio usado |
| `pricing_version` | String(32) | versão do pricing |

`agent` (`patricia-amc`) mantido junto de `client`. Migração additiva via `create_all` (idempotente). Índices reorientados p/ `occurred_at` + `event_id` único.

### 8.2 Custo duplo USD + BRL (`db/events.py`, `db/models.py`)
- `pricing` migrada p/ **forma canônica §4**: linha por `model`+`kind` (`input|output|reasoning|audio_minute`), `price_usd` por **1M tokens** (ou por minuto), + `usd_brl_rate` + `pricing_version` versionados. PK `(model, kind, effective_from)`.
- Seed: gpt-4o `2.50/10.00`, gpt-4o-mini `0.15/0.60` (por 1M); whisper-1 `0.006`/min; `usd_brl_rate=5.40`, `pricing_version=2026-06-17`.
- `compute_cost` retorna `CostResult(cost_usd, cost_brl, usd_brl_rate, pricing_version)`. Fórmula **idêntica à do Hub** (reconciliação):
  ```
  cost_usd = in/1e6*price(input) + out/1e6*price(output) + reasoning/1e6*price(reasoning)
  whisper: cost_usd = ceil(audio_seconds/60) * price(audio_minute)
  cost_brl = cost_usd * usd_brl_rate
  ```
- Tokens crus seguem no payload p/ o Hub recalcular `hub_cost_brl`.

### 8.3 Captura estendida (`usage.py`, `llm.py`, `runner.py`, `whisper.py`)
`UsageRecord` ganhou `reasoning_tokens`, `audio_seconds` (antes `audio_minutes`), `latency_ms`. `llm.py` lê `completion_tokens_details.reasoning_tokens` + latência; `runner.py` mede wall-time de cada `arun`; `whisper.py` grava `audio_seconds` cru.

### 8.4 Envelope de `LLM_CALL` (exemplo real emitido)
```json
{
  "event_id": "9f2c0d1e-...-uuid",
  "schema_version": 1,
  "event_type": "LLM_CALL",
  "client": "amc",
  "agent": "patricia-amc",
  "contact_id": "ghl_abc123",
  "conversation_id": "ghl_conv_789",
  "occurred_at": "2026-06-17T18:42:10Z",
  "payload": {
    "component": "patricia",
    "model": "gpt-4o",
    "input_tokens": 2000,
    "output_tokens": 700,
    "total_tokens": 2700,
    "reasoning_tokens": null,
    "cost_usd": 0.012,
    "cost_brl": 0.0648,
    "usd_brl_rate": 5.40,
    "pricing_version": "2026-06-17",
    "latency_ms": 1840
  }
}
```

### 8.5 SQL de pull atualizado (Hub)
```sql
-- Ingestão incremental idempotente (dedup por event_id no Hub)
SELECT event_id, schema_version, event_type, client, agent,
       contact_id, conversation_id, occurred_at, payload
FROM agent_events
WHERE id > :last_id
ORDER BY id
LIMIT 1000;

-- Custo BRL por conversa
SELECT conversation_id, SUM(cost_brl) AS custo_brl, SUM(cost_usd) AS custo_usd
FROM agent_events
WHERE client='amc' AND event_type IN ('LLM_CALL','WHISPER_TRANSCRIPTION')
GROUP BY conversation_id;
```

### 8.6 Validação (deliverable v2)
`tests/test_canonical_envelope.py` (4 testes, **passando**) exercita `compute_cost` + `emit_event` reais:
- `event_id` é UUID válido e **único** entre eventos.
- `schema_version=1`, `client="amc"`, `agent="patricia-amc"`.
- `occurred_at` tz-aware UTC.
- `cost_usd>0` e `cost_brl>0`, com `cost_brl == cost_usd × 5.40`.
- Whisper: `ceil(90s/60)=2 min → USD 0.012 / BRL 0.0648`.

Suíte total: **122 passed** (5 falhas pré-existentes inalteradas).
