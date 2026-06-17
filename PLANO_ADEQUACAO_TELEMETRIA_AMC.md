# PLANO DE ADEQUAÇÃO DE TELEMETRIA — Agente "Patricia" (AMC Veículos)

> Objetivo: adequar **este** agente (`agente-amc`) para alimentar o **ZOI Performance Hub** com telemetria operacional, financeira e comercial.
> Escopo: **plano apenas — sem código**. Referências a arquivos reais (commit `2b02953`).

---

## 1. SITUAÇÃO ATUAL

### Banco (`db/models.py`, `db/sessions.py`)
- PostgreSQL 16 (asyncpg + SQLAlchemy 2.0 async). Schema criado em `init_schema()` no startup (`main.py` lifespan).
- **Uma tabela:** `sessions` — `contact_id` (PK, String64), `state` (JSONB = `SessionState`), `terminal_reason` (String64), `created_at`, `updated_at`.
- Sem tabela de mensagens, eventos, tokens ou custo.

### Telemetria (`metrics.py`)
- Prometheus client **in-memory** (zera em restart). Exposto em `GET /metrics` (`main.py`):
  - `zoi_turns_total{stage,intent}`, `zoi_handoff_total{reason}`, `zoi_qualificados_total{com_agenda}`
  - `zoi_llm_latency_seconds{component}`, `zoi_ghl_request_latency_seconds{operation}`
- Dashboard Grafana pronto (`grafana/dashboard.json`). **Nenhuma métrica de token/custo.**

### CRM (`ghl/*.py`)
- GoHighLevel via **PIT token** (Bearer, `Version: 2021-07-28`, base `services.leadconnectorhq.com`).
- Usa: contacts, conversations (search/messages/send), tags, custom values (estoque/FAQ), workflows, calendars (free-slots, appointments).
- **Sem Opportunities / Pipeline** → eixo comercial cego.

### Logs (`logging.py`)
- structlog **JSON** em stdout. Eventos já estruturados (`turn_terminal`, `calendar_booked`, `audio_transcribed`, `team_turn_start`, etc.). **Sem arquivo, sem coletor, sem retenção.**

### IA (pipeline por turno — `orchestrator._run_turn`, linha 346)
1. **Updater LLM** — `agent/updater.py` → `llm.parse_structured` (`beta.chat.completions.parse`, gpt-4o).
2. **EstoqueExpert** — `team/runner.py` `arun()` (Agno Agent, gpt-4o).
3. **Patricia** — `team/runner.py` `run_team_turn` (linha 394) `arun()` (Agno Agent, gpt-4o).
4. **Whisper** — `audio/whisper.py` `_transcribe_bytes` (whisper-1), só quando há áudio.
- `llm.py` (`parse_structured`, `chat_text`) **descarta `response.usage`**. Agentes Agno com `telemetry=False` (`runner.py:350`).

---

## 2. GAPS

### Operacional
- `conversationId` não persistido (resolvido por turno via `/conversations/search`) → eventos sem chave de conversa estável.
- Sem tabela de mensagens/eventos → histórico vive 100% no GHL (rate/latência, sem replay local).
- Métricas Prometheus in-memory → sem TSDB garantida, contadores perdidos em restart.
- Abandono (`endpoints/abandon.py` → `terminal_reason="abandonado"`) **sem métrica** Prometheus.
- Follow-up **inexistente** (sem feature no código).

### Financeiro
- **Tokens não capturados** em nenhum dos 3 LLMs nem no Agno. `response.usage` descartado.
- **Custo inexistente:** sem tabela de preços, sem fórmula, sem cálculo.
- Whisper não contabilizado (custo por segundo/áudio ignorado).
- Sem custo atribuível por conversa (nem há onde gravar).

### Comercial
- Sem Opportunities/Pipeline → impossível atribuir oportunidade/venda à IA.
- Desfecho comercial só inferível de `terminal_reason` + nota GHL (texto livre).

---

## 3. ADEQUAÇÕES NECESSÁRIAS

### Banco — tabela append-only de eventos
Nova tabela `agent_events` (mesma engine async, criada em `init_schema()`):

