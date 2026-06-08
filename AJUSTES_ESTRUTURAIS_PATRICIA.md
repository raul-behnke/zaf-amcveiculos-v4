# Ajustes Estruturais — Patricia (pré-deploy)

Base: `RELATORIO_PRODUCAO_PATRICIA.md` + 6 problemas listados pelo operador.
Alvo: alterações em **schema do updater**, **state**, **prompt do responder**, **tools** e **orquestrador** — antes do 1º deploy real.

Cada bloco abaixo segue o formato:
**Problema → Causa raiz → Ajuste estrutural (onde no código) → Critério de aceite.**

---

## 1. Recomendações inadequadas quando filtros não retornam resultado

**Causa raiz**: `search_inventory` retorna `exatos=[]` + `parecidos=[...]` mas o responder despeja qualquer coisa do bucket "parecidos" sem amarrar à intenção real do lead (caso Delia: "Mobi automático" → IA listou modelos aleatórios).

**Ajuste estrutural**:

### 1.1 Tool — `search_inventory`
- Acrescentar campo no retorno: `match_quality: "exato" | "parecido_forte" | "parecido_fraco" | "vazio"`.
- `parecido_forte` = relaxou no máx 1 dimensão (ex.: faixa de preço +10%). `parecido_fraco` = relaxou ≥2.
- Quando `vazio` ou `parecido_fraco`: retornar também `filtros_aplicados` (echo dos critérios) e `dimensao_a_relaxar_sugerida`.

### 1.2 Prompt do responder — regra nova
> Se `match_quality ∈ {parecido_fraco, vazio}`: **NÃO** liste veículos. Pergunte qual critério o lead aceita relaxar (preço, ano, câmbio, modelo). Só depois da resposta, chame `search_inventory` de novo.

### 1.3 Nova tool — `vehicle_unavailable_fallback(external_id)`
Para o caso "veículo específico vendido" (Selma-Renegade): retorna top 3 do mesmo segmento (carroceria + faixa de preço ±15%) com motivo de similaridade.

**Aceite**: cenário Delia rodando — IA pergunta "topa câmbio manual ou amplio a faixa de preço?" em vez de listar opções soltas.

---

## 2. Continuidade excessiva após objeção

**Causa raiz**: updater não tem campo de objeção; responder não sabe que "nenhum me chamou atenção" é STOP-signal.

**Ajuste estrutural**:

### 2.1 Schema `StateUpdate` — campo novo
```python
class StateUpdate(BaseModel):
    ...
    objecoes: list[Literal[
        "preco_alto","ano_velho","km_alto","modelo_indesejado",
        "lista_recusada","sem_interesse_atual","outro"
    ]] = []
    objecao_ultimo_turno: bool = False
```

### 2.2 State — contador
```json
"vehicles_offered_since_last_acceptance": 0
```
Incrementa cada vez que responder mostra ficha; zera quando lead aceita um.

### 2.3 Regra no responder (hard rule no prompt)
> Se `objecao_ultimo_turno=true` **OU** `vehicles_offered_since_last_acceptance ≥ 3`: **proibido** apresentar novo veículo no mesmo turno. Obrigatório perguntar o critério que está furando (orçamento, modelo, câmbio, ano).

**Aceite**: replay Delia — após 1ª negativa, IA para de listar e pergunta critério.

---

## 3. Perguntas do lead ficam sem resposta

**Causa raiz**: updater só captura *intent* macro; não isola perguntas abertas. Responder pula direto pro próximo campo do funil (caso Neusa: "dono homem ou mulher?" ignorado).

**Ajuste estrutural**:

### 3.1 Schema `StateUpdate` — campo novo
```python
pergunta_pendente_lead: str | None  # extração literal da pergunta aberta do lead no turno
pergunta_pendente_categoria: Literal[
    "ficha_veiculo","historico_veiculo","financeiro","logistica","operacional","outro"
] | None
```

