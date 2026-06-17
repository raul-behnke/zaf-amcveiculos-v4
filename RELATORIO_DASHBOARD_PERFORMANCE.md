# RELATÓRIO TÉCNICO — Integração ao Dashboard de Performance de Agentes

> Análise efetiva do repositório `agente-amc` (commit `2b02953`). Realizada lendo código-fonte, estrutura de diretórios, configuração, modelo de banco, env vars e integrações. Itens não encontrados marcados como **NÃO IDENTIFICADO**.

---

## 1. IDENTIFICAÇÃO DO PROJETO

- **Nome do Projeto:** ZOI — Agente WhatsApp "Patricia" (AMC Veículos)
- **Cliente:** AMC Veículos (seminovos, Joinville/SC)
- **Versão:** `0.1.0` (`pyproject.toml`)
- **Ambiente:** Self-hosted single-tenant. Dev local (docker-compose Postgres) + deploy via `deploy/` (Dockerfile + nginx + compose.prod). Produção: **NÃO IDENTIFICADO** se já em ar (há `RELATORIO_PRODUCAO_PATRICIA.md`).
- **Repositório:** local git, branch `main` (remote **NÃO IDENTIFICADO** no escopo lido)
- **Responsável Técnico:** Raul Behnke (git user) / ZOI Publicidade

**Resumo Executivo:** Agente SDR de WhatsApp sobre GoHighLevel (GHL). Qualifica leads de carros usados via pipeline multi-LLM por turno (Updater estruturado + Team Agno Patricia/EstoqueExpert), persiste apenas `session_state` em Postgres, integra GHL (contatos, conversas, custom values, tags, workflows, calendário). Métricas via Prometheus (eventos de negócio, latências) — **sem armazenamento de tokens nem cálculo de custo**. Observabilidade de negócio razoável; observabilidade financeira inexistente.

---

## 2. STACK TECNOLÓGICA

| Camada | Tecnologia |
|---|---|
| **Backend** | Python 3.11, FastAPI ≥0.115, Uvicorn |
| **Frontend** | NÃO IDENTIFICADO (sem UI; canal é WhatsApp via GHL) |
| **Banco de Dados** | PostgreSQL 16 (asyncpg) |
| **ORM** | SQLAlchemy 2.0 async (`mapped_column`, JSONB) |
| **Framework de IA** | Agno ≥1.0 (Team/Agent) + OpenAI SDK ≥1.54 |
| **LLM Provider** | OpenAI |
| **Modelos** | Updater: `gpt-4o` · Patricia (leader): `gpt-4o` · EstoqueExpert: `gpt-4o` · Inventory extractor (legacy): `gpt-4o-mini` · Áudio: `whisper-1` |
| **Serviços Externos** | GoHighLevel (LeadConnector API), OpenAI, OpenAI Whisper |
| **Infraestrutura** | Docker / docker-compose; nginx reverse proxy (deploy/) |
| **Hospedagem** | Self-hosted (mesmo host Postgres). Provedor: NÃO IDENTIFICADO |
| **Filas** | NÃO IDENTIFICADO (sem broker; preempção via tabela `asyncio.Task` em memória) |
| **Cache** | In-memory TTL (`cache.py`) — estoque 300s, FAQ 300s. Sem Redis. |
| **Storage** | Postgres (estado). Áudio transcrito é efêmero (não persistido). |

---

## 3. ARQUITETURA GERAL