| Campo | Tipo | Nota |
|---|---|---|
| `id` | BIGSERIAL PK | |
| `event_type` | String(40) | enum lógico (§5) |
| `contact_id` | String(64) | índice; FK lógica → `sessions` |
| `conversation_id` | String(64) null | **persistir** (ver CRM) |
| `agent` | String(32) | `"patricia-amc"` (multi-tenant no Hub) |
| `payload` | JSONB | campos específicos do evento |
| `tokens_input` | Int null | só LLM_CALL |
| `tokens_output` | Int null | só LLM_CALL |
| `tokens_total` | Int null | só LLM_CALL |
| `model` | String(40) null | só LLM_CALL/WHISPER |
| `cost_usd` | Numeric(12,6) null | calculado na escrita |
| `created_at` | timestamptz | default now |

Índices: `(agent, created_at)`, `(contact_id, created_at)`, `(event_type, created_at)`. Append-only (sem UPDATE/DELETE). Helper `db/events.py::emit_event(...)`.

### Logs
- Manter structlog JSON (vantagem de ingestão). Adicionar `conversation_id` ao contexto via `structlog.contextvars` no início de `_run_turn`.
- Logs continuam complemento; **fonte de verdade da telemetria = `agent_events`** (não parsear stdout).

### Tokens — capturar `response.usage` nos 3 LLMs + Agno
- **Updater:** alterar `llm.py::parse_structured`/`chat_text` para ler `resp.usage` (`prompt_tokens`, `completion_tokens`, `total_tokens`) e retornar junto (ex.: `tuple[T, Usage]` ou objeto `LLMResult`). Updater (`agent/updater.py`) repassa ao emissor de evento.
- **Agno (EstoqueExpert + Patricia):** `arun()` retorna `RunOutput` com `.metrics` (input/output/total tokens por run). Em `team/runner.py` (`_call_inventory_expert_with_retry`, `run_team_turn`) ler `result.metrics` após cada `arun`. `telemetry=False` afeta envio à nuvem Agno, **não** o objeto de métricas local — manter `telemetry=False`, ler métricas do `RunOutput`. Fallback se `.metrics` vier vazio: contagem aproximada via `tiktoken` sobre prompt/resposta.
- **Ponto de consolidação:** `orchestrator._run_turn` agrega os 3 (+Whisper) e emite **um `LLM_CALL` por componente** com `component ∈ {updater, estoque_expert, patricia}`.

### Custos — tabela `pricing`
Nova tabela `pricing` (seed estático, versionada por data):

| Campo | Tipo |
|---|---|
| `model` | String(40) PK-parcial |
| `price_input_per_1k` | Numeric(12,6) USD |
| `price_output_per_1k` | Numeric(12,6) USD |
| `price_audio_per_min` | Numeric(12,6) null (Whisper) |
| `effective_from` | date |

Fórmula (aplicada em `emit_event` para `LLM_CALL`):
```
cost_usd = (tokens_input/1000)*price_input_per_1k + (tokens_output/1000)*price_output_per_1k
```
Whisper (`WHISPER_TRANSCRIPTION`): `cost_usd = duracao_min * price_audio_per_min`.
Custo por conversa = `SUM(cost_usd) GROUP BY conversation_id` (ou `contact_id` se conv. ausente).

### Whisper
- `audio/whisper.py::_transcribe_bytes`: capturar duração do áudio (já há bytes; derivar segundos ou usar `verbose_json` para `duration`). Emitir `WHISPER_TRANSCRIPTION` com `model=whisper-1`, `duracao_min`, `cost_usd`.

### CRM — persistir `conversation_id`
- Em `_fetch_history` (`orchestrator.py:307`, já chama `/conversations/search`), capturar o `conversationId` resolvido e **gravar em `SessionState`** (novo campo `conversation_id: str | None`) + propagar a todos os eventos do turno. Elimina re-resolução e dá chave estável ao Hub.

### Oportunidades (Fase 3)
- Adicionar `ghl/opportunities.py`: `POST /opportunities` (criar) + `PUT /opportunities/{id}` (mover stage) no handoff qualificado. Emitir `OPPORTUNITY_CREATED`/`OPPORTUNITY_UPDATED`. Requer config `GHL_PIPELINE_ID` + stage IDs (hoje inexistentes no `.env`).

---

## 4. ESTRATÉGIA DE INTEGRAÇÃO COM O HUB

**Recomendado: pull direto via Postgres sobre `agent_events`.**
- Hub lê incrementalmente por `WHERE created_at > :cursor ORDER BY created_at` (cursor por `id`/timestamp). Tabela append-only = ideal para CDC simples.
- `agent` discrimina o tenant; `conversation_id` (agora persistido) dá granularidade de conversa.
- **Prometheus permanece** para dashboards operacionais near-real-time (Grafana já pronto) — mas **não** é fonte financeira (in-memory, sem custo). Hub não depende de scrape.
- **Opcional:** endpoint `GET /usage?since=&secret=` (HMAC, igual aos demais) servindo agregados de `agent_events` — útil se o Hub não puder acessar o Postgres diretamente. Secundário ao pull SQL.

