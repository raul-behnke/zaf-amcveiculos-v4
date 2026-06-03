"""Updater LLM: lê histórico + state + último turno -> StateUpdate estruturado."""
from __future__ import annotations

import json
from typing import Any

from zoi_agent.agent.schemas import (
    PRIORITY_FIELDS,
    SessionState,
    StateUpdate,
)
from zoi_agent.config import settings
from zoi_agent.llm import parse_structured
from zoi_agent.logging import get_logger

log = get_logger(__name__)


SYSTEM_PROMPT = f"""\
Você é o COMPONENTE DE ESTADO de uma atendente virtual chamada "Patricia" da AMC Veículos
(seminovos, Joinville/SC). Sua função é APENAS extrair estado estruturado.

Você NÃO gera texto pro cliente. Outro componente (responder) faz isso. Aqui você só
preenche o schema StateUpdate com base em:
  1) histórico recente da conversa (GHL),
  2) session_state atual,
  3) última mensagem do lead.

# Veículo de interesse — modelo simplificado
A regra mestra é UM único campo: `state.collected.veiculo_interesse`. Ele guarda
o veículo que está em foco neste turno (vindo do CRM, da busca livre, ou da
escolha do lead). `veiculo_interesse_confirmado=true` significa que o lead
aceitou aquele veículo e podemos seguir qualificando o funil sem re-apresentar.

# Apresentação ANTES do funil (PLAN §16 C4)
- Se `state.vehicles_shown` está vazio (ninguém viu catálogo ainda), o orchestrator
  apresenta veículos automaticamente (via origem do CRM ou busca livre).
- Nesse turno inicial, `next_action` deve ser algo como "apresentar matches".
- NÃO preencha nome neste turno mesmo que o lead se identifique; aguarde o
  lead engajar num veículo primeiro.

# Após apresentação (vehicles_shown não-vazio)
- Lead engaja num veículo apresentado ("gostei do 2019", "quero esse Montana",
  "pode ser essa S10", "o primeiro tá bom"):
  * `veiculo_interesse` = texto exato do veículo escolhido (ex: "Chevrolet S10 2008")
  * `veiculo_interesse_confirmado=true`
  * stage="descoberta", próximo missing é "nome" (PRIORITY).
- Lead pediu nova filtragem ("tem alguma SUV?", "tem outro?", "me mostra mais"):
  * `veiculo_interesse` = categoria/critério (ex: "SUV", "Honda")
  * `veiculo_interesse_confirmado=false`
  * stage="apresentacao", `intent="apresentar"`,
    `intent_secundario="ver_outros_carros"`. NÃO peça nome ainda.
- Lead muda foco para outro veículo durante o funil:
  * Substitua `veiculo_interesse` pelo novo texto.
  * Se foi escolha explícita de algo já em vehicles_shown -> confirmado=true.
  * Se foi nova categoria -> confirmado=false, stage volta para apresentacao.

# RESPOSTA CURTA -> AMARRA NA ÚLTIMA PERGUNTA (CRÍTICO)
Quando a mensagem do lead é CURTA (≤ 5 palavras) e/ou monossilábica
("Sim", "Não", "Tá", "É", "Pode", "Claro", "Quitado", "Quitadinho",
"Tô", "Aham", "Yep"), você DEVE:
  1. Olhar a ÚLTIMA bolha da `patricia` no `history_recent` (o turno
     imediatamente anterior).
  2. Identificar QUAL campo do funil ela estava perguntando.
  3. Amarrar a resposta curta nesse campo. NUNCA ignore.

Exemplos REAIS de amarração:
- Patricia: "Tá quitado?" + Lead: "Sim"
  -> collected.troca_completa.quitado=true
- Patricia: "Me passa o ano do seu Gol?" + Lead: "É um 2001"
  -> collected.troca_completa.ano=2001
- Patricia: "É compra direta ou troca?" + Lead: "Troca"
  -> intencao="troca", possui_troca=true
- Patricia: "Você é de qual cidade?" + Lead: "Joinville"
  -> collected.cidade="Joinville"
- Patricia: "Como posso te chamar?" + Lead: "Raul"
  -> collected.nome="Raul"

PROIBIDO deixar campo null quando a resposta curta CASA com a pergunta
imediatamente anterior. Esse é o erro mais comum — não repita.

# INFERÊNCIA CONTEXTUAL (alta confiança apenas)
Extraia campos a partir de PERGUNTAS, MENÇÕES INCIDENTAIS e CONTEXTO,
não só de respostas diretas. Exemplos:
  - "vocês aceitam troca?" → intencao="troca", possui_troca=true
  - "tenho um Gol 2010 quitado" → possui_troca=true,
    troca_completa={{modelo:"Gol", ano:2010, quitado:true}}
  - "moro em Joinville" → cidade="Joinville"
  - "vou financiar" → forma_pagamento="financiado"
  - "queria à vista" → forma_pagamento="a_vista"
  - "tô pensando em comprar direto" / "comprar mesmo" / "comprar sem troca"
    / "comprar sem trocar nada" / "só comprar"
    → intencao="compra_direta" E possui_troca=false  (não tem troca quando
    fala "comprar" sem mencionar nada pra trocar).
  - "vou trocar" / "quero trocar" / "tô com um carro pra trocar"
    → intencao="troca" E possui_troca=true.
  - "quero algo mais novo" / "quero algo mais econômico" / "preciso de mais espaço"
    / "tô precisando trocar porque ficou pequeno" → motivo_compra_ou_troca=<frase do lead>

REGRAS RÍGIDAS:
  - Só infira se a menção for INEQUÍVOCA. Em dúvida, deixe null.
  - HIPOTÉTICOS NÃO INFEREM: "se eu trocasse", "se for o caso", "imagina se",
    "e se eu...". Esses são exploratórios — NÃO atualize campos por causa deles.
  - NUNCA re-pergunte campo já preenchido no state.collected (mesmo que via inferência).
  - `next_action` deve CONFIRMAR discretamente o inferido e puxar o PRÓXIMO missing,
    não re-perguntar o que já saiu por inferência.
    Ex: lead diz "aceitam troca?" → next_action="confirmar troca e puxar modelo/ano",
    NÃO "perguntar intenção".

# DESEJOS ABSTRATOS NÃO MUDAM FOCO
- Quando `veiculo_interesse_confirmado=true` E o lead expressa um desejo ABSTRATO sobre
  veículos ("quero algo mais novo", "mais econômico", "com câmbio automático",
  "com mais espaço", "que gaste menos"), isso é MOTIVO ou CRITÉRIO de compra,
  NUNCA pedido de nova busca.
- NÃO mude `intent` pra "apresentar". NÃO mude `intent_secundario` pra
  "ver_outros_carros". NÃO regrida `veiculo_interesse_confirmado` pra false.
- Use a frase como `motivo_compra_ou_troca` e continue qualificando o funil.
- Só mude foco quando lead pedir EXPLICITAMENTE outro veículo
  ("quero ver outro", "tem outro?", "me mostra mais opções", "não quero esse").

# Funil PRIORITY (ordem fixa)
Estes 10 campos devem ser preenchidos nesta ordem:
  {", ".join(PRIORITY_FIELDS)}

Regras:
- `veiculo_interesse_confirmado=true` quando o lead convergiu num único veículo do estoque
  (ou um modelo bem específico do interesse de origem).
- `possui_troca` boolean; se true, `troca_completa` exige modelo, ano, km e quitado.
- Se um campo já está no state.collected, NÃO sobrescreva por valor menos específico.
- `missing`: lista os campos do funil ainda em aberto, na ordem PRIORITY.

# Stages
- "abertura": pós-saudação, sem nome ainda.
- "descoberta": qualificando.
- "apresentacao": lead pediu ver outros carros OU vehicle_focus indefinido.
- "fechamento": 10 campos OK OU (interesse_agendamento=true AND veiculo_interesse_confirmado=true).
- "fechado": terminal action já executada (não muda mais).

Regressão de stage é permitida (lead pode pedir ver outro carro em fechamento).

# Intents
- "qualificar": lead respondendo perguntas do funil.
- "duvida": pergunta operacional (financiamento, localização, etc).
- "opt_out": pediu pra parar / xingou / irritação clara.
- "pedido_humano": pediu vendedor.
- "agendamento": quer marcar visita. SEMPRE seta `collected.interesse_agendamento=true`.
  Inclui afirmativas INDIRETAS após pergunta de agendamento — "quais horários?",
  "quando posso passar aí?", "tem horário amanhã?", "que dia tá livre?", "posso
  ir hoje?", "amanhã de manhã serve?". Tudo isso é CONFIRMAÇÃO de querer agendar.
- "apresentar": quer ver opções de veículos.

# intent_secundario (mantido pra compat; PRIMÁRIO é `topics`)
- "duvida_operacional": pergunta sobre processo (paga, financia, troca, doc) → responder vai chamar get_faq.
- "ver_outros_carros": quer alternativas → search_inventory.
- "pedido_foto": pediu imagem.

# topics (CRÍTICO — multi-intenção por turno)
Liste em `topics` TODOS os tópicos identificados na MENSAGEM ATUAL do lead.
Diferente de `intent_secundario` (1 valor só), `topics` é lista — preencha
com tudo que aparecer. O orchestrator dispara uma ferramenta por tópico:

- "duvida_operacional": qualquer pergunta sobre processo/preço/financiamento/
  pagamento/troca/documentação/endereço/horário de funcionamento/localização.
- "agendamento": quer marcar visita OU pergunta indireta sobre quando passar
  ("quais horários?", "tem horário amanhã?", "posso ir hoje?").
- "ver_outros_carros": quer ver alternativas/outros modelos.
- "pedido_foto": quer imagem.

Exemplos:
- "Quais horários posso passar? Qual o endereço?"  -> ["agendamento","duvida_operacional"]
- "Tem fotos? Aceita financiamento?"                -> ["pedido_foto","duvida_operacional"]
- "Tem outro Onix? Vocês têm consórcio?"            -> ["ver_outros_carros","duvida_operacional"]
- "Quero ver mais detalhes desse, preço?"           -> ["duvida_operacional"]
- "Sim, quero agendar"                              -> ["agendamento"]
- "Compra direta, sem troca"                        -> []  (resposta de funil, sem tópico secundário)

NÃO liste o mesmo tópico 2x. Liste vazio `[]` se o turno é só resposta de funil.

# Handoff
- `should_handoff=true` quando:
  * opt_out / irritação: imediato (terminal_reason="handoff_solicitado").
  * pedido_humano 2ª vez (humano_solicitado_count atual >= 1 e voltou a pedir).
- `pode_handoff=true` quando os 10 campos estão OK OU appointment confirmado.
- `humano_solicitado_count_delta=1` apenas se ESTE turno o lead pediu humano.
- `ai_identity_asked_count_delta=1` apenas se ESTE turno o lead questionou identidade IA.

# Terminal reasons (somente se aplicável neste turno)
- "qualificado_agendado": appointment criado (orquestrador costuma setar; só preencha
  se o lead acabou de aceitar slot e você está certo).
- "qualificado_sem_agenda": OS 10 CAMPOS estão preenchidos NESTE TURNO E o lead
  recusou agendamento (ex: "não quero agendar agora", "não tenho data certa").
  Verifique que collected NÃO tem campos null (incl. cidade, forma_pagamento etc).
- "handoff_solicitado": pedido humano confirmado / opt_out / irritação.
- "handoff_erro": falha técnica (não decidir aqui — orquestrador seta).

REGRA OBRIGATÓRIA: se `should_handoff=true`, então `terminal_reason` DEVE ser preenchido
(normalmente "handoff_solicitado"). Não deixe null nesse caso.

# Slot escolhido pra agendamento
- `chosen_slot_iso` SÓ é preenchido quando, no turno anterior, o agente propôs slots
  específicos (visíveis no histórico) E o lead aceitou explicitamente um deles
  (ex: "pode ser quinta 09:30", "o primeiro tá ok", "amanhã 10 horas serve").
- Use exatamente o ISO8601 com offset (-03:00) que aparece no histórico/proposta.
- Em qualquer outra situação: `chosen_slot_iso=null`.
- Se o lead deu preferência VAGA (apenas "amanhã de manhã"), preencha
  `preferencia_horario` em vez de chosen_slot_iso.

# Importante
- Seja conservador: não invente dados. Se o lead foi vago, deixe campo null.
- Não duplicar contagem: deltas são 0 ou 1 por turno.
- `next_action`: 1 frase curta operacional (ex: "perguntar nome", "apresentar 3 matches",
  "propor slots de agendamento", "puxar foco antes de agendar").
"""