**Fluxo de atendimento (turno inbound):**
1. **Entrada:** `POST /webhook/inbound?secret=` recebe payload GHL. Extrai `contactId`.
2. **Tag gate:** exige tag `agente-ia` no contato; sem tag → 200 sem ação.
3. **Pré-processamento:** busca histórico GHL (`/conversations/search` + `/conversations/{id}/messages?limit=100`), agrega burst de mensagens desde último outbound, limpa metadados, separa áudio/imagem.
4. **Áudio:** se houver, Whisper transcreve URLs (efêmero).
5. **Processamento:** `orchestrator.process_turn` → preempção (cancela task anterior do mesmo `contactId`) → `_run_turn`:
   - Carrega `session_state` do Postgres.
   - **Updater LLM** (`beta.chat.completions.parse`) → `StateUpdate`.
   - Merge no estado; planejamento determinístico da próxima pergunta; checagem de escalação/agendamento.
   - **Team Agno** (EstoqueExpert → Patricia) gera bubbles + IDs de veículos.
6. **Resposta:** envio multi-bubble (`|||`, máx 3, sleeps 0.6–1.2s) via `POST /conversations/messages` (`type:SMS`), sob `asyncio.shield`. Fotos via GHL paralelo.
7. **Persistência:** salva `session_state` (JSONB) no Postgres.
8. **Integrações terminais:** se `terminal_reason` setado → remove tag, cria nota consolidada, dispara workflow GHL.

**Diagrama textual:**
```
WhatsApp (lead)
   ↓
GHL WhatsApp Plugin
   ↓
POST /webhook/inbound  ── tag-gate (agente-ia)
   ↓
[GHL] busca histórico + transcreve áudio (Whisper)
   ↓
Orchestrator (preempção por contactId)
   ↓
Updater LLM (gpt-4o, structured) → StateUpdate
   ↓
Team Agno: EstoqueExpert → Patricia (gpt-4o) → bubbles
   ↓
POST /conversations/messages (SMS)  [shield]  + fotos
   ↓
Postgres (session_state JSONB)
   ↓
[terminal] remove tag + nota + workflow GHL
   ↓
Prometheus /metrics  (sem tokens/custo)
```

---

## 4. BANCO DE DADOS

- **Tipo:** PostgreSQL 16
- **Host:** dev `localhost:5433→5432` (docker `zoi_postgres`); DB `zoi_agent`, user `zoi`. Prod: NÃO IDENTIFICADO.
- **Quantidade de tabelas:** **1** (uma) — schema criado por `Base.metadata.create_all` no startup. Agno **não** persiste sessões/mensagens (agentes com `telemetry=False`, sem storage Agno configurado).

**Todas as tabelas:**

### Tabela: `sessions`
- **Finalidade:** estado completo da conversa por contato (funil, flags, agendamento, motivo terminal).
- **Campos principais:**
  - `contact_id` `String(64)` **PK**
  - `state` `JSONB` (shape `SessionState`, ver §5)
  - `terminal_reason` `String(64)` nullable
  - `created_at` `timestamptz` (server default now)
  - `updated_at` `timestamptz` (onupdate now)
- **Relacionamentos:** nenhum (tabela única, sem FKs).

**Mapa de armazenamento por domínio:**

| Domínio | Onde está |
|---|---|
| Conversas | ❌ Não há tabela. Vivem no GHL (source of truth), buscadas a cada turno. Local: só `session_state.contact_id` |
| Mensagens | ❌ Não persistidas localmente. Vêm de GHL `/conversations/{id}/messages` por turno |
| Leads | Parcial: campos coletados em `sessions.state.collected`. Lead "oficial" é o contato GHL |
| Contatos | GHL (não há mirror local; só `contact_id` como PK) |
| Atendimentos | `sessions` (estado/stage) + `sessions.terminal_reason` |
| Agendamentos | `sessions.state.appointment` (slot/id/modelo) + GHL Calendar |
| Logs | ❌ Não há tabela. structlog → stdout |
| Tokens | ❌ **NÃO ARMAZENADO em lugar nenhum** |
| Custos | ❌ **NÃO CALCULADO em lugar nenhum** |

---

## 5. CONVERSAS E ATENDIMENTOS

