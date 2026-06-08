"""Runner: monta input do Team, chama, e devolve bolhas + tracking.

Substitui o `agent/responder.py::run_responder` + dispatch heurístico de
busca de estoque (`buscar_veiculo_interesse_origem`, `search_inventory`,
`build_photo_payload`) — toda essa lógica migra pro EstoqueExpert dentro
do Team coordinate.

Mantém responsabilidade pelo:
  - Build do payload de input do Team (state + history + next_question + ...)
  - Composição final das bolhas: [abertura?, cards?, fechamento]
  - Tracking de rendered_vehicle_ids pra atualizar vehicles_shown / last_card_external_id
"""
from __future__ import annotations

import json
from typing import Any

from zoi_agent.agent.question_planner import NextQuestion
from zoi_agent.agent.schemas import SessionState, StateUpdate
from zoi_agent.agent.templates import (
    render_vehicle_card,
    render_vehicle_list,
)
from zoi_agent.logging import get_logger
from zoi_agent.team.schemas import BubbleSequence, InventoryDecision
from zoi_agent.team.sdr_team import build_sdr_team
from zoi_agent.tools.inventory import get_vehicle_details

log = get_logger(__name__)


# --- Input payload --------------------------------------------------------


def _serialize_history(history: list[dict], limit: int = 20) -> list[dict]:
    """Recorta histórico recente pra Team (mantém só essencial)."""
    msgs = sorted(history or [], key=lambda m: m.get("dateAdded") or "")
    out: list[dict] = []
    for m in msgs[-limit:]:
        out.append({
            "direction": m.get("direction"),
            "type": m.get("type"),
            "body": m.get("body"),
            "dateAdded": m.get("dateAdded"),
        })
    return out


def build_team_input(
    *,
    state: SessionState,
    update: StateUpdate,
    next_question: NextQuestion,
    history: list[dict],
    last_message: str,
    tom_turno: str,
    acknowledge_hint: dict[str, Any] | None,
    slots: list[dict] | None,
    vehicle_in_focus: dict | None,
    booking_result: dict | None,
) -> str:
    """Serializa tudo que o Team precisa em JSON estável (string).

    Patricia (Team leader) consome este JSON como user message. Inventário
    fica no system prompt do EstoqueExpert (additional_context).
    """
    payload = {
        "last_message": last_message,
        "state": {
            "stage": state.stage,
            "collected": state.collected.model_dump(exclude_none=True),
            "veiculo_origem": (
                state.veiculo_origem.model_dump() if state.veiculo_origem else None
            ),
            "vehicles_shown": list(state.vehicles_shown or []),
            "last_card_external_id": state.last_card_external_id,
            "humano_solicitado_count": state.humano_solicitado_count,
            "ai_identity_asked_count": state.ai_identity_asked_count,
            "last_sentiment": state.last_sentiment,
            "appointment": state.appointment,
        },
        "update": {
            "stage": update.stage,
            "intent": update.intent,
            "topics": list(update.topics or []),
            "sentiment": update.sentiment,
            "should_handoff": update.should_handoff,
            "pode_handoff": update.pode_handoff,
            "preferencia_horario": (
                update.preferencia_horario.model_dump(exclude_none=True)
                if update.preferencia_horario else None
            ),
            "chosen_slot_iso": update.chosen_slot_iso,
        },
        "next_question": {
            "field": next_question.field,
            "intent": next_question.intent,
            "canonical_text": next_question.canonical_text,
            "skip_funnel_reason": next_question.skip_funnel_reason,
        },
        "tom_turno": tom_turno,
        "acknowledge_hint": acknowledge_hint or {},
        "slots": slots or [],
        "vehicle_in_focus": vehicle_in_focus,
        "booking_result": booking_result,
        "history_recent": _serialize_history(history, limit=20),
    }
    return json.dumps(payload, ensure_ascii=False, default=str)


# --- Card rendering ---------------------------------------------------------