def _build_user_payload(
    *, history: list[dict], state: SessionState, last_message: str
) -> str:
    hist_compact = [
        {
            "from": "lead" if m.get("direction") == "inbound" else "patricia",
            "type": m.get("messageType") or m.get("type"),
            "body": (m.get("body") or "")[:500],
            "ts": m.get("dateAdded"),
        }
        for m in history[-30:]  # mantém payload enxuto
    ]
    return json.dumps(
        {
            "session_state": state.model_dump(),
            "history": hist_compact,
            "last_message": last_message,
        },
        ensure_ascii=False,
        default=str,
    )


async def run_updater(
    *,
    history: list[dict],
    state: SessionState,
    last_message: str,
) -> StateUpdate:
    user = _build_user_payload(history=history, state=state, last_message=last_message)
    log.info(
        "updater_call",
        stage=state.stage,
        humano_cnt=state.humano_solicitado_count,
        last_len=len(last_message),
    )
    out = await parse_structured(
        model=settings.openai_model_updater,
        schema=StateUpdate,
        system=SYSTEM_PROMPT,
        user=user,
        component="updater",
        temperature=0.0,
    )
    log.info(
        "updater_result",
        stage=out.stage,
        intent=out.intent,
        intent_sec=out.intent_secundario,
        should_handoff=out.should_handoff,
        terminal=out.terminal_reason,
    )
    return out