Decisão: **leitura SQL direta da tabela `agent_events` + `conversation_id` persistido**. Prometheus = operacional; `agent_events` = financeiro/comercial/auditável.

---

## 5. EVENTOS RECOMENDADOS (mapeados ao código real)

| Evento | Ponto de emissão | Campos | Complexidade |
|---|---|---|---|
| `CONVERSATION_STARTED` | `endpoints/greet.py` ao setar `greeted=true` | contact_id, conversation_id, veiculo_origem, ts | Baixa |
| `CONVERSATION_COMPLETED` | `orchestrator._run_turn` quando `terminal_reason ∈ {qualificado_agendado, qualificado_sem_agenda}` (linha ~534) | contact_id, terminal_reason, com_agenda, collected | Baixa |
| `HANDOFF_CREATED` | `tools/terminal.py::encaminhar_para_vendedor` (já incrementa `HANDOFF_TOTAL`) | contact_id, reason (`handoff_solicitado`/`handoff_erro`), workflow_id | Baixa |
| `APPOINTMENT_CREATED` | `tools/calendar.py::book_appointment` após `calendar_booked` | contact_id, appointment_id, slot_iso, modelo | Baixa |
| `CONVERSATION_ABANDONED` | `endpoints/abandon.py` (hoje sem métrica) | contact_id, ts | Baixa |
| `LLM_CALL` | `orchestrator._run_turn` (1 por componente) após capturar usage | component, model, tokens_input/output/total, cost_usd, latency | **Média** (depende §3 tokens) |
| `WHISPER_TRANSCRIPTION` | `audio/whisper.py::_transcribe_bytes` | model, duracao_min, chars, cost_usd | Média |
| `FOLLOWUP_STARTED` | **FEATURE INEXISTENTE** — sem código hoje | (futuro) contact_id, motivo, ts | Alta (criar feature) |
| `FOLLOWUP_FINISHED` | **FEATURE INEXISTENTE** | (futuro) contact_id, resultado, ts | Alta (criar feature) |

> `FOLLOWUP_*` marcados como inexistentes — só viáveis após implementar máquina de follow-up (scheduler + reengajamento), fora do escopo atual.

---

## 6. PLANO DE EXECUÇÃO

### ✅ Fase 1 — Quick win financeiro — IMPLEMENTADA
- ✅ Tabelas `agent_events` + `pricing` (seed gpt-4o, gpt-4o-mini, whisper-1) em `init_schema()` → `db/models.py`, `db/events.py`.
- ✅ `llm.py`: captura `response.usage` (Updater) via `_record_usage`.
- ✅ `team/runner.py`: lê `RunOutput.metrics` (EstoqueExpert + Patricia) via `_record_agno_usage`.
- ✅ `orchestrator._run_turn`: sink por turno (`usage.py`, ContextVar) + `_flush_usage_events` em `finally` → emite `LLM_CALL` (3 componentes) com custo calculado.
- ✅ `whisper.py`: `verbose_json` → `duration` → `WHISPER_TRANSCRIPTION` com custo/min.
- **Resultado:** custo por turno/conversa mensurável. `SUM(cost_usd) GROUP BY conversation_id`.

### ✅ Fase 2 — Eventos padronizados + operacional — IMPLEMENTADA
- ✅ `SessionState.conversation_id` persistido (gravado em `_run_turn_inner` após `_fetch_history`, propaga no merge).
- ✅ Eventos: `CONVERSATION_STARTED` (greet), `CONVERSATION_COMPLETED`/`HANDOFF_CREATED` (chokepoint `encaminhar_para_vendedor`), `APPOINTMENT_CREATED` (orchestrator pós-booking), `CONVERSATION_ABANDONED` (abandon).
- ✅ Contador Prometheus `zoi_abandoned_total` (`metrics.py`) incrementado no `/abandon`.
- ⏳ (Opcional) endpoint `GET /usage` — não implementado; Hub usa pull SQL direto.