### 3.2 Regra mestra revisada (prompt responder)
Hoje: "resposta a dúvida + próxima pergunta do funil".
Nova:
> Ordem **obrigatória** por turno:
> 1. Se `pergunta_pendente_lead` ≠ null → responder PRIMEIRO (com tool se necessário; se não houver dado, dizer "vou confirmar"). Bolha 1.
> 2. Acknowledgment curto do dado anterior (se houve). Bolha 2 opcional.
> 3. Próxima pergunta do funil. Bolha 3.
>
> Nunca pular o passo 1.

### 3.3 Métrica nova
`pergunta_ignorada_count` no state. Se ≥1 → flag operacional.

**Aceite**: replay Neusa — "dono homem ou mulher?" recebe resposta ("não tenho essa info, confirmo com consultor") **antes** da pergunta de troca.

---

## 4. Priorização do funil quando lead ainda busca informação

**Causa raiz**: stages atuais (`abertura → descoberta → apresentacao → fechamento`) tratam descoberta como sequencial e assumem que apresentação termina rápido. Lead em modo "estou pesquisando" é forçado pra qualificação cedo.

**Ajuste estrutural**:

### 4.1 Novo sub-stage: `exploracao`
Entre `abertura` e `descoberta`. Entrada: lead fez ≥2 perguntas de ficha/comparação sem ainda demonstrar foco.
Comportamento: **suspende coleta de campos** (exceto `nome` se ainda faltar) até `vehicle_focus_definido=true` OU lead sinalizar "quero seguir".

### 4.2 Schema — novo campo no updater
```python
modo_lead: Literal["explorando","decidindo","pronto"] = "explorando"
```
- `explorando`: ≥2 perguntas abertas sem aceite.
- `decidindo`: lead confirmou foco em 1 veículo.
- `pronto`: aceitou foco + sinalizou intenção (visita/financiamento).

### 4.3 Regra responder
> Se `modo_lead=explorando` → no máx **1 pergunta de funil por turno** e só após responder o que o lead perguntou. Priorize entregar info do veículo.
> Se `modo_lead=decidindo` → ritmo normal de qualificação.
> Se `modo_lead=pronto` → acelera fechamento/agendamento.

**Aceite**: replay Scheila — IA para de bombardear (KM, cidade, fotos, valor) e foca em responder simulação primeiro.

---

## 5. Repetição de perguntas já respondidas

**Causa raiz**: updater extrai campos OK, mas responder não recebe um "bloqueio duro" — é só instrução de prompt suave.

**Ajuste estrutural**:

### 5.1 Pré-render do prompt (orquestrador)
Antes de chamar o responder, montar **lista negra** dinâmica:
```python
campos_proibidos = [
    f"{campo}: {valor}"
    for campo, valor in collected.dict(exclude_none=True).items()
]
```
Injetar no system prompt como:
> ⛔ DADOS JÁ COLETADOS — proibido reperguntar:
> - nome: Selma
> - veiculo_interesse: Renegade 2016
> - possui_troca: true
> - troca_completa: {modelo: Argo, ano: 2020, ...}

### 5.2 Validador pós-geração
Após responder gerar bolhas, regex/embedding-check: se alguma bolha contém padrão de pergunta sobre campo já coletado → **rejeita e regenera** (máx 2 tentativas, depois pula campo).

### 5.3 Schema — extração mais agressiva
Updater deve inferir campos compostos: se lead diz "tenho um Argo 2020 1.0 quitado", **preencher** `possui_troca=true` E `troca_completa={modelo, ano, motor, quitado}` no mesmo turno — não esperar perguntar.

**Aceite**: replay Selma — IA não repergunta "compra direta ou troca?" depois de "tenho Argo 2020".

---

## 6. Memória contextual em troca de veículo

**Causa raiz**: state guarda `vehicles_shown` (lista de external_ids) mas não guarda **por que** o lead recusou, e não correlaciona próxima busca com o histórico de objeções.

