# AMC Veículos — Agente WhatsApp (ZOI fork)

Plano consolidado. Cliente: AMC Veículos (seminovos, Joinville/SC). Base: ZOI Agno + GHL.

---

## 1. Decisões de arquitetura

| Tópico | Decisão |
|---|---|
| Pipeline | 2-LLM: updater (structured output) + responder (texto multi-bubble) |
| Auth GHL | PIT single-tenant |
| Estoque | GHL Custom Value JSON (`cqH4Ba3hcS0Xuzvy4izA`), 36 veículos, cache 5min |
| FAQ | GHL Custom Value YAML (`iD172rYRHqf0aLdtGz0H`), ~5KB, cache 5min |
| Calendário | `kmDqEOaOt4479ipBYZtn`, presencial Rua Iririu 2464 Joinville, 60min |
| Workflow handoff | `b759fd01-2867-45b9-a8c8-74490793e261` (dispara em qualificado_agendado, qualificado_sem_agenda, handoff_solicitado, handoff_erro) |
| Tag gate | `agente-ia` no contato. Sem tag → webhook ignora. Remover tag = opt-out/handoff. |
| Atendimento | 24/7 |
| Timezone | America/Sao_Paulo |
| Sentido WA | `type:"SMS"` no `POST /conversations/messages` |

---

## 2. Persona — Patricia

**Quem é**: atendente virtual da AMC Veículos. Não declara IA (evasivo); se perguntado de novo, admite: *"Sou um atendente digital da AMC, qualquer detalhe específico passo pra um consultor."*

**Tom**: pré-atendente experiente, brasileiro popular, confiante, direto. Soa como quem já fez mil atendimentos — nunca formulário.

**Frases-âncora ✅**: "Opa", "Manda ver", "Deixa eu te ajudar", "Já te passo", "Posso te adiantar", "Bora marcar?", "Fechado", "Pode deixar", "Me conta", "Tô contigo", "Show", "Beleza", "Tranquilo".

**Banido ❌**:
- "(sim ou não)" no fim de pergunta
- "Qual é o seu caso:"
- "Prezado", "informo que", "gostaria de", "Atenciosamente", "venho por meio desta", "poderia me informar"
- Checklist enumerado "1) X 2) Y" em conversa
- "Vou encaminhar / passo pro consultor" sem chamar tool de handoff real
- Negociar preço, aprovar financiamento, avaliar troca em R$, prometer condição comercial, comentar documentos, reservar veículo

**Preferência lexical**: "veículo".

---

## 3. Saudação (enviada pelo endpoint dedicado, não pelo agent)

### Sem veículo de interesse
```
Olá! 👋 Bem-vindo à AMC Veículos.
Como posso te ajudar hoje? Está procurando algum carro específico?
```

### Com veículo de interesse (custom field `Veículo de Interesse` preenchido)
```
Olá! 👋 Bem-vindo à AMC Veículos.
Vi que você demonstrou interesse no {{veiculo_interesse}} 🚗
Posso te passar mais informações sobre ele?
```

---

## 4. Qualificação (10 campos, ordem PRIORITY)

1. `nome`
2. `veiculo_interesse` (texto livre)
3. `vehicle_focus_definido` (bool — convergiu num veículo do estoque)
4. `intencao` (`compra_direta` | `troca`)
5. `possui_troca` (bool)
6. `troca_completa` (`{modelo, ano, km, quitado}` se `possui_troca=true`)
7. `motivo_compra_ou_troca` (texto livre)
8. `forma_pagamento` (`a_vista` | `financiado` | `consorcio`)
9. `cidade`
10. `interesse_agendamento` (bool)

`pode_handoff=true` quando os 10 OK **OU** agendamento confirmado.

**Regra mestra de cada turno**: resposta a dúvida com dado da tool **+** próxima pergunta pendente do funil. Toda resposta avança 1 campo. Nunca trava, nunca repete dado já no state, nunca handoff implícito.

---

## 5. Tools do responder