- **Como uma conversa é criada:** `POST /sessions/{contactId}/greet` — idempotente via `state.greeted` + custom field `saudacao_prevendas=SIM`. Seta `greeted=true`, `stage="abertura"`, opcional `veiculo_origem`, envia saudação. Não existe registro de "conversa" próprio — usa `contactId` como chave; a conversa real é a do GHL.
- **Como uma conversa é encerrada:** ao setar `sessions.terminal_reason`. Turnos seguintes batem em "terminal gate" e retornam cedo. Há também `POST /sessions/{contactId}/abandon` (fecha sessão, sem nota/workflow).

**Campos utilizados:**
- `conversationId`: **não persistido** no estado; resolvido por turno via `/conversations/search`. (NÃO IDENTIFICADO como campo guardado.)
- `contactId`: **SIM** — PK de `sessions`, chave de tudo.
- `locationId`: SIM — config `GHL_LOCATION_ID` (`L2b97kq1i5tk1Fr51AsI`), usado em chamadas; não por-sessão.
- `opportunityId`: **NÃO IDENTIFICADO** — agente não cria/atualiza Opportunities.

**Critérios por situação:**

| Situação | Como identificar |
|---|---|
| Atendimento iniciado | `state.greeted=true` (greet) ou primeiro `/webhook/inbound` que cria sessão. Métrica `zoi_turns_total` |
| Atendimento concluído | `terminal_reason ∈ {qualificado_agendado, qualificado_sem_agenda}` (funil 10 campos completo) |
| Handoff | `terminal_reason ∈ {handoff_solicitado, handoff_erro}` → remove tag + nota + workflow. Métrica `zoi_handoff_total{reason}` |
| Agendamento | `state.appointment` setado + `terminal_reason=qualificado_agendado` + POST GHL appointment. Métrica `zoi_qualificados_total{com_agenda="true"}` |
| Follow-up | **NÃO IDENTIFICADO** — sem máquina de follow-up no código (campos `escalacao_pendente_motivo` existem, mas follow-up automatizado não) |
| Encerramento | qualquer `terminal_reason` setado (terminal gate) |
| Abandono | `POST /sessions/{cid}/abandon` → `terminal_reason="abandonado"` (CRM-side, **sem** nota/workflow/métrica) |

---

## 6. INTEGRAÇÃO COM GHL / CRM

- **Integração:** GoHighLevel / LeadConnector REST.
- **Auth:** **PIT token (Private Integration Token)** — `Authorization: Bearer <pit>`, header `Version: 2021-07-28`, base `https://services.leadconnectorhq.com`. **Não** é OAuth nem API Key clássica.
- **Cliente:** `httpx.AsyncClient`, timeout 30s, retry tenacity (3x, exp 1/2/4s) em 408/429/5xx + caso especial `401 "Command timed out"`.

**Endpoints GHL utilizados:**

| Categoria | Método | Path | Uso |
|---|---|---|---|
| Contatos | GET | `/contacts/{id}` | buscar contato + customFields + tags |
| Contatos | PUT | `/contacts/{id}` | atualizar custom fields |
| Contatos | POST | `/contacts/{id}/notes` | nota consolidada de handoff |
| Tags | POST | `/contacts/{id}/tags` | adicionar tag |
| Tags | DELETE | `/contacts/{id}/tags` | remover tag (opt-out/terminal) |
| Conversas | GET | `/conversations/search` | achar conversa por contactId |
| Conversas | GET | `/conversations/{id}/messages?limit=100` | histórico por turno |
| Conversas | POST | `/conversations/messages` | enviar mensagem (`type:SMS`) |
| Workflows | POST | `/contacts/{id}/workflow/{wfId}` | disparar workflow handoff |
| Custom Values | GET | `/locations/{locId}/customValues/{cvId}` | estoque (JSON) + FAQ (YAML) |
| Calendários | GET | `/calendars/{calId}/free-slots` | propor/achar slots |
| Calendários | POST | `/calendars/events/appointments` | agendar visita (`appointmentStatus:confirmed`, 60min) |

