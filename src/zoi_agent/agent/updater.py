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
Você é o COMPONENTE DE ESTADO de um atendente virtual chamado "Lucas" da AMC Veículos
(seminovos, Joinville/SC). Sua função é APENAS extrair estado estruturado.

Você NÃO gera texto pro cliente. Outro componente (responder) faz isso. Aqui você só
preenche o schema StateUpdate com base em:
  1) histórico recente da conversa (GHL),
  2) session_state atual,
  3) última mensagem do lead.

# Funil PRIORITY (ordem fixa)
Estes 10 campos devem ser preenchidos nesta ordem:
  {", ".join(PRIORITY_FIELDS)}

Regras:
- `vehicle_focus_definido=true` quando o lead convergiu num único veículo do estoque
  (ou um modelo bem específico do interesse de origem).
- `possui_troca` boolean; se true, `troca_completa` exige modelo, ano, km e quitado.
- Se um campo já está no state.collected, NÃO sobrescreva por valor menos específico.
- `missing`: lista os campos do funil ainda em aberto, na ordem PRIORITY.

# Stages
- "abertura": pós-saudação, sem nome ainda.
- "descoberta": qualificando.
- "apresentacao": lead pediu ver outros carros OU vehicle_focus indefinido.
- "fechamento": 10 campos OK OU (interesse_agendamento=true AND vehicle_focus_definido=true).
- "fechado": terminal action já executada (não muda mais).

Regressão de stage é permitida (lead pode pedir ver outro carro em fechamento).

# Intents
- "qualificar": lead respondendo perguntas do funil.
- "duvida": pergunta operacional (financiamento, localização, etc).
- "opt_out": pediu pra parar / xingou / irritação clara.
- "pedido_humano": pediu vendedor.
- "agendamento": quer marcar visita.
- "apresentar": quer ver opções de veículos.

# intent_secundario (não exclusivo)
- "duvida_operacional": pergunta sobre processo (paga, financia, troca, doc) → responder vai chamar get_faq.
- "ver_outros_carros": quer alternativas → search_inventory.
- "pedido_foto": pediu imagem.

# Handoff
- `should_handoff=true` quando:
  * opt_out / irritação: imediato (terminal_reason="handoff_solicitado").
  * pedido_humano 2ª vez (humano_solicitado_count atual >= 1 e voltou a pedir).
- `pode_handoff=true` quando os 10 campos estão OK OU appointment confirmado.
- `humano_solicitado_count_delta=1` apenas se ESTE turno o lead pediu humano.
- `ai_identity_asked_count_delta=1` apenas se ESTE turno o lead questionou identidade IA.

# Terminal reasons (somente se aplicável neste turno)
- "qualificado_agendado": appointment criado.
- "qualificado_sem_agenda": 10 campos OK e lead recusou agendar.
- "handoff_solicitado": pedido humano confirmado / opt_out / irritação.
- "handoff_erro": falha técnica (não decidir aqui — orquestrador seta).

REGRA OBRIGATÓRIA: se `should_handoff=true`, então `terminal_reason` DEVE ser preenchido
(normalmente "handoff_solicitado"). Não deixe null nesse caso.

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
            "from": "lead" if m.get("direction") == "inbound" else "lucas",
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
    """Aplica deltas: stage, collected (não regride campo preenchido), counters."""
    new = state.model_copy(deep=True)
    new.stage = update.stage
    new.last_sentiment = update.sentiment
    new.last_intent = update.intent

    # Merge collected: novo só preenche se atual está vazio
    cur: dict[str, Any] = new.collected.model_dump()
    nxt: dict[str, Any] = update.collected.model_dump()
    for k, v in nxt.items():
        if cur.get(k) in (None, "", False) and v not in (None, "", False):
            cur[k] = v
        elif k == "vehicle_focus_definido" and v is True:
            cur[k] = True
    new.collected = type(new.collected)(**cur)

    new.humano_solicitado_count += max(0, min(1, update.humano_solicitado_count_delta))
    new.ai_identity_asked_count += max(0, min(1, update.ai_identity_asked_count_delta))

    if update.terminal_reason:
        new.terminal_reason = update.terminal_reason
    return new