### `search_inventory(query: str)`
1. Mini LLM (`gpt-4o-mini`) extrai filtros estruturados.
2. Python aplica filtros estritos (unidecode + lower + substring). Sem fuzzy, sem score, sem perfis.
3. `len(exatos) >= limit` → retorna.
4. Caso restrito → 2ª chamada mini LLM seleciona "parecidos" com justificativa contextual (sem ordem hardcoded de relaxamento).
5. Retorna `{exatos:[...], parecidos:[{vehicle, motivo}], total}`.

**Schema `InventoryFilters`**: `marca, modelo, carroceria, cambio, combustivel, cor, ano_min, ano_max, preco_min, preco_max, km_max, portas, opcionais, keywords, sort_by, limit=10`. Listas multi-valor onde fizer sentido.

**Output por veículo**: `titulo, marca, modelo, ano, preco, quilometragem, cambio, cor, opcionais[top5], imagens[0], external_id`.

### `get_vehicle_details(external_id: str)`
Só sob pedido explícito do lead. Retorna ficha completa crua (21 campos) — responder filtra o que mencionar.

### Envio de fotos
- Só sob pedido explícito.
- Se veículo tem **apenas 1 imagem** → responder informa *"esse não tem fotos cadastradas no momento"* (não envia).
- ≥2 imagens → 1 send por imagem em **paralelo** via `POST /conversations/messages` (`type:"SMS"`, `attachments:[url]`, `contactId`, `conversationId`). Sem ordenação.
- 1 mensagem de texto **depois** das fotos, contendo contexto + próxima pergunta de qualificação.
- `asyncio.gather(..., return_exceptions=True)` sob `asyncio.shield`.

### `get_faq()`
Retorna YAML completo (5KB). Cache 5min. Disparado quando updater retorna `intent_secundario="duvida_operacional"`. Orquestrador injeta YAML no prompt do responder antes de gerar.

### `buscar_veiculo_interesse_origem(contactId)`
1. `GET /contacts/{id}` → lê `customFields[Be4tea6NmcudLaKTYpdR].value`.
2. Se não vazio → chama `search_inventory(query=valor)` internamente.
3. Retorna `{texto_origem, matches:[...]}` ou `null`.
4. Cache por sessão.

### `registrar_nota_atendimento(contactId, body)`
`POST /contacts/{id}/notes`. Body = template da §10.

### `encaminhar_para_vendedor(contactId, motivo)`
1. Remove tag `agente-ia` do contato.
2. Dispara workflow `b759fd01-…`.
3. Cria nota com motivo.
4. Marca sessão `terminal_reason` + para de responder.

---

## 6. Schemas

### `StateUpdate` (output do updater)
```python
class TrocaInfo(BaseModel):
    modelo: str | None
    ano: int | None
    km: int | None
    quitado: bool | None

class Collected(BaseModel):
    nome: str | None
    veiculo_interesse: str | None
    vehicle_focus_definido: bool = False
    intencao: Literal["compra_direta","troca"] | None
    possui_troca: bool | None
    troca_completa: TrocaInfo | None
    motivo_compra_ou_troca: str | None
    forma_pagamento: Literal["a_vista","financiado","consorcio"] | None
    cidade: str | None
    interesse_agendamento: bool | None

class StateUpdate(BaseModel):
    stage: Literal["abertura","descoberta","apresentacao","fechamento","fechado"]
    collected: Collected
    missing: list[str]
    next_action: str
    sentiment: Literal["neutro","positivo","negativo","irritado"]
    intent: Literal["qualificar","duvida","opt_out","pedido_humano","agendamento","apresentar"]
    intent_secundario: Literal["duvida_operacional","ver_outros_carros","pedido_foto",None] = None
    should_handoff: bool
    handoff_reason: str | None
    pode_handoff: bool
    terminal_reason: str | None
    preferencia_horario: dict | None
```

### `session_state` JSONB
```json
{
  "stage": "descoberta",
  "greeted": true,
  "veiculo_origem": {"texto":"Renault Duster","matches_external_ids":["1632332"]},
  "collected": { ... },
  "vehicles_shown": ["1632332"],
  "humano_solicitado_count": 0,
  "ai_identity_asked_count": 0,
  "last_sentiment": "neutro",
  "last_intent": "qualificar",
  "terminal_reason": null,
  "appointment": null,
  "created_at": "...",
  "updated_at": "..."
}
```