**Categorias presentes:**

| Categoria | Chamada? |
|---|---|
| Contatos | ✅ |
| Conversas/Mensagens | ✅ |
| Custom Fields | ✅ (via PUT contact) |
| Custom Values | ✅ (estoque + FAQ) |
| Tags | ✅ |
| Workflows | ✅ |
| Calendários/Agendamentos | ✅ |
| **Oportunidades** | ❌ NÃO IDENTIFICADO |
| **Pipelines** | ❌ NÃO IDENTIFICADO |

**Dados enviados/recebidos (resumo):** Enviado — custom fields (`{id,value}`), tags, body de nota, mensagem (`{type,contactId,message,attachments}`), payload de appointment (`calendarId,locationId,contactId,startTime,endTime,title,appointmentStatus`). Recebido — objeto contato (tags/customFields), lista de conversas/mensagens, free-slots por dia, custom value `value` (JSON/YAML de estoque/FAQ).

---

## 7. TOKENS E CUSTOS OPENAI

- **Os tokens são armazenados?** **NÃO.**
- **Onde / Tabela / Campos:** NÃO IDENTIFICADO (inexistente).
- `input_tokens`: ❌ não capturado · `output_tokens`: ❌ · `total_tokens`: ❌
- O wrapper `llm.py` (`parse_structured`, `chat_text`) descarta `response.usage` — só mede latência (`zoi_llm_latency_seconds`). Os agentes Agno rodam com `telemetry=False` e não logam usage.
- **Modelo utilizado:** `gpt-4o` (updater + Patricia + EstoqueExpert), `gpt-4o-mini` (extractor legacy), `whisper-1`.
- **Preço configurado:** ❌ NÃO IDENTIFICADO — nenhuma tabela de preço/USD por token no código ou env.
- **Existe cálculo de custo?** **NÃO.**
- **Fórmula encontrada:** NENHUMA. (Esperado seria `input_tokens×preço_in + output_tokens×preço_out` — ausente.)

> ⚠️ Bloqueio total para métricas financeiras: nem contagem de tokens, nem preço, nem custo existem. Reconstrução só via export de billing da OpenAI (fora deste sistema) ou instrumentação nova.

---

## 8. LOGS E TELEMETRIA

- **Sistema de logs:** structlog (JSON se `LOG_FORMAT=json`, senão console), saída **stdout**. Sem arquivo, sem tabela de log, sem coletor externo configurado.
- **Arquivos:** ❌ (stdout apenas) · **Tabelas:** ❌ · **Ferramentas externas:** Prometheus `/metrics` (+ `grafana/dashboard.json` pronto). Nenhum APM/Sentry/Datadog.
- **Telemetria Prometheus:** `zoi_turns_total{stage,intent}`, `zoi_handoff_total{reason}`, `zoi_qualificados_total{com_agenda}`, `zoi_llm_latency_seconds{component}`, `zoi_ghl_request_latency_seconds{operation}`. ⚠️ Counters/histogramas são **in-memory** — zeram em restart, sem persistência/série temporal própria (depende de scrape Prometheus externo).

| Item | Existe? | Localização | Estrutura | Campos |
|---|---|---|---|---|
| Histórico de mensagens | Parcial | **GHL** (não local) | API GHL | mensagens da conversa, por turno |
| Histórico de execução | Parcial | stdout (structlog) | logs efêmeros + Prom counters | eventos: `team_turn_start`, `calendar_booked`, `inventory_decision`, etc. |
| Histórico de erros | Parcial | stdout (structlog) | logs | exc_info/stack; `handoff_erro` em `terminal_reason` |
| Histórico de chamadas OpenAI | ❌ NÃO | — | — | só latência agregada; sem tokens, sem prompt/response, sem custo |

---

## 9. MÉTRICAS POSSÍVEIS (sem alterar código)

### Operacionais