async def _render_cards_from_decision(
    decision: InventoryDecision,
) -> tuple[list[str], list[str]]:
    """Renderiza cards determinísticos quando action=mostrar_card_*.

    Retorna (bolhas_cards, rendered_external_ids).
    Para `comentar_em_texto`, `perguntar_refinamento`, `nao_mostrar`:
    sem cards renderizados, IDs ainda contam pra tracking (vehicles_selecionados).
    """
    bolhas: list[str] = []
    rendered_ids: list[str] = []

    if decision.action not in ("mostrar_card_unico", "mostrar_card_lista"):
        # IDs ainda registram interesse (pra tracking vehicles_shown),
        # mas sem renderização visual.
        rendered_ids = [v.external_id for v in decision.veiculos_selecionados if v.external_id]
        return bolhas, rendered_ids

    # Pega ficha completa de cada veículo selecionado pra renderizar card
    veiculos_dicts: list[dict[str, Any]] = []
    for v in decision.veiculos_selecionados:
        if not v.external_id:
            continue
        details = await get_vehicle_details(v.external_id)
        if details:
            veiculos_dicts.append(details)

    if not veiculos_dicts:
        return bolhas, rendered_ids

    if decision.action == "mostrar_card_unico":
        bolhas.append(render_vehicle_card(veiculos_dicts[0]))
        rendered_ids.append(str(veiculos_dicts[0].get("external_id")))
    else:  # mostrar_card_lista
        bolhas.append(render_vehicle_list(veiculos_dicts[:3]))
        for v in veiculos_dicts[:3]:
            eid = v.get("external_id")
            if eid:
                rendered_ids.append(str(eid))

    return bolhas, rendered_ids


# --- Public API -----------------------------------------------------------


async def run_team_turn(
    *,
    state: SessionState,
    update: StateUpdate,
    next_question: NextQuestion,
    history: list[dict],
    last_message: str,
    tom_turno: str,
    acknowledge_hint: dict[str, Any] | None,
    slots: list[dict] | None = None,
    vehicle_in_focus: dict | None = None,
    booking_result: dict | None = None,
) -> dict[str, Any]:
    """Executa um turno completo do SDR Team e devolve bolhas compostas.

    Returns dict com:
      - bubbles: list[str] — bolhas finais na ordem de envio
      - rendered_vehicle_ids: list[str] — IDs de veículos efetivamente apresentados
      - inventory_decision: InventoryDecision | None — decisão do member (pra logs)
    """
    team = await build_sdr_team()
    payload = build_team_input(
        state=state,
        update=update,
        next_question=next_question,
        history=history,
        last_message=last_message,
        tom_turno=tom_turno,
        acknowledge_hint=acknowledge_hint,
        slots=slots,
        vehicle_in_focus=vehicle_in_focus,
        booking_result=booking_result,
    )

    log.info(
        "team_turn_start",
        contact_stage=state.stage,
        last_message_preview=last_message[:60],
        next_q_field=next_question.field,
    )

    result = await team.arun(input=payload)
    content = getattr(result, "content", None)

    if not isinstance(content, BubbleSequence):
        # Fallback defensivo se Agno devolveu string ao invés de schema
        log.error("team_output_not_schema", type=type(content).__name__)
        text = str(content) if content else ""
        return {
            "bubbles": [text] if text else ["Desculpa, vou chamar o consultor."],
            "rendered_vehicle_ids": [],
            "inventory_decision": None,
        }

    seq: BubbleSequence = content
    inv_decision = seq.inventory_decision

    cards_bolhas: list[str] = []
    rendered_ids: list[str] = []
    if inv_decision:
        cards_bolhas, rendered_ids = await _render_cards_from_decision(inv_decision)
        log.info(
            "inventory_decision",
            action=inv_decision.action,
            n_veiculos=len(inv_decision.veiculos_selecionados),
            motivo=inv_decision.motivo_geral[:120] if inv_decision.motivo_geral else None,
            rendered_count=len(cards_bolhas),
        )

    bubbles: list[str] = []
    if seq.abertura:
        bubbles.append(seq.abertura)
    bubbles.extend(cards_bolhas)
    bubbles.append(seq.fechamento)

    return {
        "bubbles": bubbles,
        "rendered_vehicle_ids": rendered_ids,
        "inventory_decision": inv_decision,
    }