Áudios NÃO persistem em session_state. Transcrição é efêmera, usada apenas como body do turno atual. Histórico original fica no GHL conversation.

---

## 7. Stages

| Stage | Entrada | Comportamento |
|---|---|---|
| `abertura` | sessão pós-greet | apresenta veículo de origem se houver, captura nome |
| `descoberta` | `nome` OK | qualifica ordem PRIORITY |
| `apresentacao` | lead pediu ver outros OU `vehicle_focus_definido=false` após N | `search_inventory` + apresenta |
| `fechamento` | 10 campos OK OU (`interesse_agendamento=true` AND `vehicle_focus_definido=true`) | propõe slots / cria appointment |
| `fechado` | terminal action executada | não responde mais |

Regressão livre. FAQ é interrupção transparente que mantém stage atual. `terminal_reason` em linguagem natural separado do stage.

---

## 8. Endpoints

### `POST /sessions/{contactId}/greet?secret={WEBHOOK_SECRET}`
- Síncrono (200 só após send OK).
- Idempotente: checa `state.greeted` E `saudao_prvendas=SIM`; se qualquer um indica enviada, 200 sem reenviar.
- Lê custom field `Veículo de Interesse`.
- Envia template apropriado via GHL.
- Cria sessão com `state.greeted=true` + `state.veiculo_origem`.
- Marca custom field `saudao_prvendas=SIM`.
- Auth: `?secret=` HMAC compare.

### `POST /webhook/inbound?secret={WEBHOOK_SECRET}`
- Gate: contato deve ter tag `agente-ia`; sem tag → 200 sem ação.
- Filtro: `direction=inbound` E `messageType in {SMS, WhatsApp}`.
- Áudio: transcreve via Whisper (`whisper-1`), todos áudios da mensagem, concatena.
- Imagem/doc: ignora.
- Vazio/emoji: processa.
- Sem dedup por `messageId` (preempção + debounce 12s GHL).
- Histórico: `GET /conversations/{id}/messages?limit=100`.
- Preempção por `contactId` (`asyncio.Task` table).
- Send phase sob `asyncio.shield`.

### `POST /sessions/{contactId}/abandon?secret={WEBHOOK_SECRET}`
- Disparado por workflow GHL após inatividade.
- Só fecha sessão no DB. **Sem nota, sem workflow** (`abandonado` é tratado diretamente no CRM).

### `GET /metrics`
- Prometheus exposition format.

---

## 9. Mensagens — mecânica

- **Multi-bubble**: separador `|||`, máx 3, sleep 0.6–1.2s entre sends, sequencial.
- **Pergunta de funil**: SEMPRE no último bubble (instrução explícita no prompt).
- **Falha no bubble N**: tenacity 3x. Persistente → pula com nota de erro no log, segue p/ bubble seguinte.
- **Fotos + texto**: fotos em paralelo → wait 1s → bubbles de texto sequencial.
- **Shield**: `asyncio.shield` envolve todo bloco de envio (imagens + bubbles).

---

## 10. Nota consolidada (terminal action)

Texto simples estruturado:

```
[ZOI] Qualificação — {terminal_reason}
Data: {timestamp_sp}

Lead: {nome}
Cidade: {cidade}

Veículo de interesse: {veiculo_interesse}
Foco definido: {modelo_focado ou "-"}
Intenção: {compra_direta | troca}
Possui troca: {sim/não}
Troca: {modelo} {ano} {km}km {quitado?} | "-"
Motivo: {motivo_compra_ou_troca}
Pagamento: {a_vista | financiado | consorcio}

Agendamento: {data hora | "sem agendamento marcado"}
Handoff: {motivo se aplicável}

Observações: {free text se houver}
```

Estados que geram nota + workflow:
- `qualificado_agendado` — inclui dados do appointment
- `qualificado_sem_agenda` — destaca "sem agendamento marcado"
- `handoff_solicitado` — motivo explícito do lead
- `handoff_erro` — detalhe técnico

`abandonado`: sem nota, sem workflow.

---

## 11. Agendamento

### `propose_slots(preferencia: {dia, periodo})`
- `GET /calendars/{id}/free-slots` — janela hoje + amanhã + depois (3 dias).
- Filtra por preferência declarada.
- Retorna até 3 slots.