**Ajuste estrutural**:

### 6.1 State — novo bloco
```json
"vehicle_journey": [
  {
    "external_id": "1632332",
    "shown_at": "...",
    "lead_reaction": "interessou" | "recusou" | "ignorou",
    "motivo_recusa": "preco" | "ano" | "modelo" | "outro" | null,
    "trecho_lead": "..."
  }
]
```

### 6.2 Tool `search_inventory` — input enriquecido
Aceitar `exclude_external_ids` (já oferecidos e recusados) + `evitar_caracteristicas` derivado dos motivos de recusa (ex.: recusou 3 acima de 50k → próxima busca aplica `preco_max=50000` mesmo sem o lead repetir).

### 6.3 Regra responder
> Ao apresentar veículo novo após troca de foco: **sempre** referenciar o anterior em 1 frase ("já que o Mobi não rolou pelo câmbio manual, olha esse Onix automático…"). Sem isso, lead sente que IA não escutou.

### 6.4 Carry-over de qualificação
Quando lead troca de veículo de interesse, **NÃO zerar** campos já coletados (nome, intenção, troca, pagamento, cidade) — só zerar `vehicle_focus_definido` e `veiculo_interesse`.

**Aceite**: replay Delia — IA não re-pergunta nome e amarra próxima sugestão à objeção anterior ("topa subir um pouco no preço pra automático?").

---

## 7. Resumo das mudanças por arquivo

| Arquivo / módulo | Mudança |
|---|---|
| `schemas.py` (StateUpdate) | + `objecoes`, `objecao_ultimo_turno`, `pergunta_pendente_lead`, `pergunta_pendente_categoria`, `modo_lead` |
| `schemas.py` (session_state) | + `vehicles_offered_since_last_acceptance`, `pergunta_ignorada_count`, `vehicle_journey[]` |
| `tools/inventory.py` | + `match_quality`, `filtros_aplicados`, `dimensao_a_relaxar_sugerida`; `exclude_external_ids`, `evitar_caracteristicas` |
| `tools/inventory.py` | + nova `vehicle_unavailable_fallback(external_id)` |
| `agent/updater.py` | extração mais agressiva (campos compostos); detecção de pergunta aberta; classificação de objeção; cálculo de `modo_lead` |
| `agent/responder.py` | prompt: ordem obrigatória (resposta→ack→funil); regra anti-listagem após objeção; lista negra de campos coletados; carry-over em troca |
| `orchestrator.py` | pré-render da lista negra; validador pós-geração que rejeita repergunta; atualização de `vehicle_journey` |
| `tools/handoff.py` | + rota `solicitou_simulacao` (terminal_reason novo) |
| `terminal_reasons` (§10) | + `solicitou_simulacao` (gera nota + workflow) |

---

## 8. Stages revisados

```
abertura → [exploracao] → descoberta → apresentacao → fechamento → fechado
                  ↑              ↑               ↓
                  └── regressão livre quando modo_lead muda ──┘
```

`exploracao` é o novo buffer que evita o "robô disparando perguntas" do caso Delia/Scheila.

---

## 9. Ordem de implementação sugerida (antes do deploy)

1. Schema (`StateUpdate` + `session_state`) — base de tudo.
2. Updater: extração composta + `modo_lead` + `pergunta_pendente_lead` + `objecao_ultimo_turno`.
3. Orquestrador: lista negra dinâmica + validador anti-repergunta.
4. Responder: novo prompt com ordem obrigatória + regra anti-listagem.
5. Inventory: `match_quality` + `exclude_external_ids` + fallback.
6. Stage `exploracao` no orquestrador.
7. Replay dos 5 casos críticos (Delia, Scheila, Neusa, Selma, Raul) com fixtures dos payloads reais — gate para deploy.

**Critério de release**: nos 5 replays, IA cumpre §1–§6 deste doc sem ruptura. Sem isso, não deployar.