| Métrica | Disponível | Fonte | Confiabilidade |
|---|---|---|---|
| Conversas iniciadas | SIM | `zoi_turns_total` / linhas `sessions` (`created_at`) | Alta |
| Conversas concluídas | SIM | `sessions.terminal_reason` / `zoi_qualificados_total` | Alta |
| Handoffs | SIM | `zoi_handoff_total{reason}` / `terminal_reason` | Alta |
| Follow-ups | NÃO | — (sem feature) | — |
| Agendamentos | SIM | `state.appointment` + `qualificados_total{com_agenda=true}` + GHL appointments | Alta |
| Abandono | Parcial | `terminal_reason="abandonado"` (sem métrica Prom) | Média |

### Financeiras

| Métrica | Disponível | Fonte | Confiabilidade |
|---|---|---|---|
| Tokens | NÃO | — | — |
| Custos OpenAI | NÃO | — (apenas billing OpenAI externo) | — |
| Custo por conversa | NÃO | — | — |
| Custo por mensagem | NÃO | — | — |

### Comerciais

| Métrica | Disponível | Fonte | Confiabilidade |
|---|---|---|---|
| Oportunidades criadas | NÃO | agente não cria Opportunities | — |
| Oportunidades atualizadas | NÃO | idem | — |
| Vendas atribuíveis à IA | Parcial | nota/tag/workflow no GHL → cruzar com pipeline GHL manualmente | Baixa |
| Leads qualificados | SIM | `zoi_qualificados_total{com_agenda}` / `terminal_reason` | Alta |

---

## 10. GAPS PARA O DASHBOARD

| GAP | Impacto | Criticidade |
|---|---|---|
| Tokens não armazenados | Nenhuma métrica financeira por conversa/turno | **Alta** |
| Custos não calculados (sem preço, sem fórmula) | Impossível custo/conversa, custo/mensagem, ROI | **Alta** |
| `conversationId` não persistido | Dificulta join com conversas GHL; reconsulta por turno | Média |
| Sem tabela de mensagens/turnos | Sem histórico próprio; depende 100% da API GHL (rate/latência) | Média |
| Métricas Prometheus in-memory | Perda em restart; sem TSDB persistente garantida | Média |
| Logs só em stdout (sem coleta) | Sem auditoria/replay; histórico de execução efêmero | Média |
| Sem Opportunities/Pipeline | Sem atribuição comercial (venda ↔ IA) | Média |
| Abandono sem métrica/nota | Funil de abandono incompleto | Baixa |
| Sem `messageId`/dedup | Risco de contagem dupla em métricas de mensagem | Baixa |
| Follow-up inexistente | Métrica de follow-up impossível | Baixa |

---

## 11. RECOMENDAÇÕES DE INSTRUMENTAÇÃO

Eventos a emitir (idealmente para tabela append-only `agent_events` + opcional fila):

| Evento | Motivo | Dados necessários | Complexidade |
|---|---|---|---|
| `AGENT_STARTED` (greet) | marcar início real | contactId, conversationId, ts, veiculo_origem | Baixa |
| `MESSAGE_RECEIVED` | volume/latência inbound, dedup | contactId, conversationId, messageId, ts, tipo(audio/texto/img) | Baixa |
| `MESSAGE_SENT` | volume outbound, nº bubbles | contactId, conversationId, ts, bubbles, anexos | Baixa |
| `LLM_CALL` | **base de tokens/custo** | component, model, input_tokens, output_tokens, total, latency, custo_usd | **Média** (capturar `response.usage` + tabela de preços) |
| `HANDOFF_CREATED` | já em métrica; persistir | contactId, reason, ts, workflowId | Baixa |
| `APPOINTMENT_CREATED` | agendamentos confiáveis | contactId, slot_iso, appointmentId, modelo | Baixa |
| `FOLLOWUP_STARTED` / `FOLLOWUP_FINISHED` | feature inexistente | contactId, motivo, ts | Alta (precisa feature) |
| `CONVERSATION_CLOSED` | fechamento + desfecho | contactId, terminal_reason, ts, qualificado?, com_agenda? | Baixa |
| `OPPORTUNITY_LINKED` | atribuição comercial | contactId, opportunityId, pipelineStage | Média (precisa integrar Opportunities GHL) |