### `book_appointment(slot, contactId)`
- `POST /calendars/events/appointments`:
  - `calendarId`, `locationId`, `contactId` ✓
  - `startTime`/`endTime` ISO8601 com offset SP
  - `title`: `"Visita AMC — {nome} — {modelo_interesse}"`
  - `appointmentStatus`: `confirmed`
  - `assignedUserId`: omitido
  - `address`: omitido
  - `notes`: resumo da qualificação automático (template §10)
- Lead aceita slot → cria direto, sem confirmação extra.
- Conflito (slot ocupado entre listar e bookar) → não previsto; se ocorrer, cai no retry/handoff_erro.

Gate duplo: `interesse_agendamento=true AND vehicle_focus_definido=true`.

---

## 12. Política de handoff / opt-out

- Pedido de humano calmo: **insiste 1x** ("posso te adiantar?"); na **2ª menção**, faz handoff.
- Irritação / opt-out explícito ("para", "chega", "não quero"): **handoff imediato**.
- Updater rastreia `humano_solicitado_count` no state.
- Handoff = `encaminhar_para_vendedor(contactId, motivo)`: remove tag `agente-ia` + workflow `b759fd01-…` + nota.

---

## 13. Falhas (resumo)

| Falha | Ação |
|---|---|
| Updater LLM 3x | nota + workflow + `terminal_reason=handoff_erro` + remove tag |
| Responder LLM 3x | idem |
| GHL send 3x | log + tenta workflow; se workflow falhar, só log |
| Whisper 3x | pede pra digitar; persistente 3 vezes seguidas → handoff_erro |
| Mini LLM (search) | tool retorna `{error:true}`; responder narra erro técnico; sem handoff |
| Greet endpoint | non-2xx; workflow GHL retenta |

Retry: tenacity 3 attempts, exponencial 1/2/4s.

---

## 14. Variáveis de ambiente

Ver `.env.example`. Resumo dos IDs reais já confirmados:

- `GHL_PIT_TOKEN=pit-39b6e16a-4074-4639-85d7-2c0ad2987b42`
- `GHL_LOCATION_ID=L2b97kq1i5tk1Fr51AsI`
- `GHL_STOCK_CUSTOM_VALUE_ID=cqH4Ba3hcS0Xuzvy4izA`
- `GHL_FAQ_CUSTOM_VALUE_ID=iD172rYRHqf0aLdtGz0H`
- `GHL_FIELD_VEICULO_INTERESSE=Be4tea6NmcudLaKTYpdR`
- `GHL_FIELD_SAUDACAO_PREVENDAS=SnRsIPXGatlCLewudEUe`
- `GHL_CALENDAR_ID=kmDqEOaOt4479ipBYZtn`
- `GHL_HANDOFF_WORKFLOW_ID=b759fd01-2867-45b9-a8c8-74490793e261`
- `WEBHOOK_SECRET=fd802d8aeb97dbc3c36b09ef3126f63f46276a923d93ac40609c5fcbdee5795b`

---

## 15. GHL — Workflows (você configura no UI)

### Workflow 1: "ZOI — Saudação"
- **Trigger**: contato criado com tag `agente-ia` E custom field `SAUDAÇÃO PRÉ-VENDAS != "SIM"`.
- **Ação única**: HTTP Request:
  - Method: `POST`
  - URL: `https://{NGROK}/sessions/{{contact.id}}/greet?secret={WEBHOOK_SECRET}`
  - Headers: `Content-Type: application/json`
  - Body: `{}`
  - Wait for response: yes (síncrono)

### Workflow 2: "ZOI — Webhook Inbound"
- **Trigger**: mensagem recebida (inbound) num contato com tag `agente-ia`.
- **Ação única**: HTTP Request:
  - Method: `POST`
  - URL: `https://{NGROK}/webhook/inbound?secret={WEBHOOK_SECRET}`
  - Body: payload da mensagem (JSON com `contactId`, `body`, `type`, `attachments`, `dateAdded`...).