### ⏳ Fase 3 — Comercial + follow-up — PENDENTE
- `ghl/opportunities.py` (criar/mover Opportunity no handoff qualificado) + config `GHL_PIPELINE_ID`/stages → `OPPORTUNITY_CREATED/UPDATED`. **Bloqueio:** pipeline/stage IDs do cliente não definidos.
- Implementar follow-up (scheduler) → `FOLLOWUP_STARTED/FINISHED`. **Feature nova** — fora do código atual.

> Status de verificação: `py_compile` OK em todos os arquivos; suíte de testes **118 passed** (5 falhas + 1 erro de coleta são pré-existentes, em `test_terminal.py`/`test_question_planner.py`/`test_inventory.py`, não tocados nesta adequação).

---

## 7. RESUMO EXECUTIVO

O agente já tem base operacional sólida (Prometheus + estado Postgres + CRM GHL amplo) e logs JSON estruturados — bom ponto de partida para ingestão. Os bloqueios são **financeiro** (tokens/custo zerados: `llm.py` descarta `usage`, Agno sem leitura de métricas, sem `pricing`) e **persistência** (sem tabela de eventos, `conversationId` volátil). A correção é direta e barata: capturar `response.usage` (Updater) e `RunOutput.metrics` (Agno) num único ponto de consolidação (`_run_turn`), gravar numa tabela append-only `agent_events` com custo calculado via `pricing`, e persistir `conversation_id`. O Hub consome por **pull SQL** sobre `agent_events`; Prometheus segue para dashboards operacionais. Comercial (Opportunities) e follow-up ficam para a Fase 3 por exigirem novas features/integrações. Esforço total estimado: **Fase 1 ~3–4 dias**, Fase 2 ~3 dias, Fase 3 ~1 sprint.

---

## 8. SCORE DE ADERÊNCIA AO HUB

| Dimensão | Baseline | Pós Fase 1+2 (atual) | Projetado pós Fase 3 |
|---|---|---|---|
| Banco (persistência telemetria) | 3 | 8 | 8 |
| Logs | 5 | 6 | 6 |
| Custos/Tokens | 0 | 9 | 9 |
| CRM | 8 | 8 | 9 (com Opportunities) |
| Conversas (chave estável) | 6 | 9 | 9 |
| Telemetria | 5 | 8 | 8 |
| Monitoramento | 5 | 7 | 7 |
| **Média** | **4,6 / 10** | **~7,6 / 10** | **~8,0 / 10** |

> Eixo comercial (CRM Opportunities) é o único item ainda em baseline — depende da Fase 3 e de config do cliente.

**Após Fase 1+2:** ~7,5 (financeiro + operacional resolvidos). **Fase 3** fecha o eixo comercial → ~8,0.
Classificação projetada: 🟢 **Pronto para Integração** ao ZOI Performance Hub.

---

## ADENDO — Alinhamento de Frota v1 (decisões centrais, sobrepõem o acima)

### A. Contrato canônico de eventos
A tabela `agent_events` DEVE seguir o envelope do `CONTRATO_EVENTOS_CANONICO.md` (v1):
`event_id` (UUID, idempotência), `schema_version=1`, `event_type` (vocabulário canônico), `client="amc"`, `agent="patricia-amc"`, `contact_id`, `conversation_id`, `occurred_at` (ISO8601 UTC), `payload` (JSONB por tipo). `LLM_CALL`/`WHISPER_TRANSCRIPTION` seguem §3.1/§3.2 do contrato (tokens crus + custo).

### B. Custo DUPLO em BRL (decisão da frota)
O agente CALCULA custo localmente em BRL (não delega ao Hub) — tabela `pricing` local com `usd_brl_rate` + `pricing_version`. Cada `LLM_CALL`/`WHISPER` grava `cost_usd`, `cost_brl`, `usd_brl_rate`, `pricing_version` **E** os tokens crus. O Hub recalcula `hub_cost_brl` para reconciliação. Ajuste de §3.3: a tabela `pricing` ganha `usd_brl_rate` e custo passa a ser persistido em USD e BRL.

### C. Integração — transporte PULL SQL
Confirmado: Hub lê `agent_events` direto via Postgres por cursor `id`. Adapter: `hub/adapters/postgres_adapter.py` (registry `patricia-amc`). Garantir coluna `id` BIGSERIAL + índice `(occurred_at)`. Endpoint `/usage` permanece opcional.

### D. LGPD — adiado para pós-MVP
Mascaramento/criptografia de PII NÃO entra no MVP. Registrar como dívida técnica pós-MVP; não bloqueia Fase 1.