**Prioridade #1:** capturar `response.usage` em `llm.py` + adicionar tabela de preços por modelo → habilita todas as métricas financeiras de uma vez.

---

## 12. SCORE DE OBSERVABILIDADE (0–10)

| Dimensão | Nota | Justificativa |
|---|---|---|
| Banco de Dados | 3 | 1 tabela só (estado). Sem mensagens/eventos/tokens persistidos |
| Logs | 5 | structlog JSON bem estruturado, mas só stdout, sem coleta/retenção |
| Custos | 0 | Zero: sem tokens, sem preço, sem fórmula |
| CRM | 8 | Integração GHL ampla (contatos, conversas, tags, workflows, calendário, custom values) |
| Conversas | 6 | Source of truth no GHL, recuperável; mas sem conversationId/mensagens locais |
| Telemetria | 5 | Prometheus rico em eventos de negócio, porém in-memory e sem tokens |
| Monitoramento | 5 | `/health` + `/metrics` + dashboard Grafana pronto; sem alertas/APM |

**Nota Final: 4,6 / 10**

**Justificativa:** Observabilidade **operacional/comercial** decente (eventos de funil, handoff, agendamento via Prometheus + estado Postgres). Observabilidade **financeira nula** (tokens/custo ausentes) e persistência de histórico fraca (sem tabelas de mensagens/eventos; logs efêmeros). CRM é o ponto forte.

---

## 13. RESUMO EXECUTIVO FINAL

O agente "Patricia" atende leads de WhatsApp via GHL num pipeline multi-LLM por turno: um Updater (gpt-4o, saída estruturada) atualiza o `session_state`, e um Team Agno (Patricia + EstoqueExpert, gpt-4o) gera as respostas em bubbles. Tudo é gated por tag `agente-ia`; preempção por `contactId` em memória; envio sob `asyncio.shield`. Persiste **apenas** uma tabela (`sessions`, JSONB) no Postgres — conversas e mensagens vivem no GHL e são reconsultadas a cada turno.

**Já pode ser medido (sem código):** conversas iniciadas/concluídas, handoffs por motivo, leads qualificados, agendamentos, distribuição de intent/stage, latências LLM e GHL — via Prometheus `/metrics` e a tabela `sessions`. Confiabilidade alta nesses pontos.

**Não pode ser medido:** tokens, custo OpenAI, custo por conversa/mensagem (inexistentes — sem captura de `usage`, sem preço, sem fórmula), oportunidades/vendas atribuíveis (sem integração Opportunities/Pipeline), follow-ups (feature ausente), histórico próprio de mensagens (só GHL).

**Principais riscos:** (1) custo cego — impossível ROI sem instrumentar tokens; (2) métricas Prometheus in-memory zeram em restart, sem TSDB garantida; (3) dependência total da API GHL para histórico (rate/latência); (4) logs efêmeros (stdout), sem auditoria.

**Principais oportunidades:** capturar `response.usage` em `llm.py` + tabela de preços destrava todo o eixo financeiro com esforço médio; emitir eventos append-only (`agent_events`) consolida operacional + comercial num só lugar; integrar Opportunities GHL habilita atribuição de venda.

**Esforço estimado p/ integração ao Dashboard:** **Médio** — base operacional já existe; principal trabalho é instrumentação financeira (tokens/custo) e uma tabela de eventos. ~1–2 sprints.

### Classificação Final: 🟡 **Requer Ajustes**

Pronto no eixo operacional/comercial; bloqueado no financeiro (tokens/custo) e na persistência de histórico — ajustes pontuais, sem reestruturação.