### Workflow 3: "ZOI — Abandono por inatividade"
- **Trigger**: contato com tag `agente-ia` sem nova mensagem inbound por X horas (definir — sugestão 24h).
- **Ação**: HTTP POST `/sessions/{{contact.id}}/abandon?secret={WEBHOOK_SECRET}`.

### Workflow 4: "ZOI — Handoff" (`b759fd01-…`)
- Já existente. Disparado pelo agent via API. Você define no UI o que acontece (notifica consultor, atribui owner, manda email, etc).

---

## 16. Cenários de teste (contato real)

Script de testes manuais — cada cenário usa contato GHL real (criar contato dedicado por cenário).

### C1 — Saudação sem veículo de origem
- Preparação: contato sem custom field "Veículo de Interesse", com tag `agente-ia`.
- Esperado: saudação genérica enviada via greet endpoint.

### C2 — Saudação com veículo de origem
- Preparação: contato com `Veículo de Interesse=Renault Duster`.
- Esperado: saudação com menção ao Duster.

### C3 — Idempotência do greet
- Disparar greet 2x. Esperado: 2ª chamada retorna 200 sem reenvio.

### C4 — Apresentação imediata pós-saudação
- Lead responde "quero ver". Esperado: agent chama `buscar_veiculo_interesse_origem` + apresenta matches, sem qualificar ainda.

### C5 — Qualificação ordem PRIORITY
- Lead responde "ok". Esperado: agent pergunta nome → vehicle_focus → intenção → ... na ordem.

### C6 — Busca por filtro complexo
- Lead: "tem SUV automático até 80mil?". Esperado: `search_inventory` retorna matches estritos; se zero, retorna parecidos com justificativa.

### C7 — Pedido de foto (≥2 imagens)
- Lead: "manda foto do Logan". Esperado: N sends paralelos com imagens + 1 send de texto com próxima pergunta.

### C8 — Pedido de foto (1 imagem)
- Esperado: agent diz "esse não tem fotos cadastradas no momento" + segue qualificação.

### C9 — FAQ
- Lead: "vocês financiam?". Esperado: updater marca `intent_secundario=duvida_operacional` → `get_faq` → responder usa dado real + próxima pergunta.

### C10 — Áudio
- Lead manda áudio. Esperado: Whisper transcreve, agent responde como se fosse texto. Áudio não aparece em session_state.

### C11 — Múltiplos áudios na mesma mensagem
- Esperado: concatena transcrições, processa como 1 mensagem.

### C12 — Imagem/doc enviado pelo lead
- Esperado: ignora, responde texto normal baseado no contexto.

### C13 — Mudança de interesse
- Lead em `fechamento` diz "quero ver outro carro". Esperado: stage regride p/ `apresentacao`.

### C14 — Pedido de humano calmo (1ª vez)
- Lead: "posso falar com vendedor?". Esperado: agent insiste ("posso te adiantar?"); state `humano_solicitado_count=1`.

### C15 — Pedido de humano (2ª vez)
- Lead insiste. Esperado: handoff (remove tag, nota com motivo, workflow disparado).

### C16 — Irritação explícita
- Lead: "para de me mandar mensagem". Esperado: handoff imediato.

### C17 — Pergunta sobre identidade (1ª)
- Lead: "você é robô?". Esperado: evasivo. `ai_identity_asked_count=1`.

### C18 — Pergunta sobre identidade (2ª)
- Esperado: admite ("Sou um atendente digital da AMC...").

### C19 — Agendamento gate
- Lead diz "quero agendar" mas `vehicle_focus_definido=false`. Esperado: agent puxa foco antes de propor slots.

### C20 — Agendamento completo
- Foco + interesse_agendamento. Esperado: `propose_slots` filtrado por preferência → lead aceita → `book_appointment` criado → nota + workflow.

### C21 — Qualificação completa sem agenda
- 10 campos OK, lead recusa agendar. Esperado: nota com "sem agendamento marcado" + workflow.

### C22 — Preempção
- Lead manda 3 mensagens em 2s. Esperado: task anterior cancelada, só último turno responde. Sem partial sends.

### C23 — Falha do updater (simular OpenAI down)
- Esperado: nota handoff_erro + workflow + remove tag.

### C24 — Tag ausente
- Webhook chega de contato sem tag `agente-ia`. Esperado: 200 sem ação, sem log de erro.