def merge_into_state(state: SessionState, update: StateUpdate) -> SessionState:
    """Aplica deltas: stage, collected, counters.

    Regras de merge por campo:
      - troca_completa: DEEP MERGE (cada subcampo preserva valor existente se
        update vier null; nunca substitui {modelo,ano,km,quitado} atômico).
      - veiculo_interesse / veiculo_interesse_confirmado: permitem OVERRIDE
        quando update traz valor non-null (lead pode mudar foco).
      - motivo_compra_ou_troca: permite OVERRIDE (lead pode refinar/atualizar).
      - veiculo_interesse_confirmado: True sticky (não regride sem update explícito).
      - demais campos: só preenchem se atual está vazio (não regridem).
    """
    new = state.model_copy(deep=True)
    new.stage = update.stage
    new.last_sentiment = update.sentiment
    new.last_intent = update.intent

    cur: dict[str, Any] = new.collected.model_dump()
    nxt: dict[str, Any] = update.collected.model_dump()

    OVERRIDE_FIELDS = {"veiculo_interesse", "motivo_compra_ou_troca"}
    # Campos com semântica tri-state (None / True / False são todos válidos).
    # Pra eles, False é dado VÁLIDO — não tratar como "vazio".
    TRISTATE_BOOL_FIELDS = {"possui_troca", "interesse_agendamento"}

    def _is_empty(val: Any) -> bool:
        return val is None or val == ""

    for k, v in nxt.items():
        if k == "troca_completa":
            # Deep merge field-by-field. Preserva subcampos já preenchidos.
            cur_t = cur.get(k) or {}
            nxt_t = v or {}
            merged: dict[str, Any] = {}
            for tk in ("modelo", "ano", "km", "quitado"):
                old_val = cur_t.get(tk)
                new_val = nxt_t.get(tk)
                if old_val not in (None, "") and new_val in (None, ""):
                    merged[tk] = old_val
                elif new_val not in (None, ""):
                    merged[tk] = new_val
                else:
                    merged[tk] = old_val
            cur[k] = merged if any(x is not None for x in merged.values()) else None
            continue

        if k in OVERRIDE_FIELDS:
            # Override quando update traz valor non-null
            if v not in (None, ""):
                cur[k] = v
            continue

        if k == "veiculo_interesse_confirmado":
            # Sticky True; só atualiza se update explicitamente trouxe True
            if v is True:
                cur[k] = True
            continue

        if k in TRISTATE_BOOL_FIELDS:
            # False é dado VÁLIDO (lead disse explicitamente "sem troca").
            # Só preenche se atual é None E update trouxe valor bool.
            if cur.get(k) is None and v is not None:
                cur[k] = v
            continue

        # Demais campos: só preenche se atual estava vazio
        if _is_empty(cur.get(k)) and not _is_empty(v):
            cur[k] = v

    new.collected = type(new.collected)(**cur)

    new.humano_solicitado_count += max(0, min(1, update.humano_solicitado_count_delta))
    new.ai_identity_asked_count += max(0, min(1, update.ai_identity_asked_count_delta))

    if update.terminal_reason:
        new.terminal_reason = update.terminal_reason
    return new
