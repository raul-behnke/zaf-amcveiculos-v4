"""Runner: orquestra 2 Agno Agents sequencialmente (EstoqueExpert -> Patricia).

Substitui o Agno Team Coordinate (que estava deixando o leader Patricia
"alucinar" InventoryDecision sem realmente delegar pro member). Agora o
orchestrator decide via SINAL DETERMINÍSTICO quando o EstoqueExpert é
chamado; resultado dele é injetado no input da Patricia.

Pipeline (quando há sinal de estoque):
  1) orchestrator detecta sinal (intent/topics/state/last_message)
  2) EstoqueExpert.arun(payload) -> InventoryDecision
  3) Patricia.arun(payload + inventory_decision) -> BubbleSequence
  4) compose final bubbles: [abertura?, cards?, fechamento]

Quando NÃO há sinal de estoque: pula passo 2, Patricia roda direto.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from agno.agent import Agent
from agno.models.openai import OpenAIChat

from zoi_agent.agent.question_planner import NextQuestion
from zoi_agent.agent.schemas import SessionState, StateUpdate
from zoi_agent.agent.templates import render_vehicle_card, render_vehicle_list
from zoi_agent.config import settings
from zoi_agent.logging import get_logger
from zoi_agent.team.inventory_expert import build_inventory_expert
from zoi_agent.team.patricia import PATRICIA_INSTRUCTIONS, consultar_faq
from zoi_agent.team.schemas import (
    BubbleSequence,
    InventoryAction,
    InventoryDecision,
    VeiculoSelecionado,
)
from zoi_agent.tools.inventory import get_vehicle_details, load_inventory

log = get_logger(__name__)

# Timeouts duros pra detectar trava silenciosa do Agno arun()
_ARUN_TIMEOUT_S = 45.0


# --- Detecção de sinal de estoque ------------------------------------------


_VEHICLE_KEYWORDS = (
    "foto", "fotos", "carro", "veicul", "modelo", "anuncio", "anúncio",
    "manda", "mostra", "mostra-me", "mostrar", "tem algum", "tem outro",
    "tem outra", "tem mais", "tem mais alguma", "alternativ", "outra opção",
    "outras opções", "outras opcoes", "mais barato", "mais novo", "mais novinho",
    "tem alguma", "vocês tem", "voce tem", "vc tem", "tem disponivel",
    "tem disponível", "marca", "ano",
)


def detect_inventory_signal(
    *,
    state: SessionState,
    update: StateUpdate,
    last_message: str,
) -> dict[str, Any]:
    """Decide deterministicamente se o EstoqueExpert deve ser chamado.

    Sinal forte (chama):
      - 1º turno com veiculo_origem (mostrar origem ou alternativa)
      - update.intent == "apresentar"
      - update.intent_secundario in {ver_outros_carros, pedido_foto}
      - "ver_outros_carros" in update.topics
      - "pedido_foto" in update.topics
      - last_message contém keyword de veículo (foto, modelo, anúncio, etc)

    Sinal sutil (NÃO chama, deixa Patricia conduzir funil):
      - intent == "qualificar", topics vazios, vehicles_shown não-vazio,
        pergunta de funil normal
    """
    reasons: list[str] = []
    no_vehicles_shown = not (state.vehicles_shown or [])
    has_origem = bool(state.veiculo_origem and state.veiculo_origem.texto)

    if has_origem and no_vehicles_shown:
        reasons.append("primeiro_turno_com_origem")
    if update.intent == "apresentar":
        reasons.append("intent_apresentar")
    if update.intent_secundario in ("ver_outros_carros", "pedido_foto"):
        reasons.append(f"intent_sec_{update.intent_secundario}")
    topics = set(update.topics or [])
    if "ver_outros_carros" in topics:
        reasons.append("topic_ver_outros_carros")
    if "pedido_foto" in topics:
        reasons.append("topic_pedido_foto")

    lm_low = (last_message or "").lower()
    if any(kw in lm_low for kw in _VEHICLE_KEYWORDS):
        reasons.append("keyword_in_message")

    return {
        "trigger": bool(reasons),
        "reasons": reasons,
    }


# --- Input payload ---------------------------------------------------------


def _serialize_history(history: list[dict], limit: int = 20) -> list[dict]:
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


def _base_payload(
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
    faq_yaml: str | None = None,
) -> dict[str, Any]:
    return {
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
        "faq_yaml": faq_yaml,
        "history_recent": _serialize_history(history, limit=20),
    }


# --- Validação / blindagem da InventoryDecision -----------------------------


async def _validate_inventory_decision(
    decision: InventoryDecision,
) -> tuple[InventoryDecision, list[str]]:
    """Blindagem da decisão do EstoqueExpert. Retorna (decisão_validada, warnings).

    Defesas:
      1. Filtra external_ids inexistentes no inventário real (anti-alucinação ID).
      2. Detecta incoerência: action=mostrar_card_* com veiculos_selecionados vazio.
      3. Degrada graciosamente:
         - mostrar_card_* sem veículos válidos -> nao_mostrar (não mente)
         - mostrar_card_unico com >1 veículo válido -> mostrar_card_lista
         - mostrar_card_lista com 1 veículo válido -> mostrar_card_unico
      4. Filtra enviar_fotos_de inválido (não força envio com ID inexistente).
    """
    warnings: list[str] = []
    inv = await load_inventory()
    valid_ids = {str(v.get("external_id")) for v in inv if v.get("external_id")}

    # Filtra veiculos_selecionados pra apenas IDs reais
    valid_selecionados: list[VeiculoSelecionado] = []
    for v in decision.veiculos_selecionados:
        if v.external_id and str(v.external_id) in valid_ids:
            valid_selecionados.append(v)
        else:
            warnings.append(f"ID_INEXISTENTE:{v.external_id}")

    # Filtra enviar_fotos_de
    safe_enviar_fotos = decision.enviar_fotos_de
    if safe_enviar_fotos and str(safe_enviar_fotos) not in valid_ids:
        warnings.append(f"ENVIAR_FOTOS_ID_INEXISTENTE:{safe_enviar_fotos}")
        safe_enviar_fotos = None

    # Coerência action × veiculos
    new_action: InventoryAction = decision.action
    if decision.action in ("mostrar_card_unico", "mostrar_card_lista"):
        if not valid_selecionados:
            warnings.append("ACTION_MOSTRAR_SEM_VEICULOS_VALIDOS")
            new_action = "nao_mostrar"
        elif decision.action == "mostrar_card_unico" and len(valid_selecionados) > 1:
            warnings.append("UNICO_COM_MULTIPLOS:upgrade_to_lista")
            new_action = "mostrar_card_lista"
        elif decision.action == "mostrar_card_lista" and len(valid_selecionados) == 1:
            warnings.append("LISTA_COM_UM:downgrade_to_unico")
            new_action = "mostrar_card_unico"

    # comentar_em_texto requer pelo menos 1 ID válido
    if decision.action == "comentar_em_texto" and not valid_selecionados:
        warnings.append("COMENTAR_SEM_VEICULOS_VALIDOS")
        new_action = "nao_mostrar"

    # perguntar_refinamento sem texto -> degrada
    if decision.action == "perguntar_refinamento" and not decision.pergunta_refinamento:
        warnings.append("REFINAMENTO_SEM_PERGUNTA")
        new_action = "nao_mostrar"

    validated = InventoryDecision(
        action=new_action,
        veiculos_selecionados=valid_selecionados,
        pergunta_refinamento=decision.pergunta_refinamento,
        hint_narrativo=decision.hint_narrativo,
        texto_sugerido_apresentacao=decision.texto_sugerido_apresentacao,
        enviar_fotos_de=safe_enviar_fotos,
        motivo_geral=decision.motivo_geral,
    )
    return validated, warnings


async def _call_inventory_expert_with_retry(
    base_payload: dict[str, Any],
    *,
    max_attempts: int = 2,
) -> InventoryDecision | None:
    """Chama EstoqueExpert com 1 retry em caso de output inválido.

    Retry-trigger:
      - Output não é InventoryDecision (schema parse fail)
      - action=mostrar_card_* com veiculos_selecionados vazio
      - Todos os external_ids são inválidos (anti-alucinação ID)

    Em caso de falha persistente: retorna a melhor versão validada possível
    (action pode virar nao_mostrar / comentar_em_texto).
    """
    last_decision: InventoryDecision | None = None
    feedback: str | None = None

    for attempt in range(1, max_attempts + 1):
        payload = dict(base_payload)
        if feedback:
            payload["_retry_feedback"] = feedback

        try:
            expert = await build_inventory_expert()
            expert_input = json.dumps(payload, ensure_ascii=False, default=str)
            expert_result = await asyncio.wait_for(
                expert.arun(input=expert_input), timeout=_ARUN_TIMEOUT_S
            )
            expert_content = getattr(expert_result, "content", None)

            if not isinstance(expert_content, InventoryDecision):
                log.error(
                    "inventory_expert_output_not_schema",
                    attempt=attempt,
                    type=type(expert_content).__name__,
                )
                feedback = (
                    "Sua resposta anterior não respeitou o schema InventoryDecision. "
                    "Devolva EXATAMENTE no schema, com action válido e veiculos_selecionados[]."
                )
                continue

            validated, warnings = await _validate_inventory_decision(expert_content)
            last_decision = validated

            if warnings:
                log.warning(
                    "inventory_decision_warnings",
                    attempt=attempt,
                    warnings=warnings,
                    original_action=expert_content.action,
                    validated_action=validated.action,
                )

            # Decide se retry vale a pena
            had_critical_error = any(
                w.startswith("ACTION_MOSTRAR_SEM_VEICULOS") or
                w.startswith("ID_INEXISTENTE") or
                w == "REFINAMENTO_SEM_PERGUNTA"
                for w in warnings
            )
            if had_critical_error and attempt < max_attempts:
                feedback = (
                    f"REGENERE. Erros na sua resposta anterior: {', '.join(warnings)}. "
                    "Use APENAS external_ids que aparecem no snapshot INVENTÁRIO. "
                    "Se for mostrar_card_*, veiculos_selecionados DEVE ter pelo menos 1 ID válido. "
                    "Se não tem certeza, use comentar_em_texto ou nao_mostrar."
                )
                continue

            return validated
        except Exception as e:
            log.error("inventory_expert_call_failed", attempt=attempt, err=str(e))
            feedback = (
                "Sua chamada falhou. Tente novamente respeitando rigorosamente o schema."
            )

    return last_decision


# --- Patricia Agent (standalone — sem Team Coordinate) ---------------------


def build_patricia_agent() -> Agent:
    """Patricia como Agent standalone. Output: BubbleSequence (sem inventory_decision)."""
    return Agent(
        name="Patricia",
        model=OpenAIChat(
            id=settings.openai_model_patricia,
            api_key=settings.openai_api_key,
        ),
        description=(
            "SDR conversacional da AMC Veículos (Joinville/SC). Empática, "
            "experiente, conduz qualificação de leads via WhatsApp."
        ),
        role="Conversa com o lead, qualifica, integra decisão de estoque.",
        instructions=PATRICIA_INSTRUCTIONS,
        tools=[consultar_faq],
        output_schema=BubbleSequence,
        markdown=False,
        telemetry=False,
    )


# --- Card rendering ---------------------------------------------------------


async def _render_cards_from_decision(
    decision: InventoryDecision,
) -> tuple[list[str], list[str]]:
    bolhas: list[str] = []
    rendered_ids: list[str] = []

    if decision.action not in ("mostrar_card_unico", "mostrar_card_lista"):
        rendered_ids = [v.external_id for v in decision.veiculos_selecionados if v.external_id]
        return bolhas, rendered_ids

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
    else:
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
    faq_yaml: str | None = None,
) -> dict[str, Any]:
    """Executa o turno com 2 Agno Agents sequenciais (quando há sinal de estoque).

    Returns dict com:
      - bubbles: list[str] — bolhas finais na ordem de envio
      - rendered_vehicle_ids: list[str] — IDs de veículos efetivamente apresentados
      - inventory_decision: InventoryDecision | None
    """
    signal = detect_inventory_signal(
        state=state, update=update, last_message=last_message
    )
    log.info(
        "team_turn_start",
        contact_stage=state.stage,
        last_message_preview=last_message[:60],
        next_q_field=next_question.field,
        inventory_signal=signal["reasons"],
    )

    base = _base_payload(
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
        faq_yaml=faq_yaml,
    )

    # 1) EstoqueExpert (quando há sinal) — com retry + validação blindada
    inv_decision: InventoryDecision | None = None
    if signal["trigger"]:
        inv_decision = await _call_inventory_expert_with_retry(base)
        if inv_decision:
            log.info(
                "inventory_decision",
                action=inv_decision.action,
                n_veiculos=len(inv_decision.veiculos_selecionados),
                motivo=(inv_decision.motivo_geral or "")[:140],
                enviar_fotos_de=inv_decision.enviar_fotos_de,
            )

    # Renderiza cards determinísticos pra inserir entre bolhas Patricia
    cards_bolhas: list[str] = []
    rendered_ids: list[str] = []
    if inv_decision:
        cards_bolhas, rendered_ids = await _render_cards_from_decision(inv_decision)

    # 2) Patricia — recebe payload base + inventory_decision (já validada)
    patricia_payload = dict(base)
    patricia_payload["inventory_decision"] = (
        inv_decision.model_dump() if inv_decision else None
    )
    patricia_payload["cards_renderizados_serao_inseridos_entre_bolhas"] = bool(cards_bolhas)
    # CONTRATO DURO: se NÃO há cards (cards_bolhas vazio), Patricia NÃO PODE
    # mencionar/prometer veículos no texto. Anti-mentira ("separei opções",
    # "olha essas alternativas") quando o orquestrador não vai inserir nada.
    patricia_payload["_contrato_apresentacao"] = (
        "VAI HAVER cards inseridos entre abertura e fechamento — você pode "
        "fazer a ponte ('olha essas opções', 'separei aqui')."
        if cards_bolhas
        else (
            "NÃO HAVERÁ cards neste turno. PROIBIDO dizer 'separei opções', "
            "'olha essas alternativas', 'achei essas', 'tenho algumas pra você' "
            "OU prometer veículos que NÃO existem nas bolhas. Se "
            "inventory_decision.action='comentar_em_texto', responda em prosa "
            "usando hint_narrativo. Se action='nao_mostrar' ou null, conduza "
            "funil/FAQ normalmente sem mencionar estoque inexistente."
        )
    )

    patricia = build_patricia_agent()
    patricia_input = json.dumps(patricia_payload, ensure_ascii=False, default=str)
    try:
        result = await asyncio.wait_for(
            patricia.arun(input=patricia_input), timeout=_ARUN_TIMEOUT_S
        )
    except TimeoutError:
        log.error("patricia_arun_timeout", timeout_s=_ARUN_TIMEOUT_S)
        raise RuntimeError(f"Patricia.arun timeout >{_ARUN_TIMEOUT_S}s") from None
    content = getattr(result, "content", None)

    if not isinstance(content, BubbleSequence):
        log.error("patricia_output_not_schema", type=type(content).__name__)
        text = str(content) if content else ""
        return {
            "bubbles": [text] if text else ["Desculpa, vou chamar o consultor."],
            "rendered_vehicle_ids": rendered_ids,
            "inventory_decision": inv_decision,
        }

    seq: BubbleSequence = content
    bubbles: list[str] = []
    if seq.abertura:
        bubbles.append(seq.abertura)
    bubbles.extend(cards_bolhas)
    for extra in (seq.bolhas_extras or [])[:2]:
        if extra and extra.strip():
            bubbles.append(extra.strip())
    bubbles.append(seq.fechamento)

    return {
        "bubbles": bubbles,
        "rendered_vehicle_ids": rendered_ids,
        "inventory_decision": inv_decision,
    }