---

## 17. Sprint plan

Ordem fase a fase. Cada sprint termina com smoke test do cenário relevante.

### Sprint 0 — Bootstrap
- Fork ZOI base p/ workspace `zaf-amcveiculos-plan/`.
- `pyproject.toml`, `.env`, `pip install -e ".[dev]"`.
- Postgres local + Agno auto-create schema.
- Logger `structlog` JSON + Prometheus client.

### Sprint 1 — GHL client + smoke
- `ghl/client.py` (httpx + PIT + tenacity).
- Wrappers: `contacts.get`, `conversations.search`, `conversations.get_messages`, `conversations.send_message`, `custom_values.get`, `contacts.add_note`, `contacts.update_field`, `contacts.remove_tag`, `workflows.add_to_workflow`.
- Smoke test: fetch contato `d9ILOnEyNkYhkIALa3wq`.

### Sprint 2 — Inventory tools
- `tools/inventory.py`: `load_inventory` (cache), `extract_filters` (mini LLM), `apply_filters`, `select_similar` (mini LLM), `search_inventory`, `get_vehicle_details`.
- Test: cenários C6.

### Sprint 3 — FAQ tool
- `tools/faq.py`: `get_faq` (cache).
- Test: C9.

### Sprint 4 — Whisper
- `audio/whisper.py`: transcribe (multi-audio concat, 3-retry).
- Test: C10, C11.

### Sprint 5 — Updater LLM
- `agent/updater.py`: prompt completo + `StateUpdate` structured output.
- Test: cenários de extração (input fake conversation → state esperado).

### Sprint 6 — Responder LLM
- `agent/responder.py`: prompt persona Patricia + multi-bubble parser.
- Test: gera bubbles válidos, última pergunta = próximo campo.

### Sprint 7 — Orchestrator
- `orchestrator.py`: per-contact Task table, preempção, shield, pipeline updater→responder→send.
- Multi-bubble send com sleeps.
- Test: C22 (preempção).

### Sprint 8 — Greet endpoint
- `endpoints/greet.py`: lê custom field, escolhe template, envia, persiste state + marca custom field.
- Idempotência.
- Test: C1, C2, C3.

### Sprint 9 — Inbound webhook + script de mapeamento
- `scripts/inspect_webhook.py` — recebe POST, dumpa headers + body.
- Configura workflow GHL, dispara mensagem real, salva payload de referência.
- `webhooks/inbound.py`: parse payload mapeado, gate por tag, filtros, transcrição áudio, fetch histórico, dispara orquestrador.
- Test: C4, C5, C12, C24.

### Sprint 10 — Fotos
- Envio paralelo via `asyncio.gather`. Texto sequencial depois.
- Test: C7, C8.

### Sprint 11 — Handoff + opt-out
- `tools/handoff.py`: remove tag + workflow + nota.
- Updater detecta sentiment/intent.
- Test: C14, C15, C16, C17, C18.

### Sprint 12 — Calendário
- `tools/calendar.py`: `propose_slots`, `book_appointment`.
- Updater extrai `preferencia_horario`.
- Test: C19, C20.

### Sprint 13 — Terminal actions
- `tools/terminal.py`: monta nota consolidada + dispara workflow + para de responder.
- Test: C20, C21.

### Sprint 14 — Abandon endpoint
- `endpoints/abandon.py`: fecha sessão. Sem nota, sem workflow.

### Sprint 15 — Métricas + dashboard
- `/metrics` Prometheus.
- Counters: turnos, handoff_erro, qualificados_dia.
- Histograms: latência updater/responder.
- Grafana dashboard JSON.

### Sprint 16 — End-to-end + falhas
- C13, C23, restantes.
- Documentação de operação.

---

## 18. Pendências p/ codar

- `scripts/inspect_webhook.py` é prioridade no Sprint 9 — sem ele não conseguimos mapear payload real do GHL.
- Confirmar IDs de status do calendário (`appointmentStatus="confirmed"` válido) no 1º teste.
- Definir janela exata de abandono (sugestão 24h) com cliente.
- `assignedUserId` no GHL configurado p/ atribuição automática no calendário.
