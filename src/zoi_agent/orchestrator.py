"""Per-contact orchestrator: preempção via Task table + pipeline updater->team->send.

Pipeline por turno (pós-Agno Team):
  1) load session_state (DB)
  2) se terminal_reason setado: ignora (não responde)
  3) fetch histórico GHL (limit 100)
  4) run_updater -> StateUpdate
  5) merge_into_state
  6) plan_next_question (planner determinístico)
  7) dispatch determinístico (tom_turno, acknowledge_hint, vehicle_in_focus,
     propose_slots/find_exact_slot quando agendamento)
  8) guard: strip premature terminal
  9) book_appointment se chosen_slot_iso ou auto_book_slot_iso
  10) Team Agno (Patricia leader + EstoqueExpert member) -> BubbleSequence
  11) compose final bubbles, _enforce_singular_question
  12) shield(send_bubbles)  -- não é cancelado por preempção
  13) handle terminal + save state
"""
from __future__ import annotations

import asyncio
import random
from typing import Any

from zoi_agent.agent.question_planner import plan_next_question, push_asked_field
from zoi_agent.agent.schemas import SessionState
from zoi_agent.agent.updater import merge_into_state, run_updater
from zoi_agent.config import settings
from zoi_agent.db import sessions as session_repo
from zoi_agent.ghl import conversations as ghl_conv
from zoi_agent.logging import get_logger
from zoi_agent.metrics import TURNS_TOTAL
from zoi_agent.team.runner import run_team_turn
from zoi_agent.tools.calendar import book_appointment, find_exact_slot, propose_slots
from zoi_agent.tools.faq import get_faq_raw
from zoi_agent.tools.handoff import encaminhar_para_vendedor
from zoi_agent.tools.inventory import get_vehicle_details
from zoi_agent.tools.photos import build_photo_payload_by_id
from zoi_agent.tools.terminal import TERMINAL_REASONS

log = get_logger(__name__)


# --- Task table (preempção por contactId) ---------------------------------

_TASKS: dict[str, asyncio.Task] = {}


_PLURAL_PATTERNS: tuple[tuple[str, str], ...] = (
    ("algum desses veículos chamou a sua atenção", "esse veículo te chamou atenção"),
    ("algum desses veículos chamou sua atenção", "esse veículo te chamou atenção"),
    ("algum desses chamou a sua atenção", "esse te chamou atenção"),
    ("algum desses chamou sua atenção", "esse te chamou atenção"),
    ("algum desses chamou atenção", "esse te chamou atenção"),
    ("qual desses chamou mais sua atenção", "esse te chamou atenção"),
    ("qual desses chamou sua atenção", "esse te chamou atenção"),
    ("qual desses chamou atenção", "esse te chamou atenção"),
    ("qual desses", "esse"),
    ("algum desses", "esse"),
    ("quais desses", "esse"),
    ("alguns desses", "esse"),
    ("algum deles chamou", "esse te chamou"),
    ("algum deles", "esse"),
    ("desses veículos", "esse veículo"),
    ("desses carros", "esse carro"),
    ("desses modelos", "esse modelo"),
)


def _enforce_singular_question(bubble: str) -> str:
    """Reescreve frases plurais quando só 1 veículo foi apresentado."""
    if not bubble:
        return bubble
    out = bubble
    low = out.lower()
    for needle, repl in _PLURAL_PATTERNS:
        if needle in low:
            idx = low.find(needle)
            replaced = out[:idx] + repl + out[idx + len(needle):]
            if out[idx].isupper():
                replaced = out[:idx] + repl[0].upper() + repl[1:] + out[idx + len(needle):]
            out = replaced
            low = out.lower()
    return out


def _set_task(contact_id: str, task: asyncio.Task) -> None:
    _TASKS[contact_id] = task

    def _cleanup(t: asyncio.Task) -> None:
        cur = _TASKS.get(contact_id)
        if cur is t:
            _TASKS.pop(contact_id, None)

    task.add_done_callback(_cleanup)


def cancel_existing(contact_id: str) -> None:
    prev = _TASKS.get(contact_id)
    if prev and not prev.done():
        log.info("turn_preempted", contact_id=contact_id)
        prev.cancel()


# --- Dispatch determinístico (pós-Agno) -----------------------------------


async def _dispatch_deterministic(
    *,
    state: SessionState,
    update_intent: str | None,
    update_topics: list[str],
    update_preferencia_dia: str | None,
    update_preferencia_periodo: str | None,
    update_preferencia_hora: str | None,
    last_message: str,
) -> dict[str, Any]:
    """Dispatch determinístico (escopo enxuto pós-Agno):

      - tom_turno (calibrado por sentiment + stage)
      - acknowledge_hint (motivo / situacao_troca / acabou_de_dar_nome)
      - vehicle_in_focus (ficha do último card pra anti-alucinação)
      - propose_slots / find_exact_slot quando intent=agendamento + focus_ok

    Estoque (origem, search, photos prep) saiu daqui — migrou pro EstoqueExpert.
    FAQ saiu daqui — virou tool da Patricia.
    """
    out: dict[str, Any] = {}

    quer_agendar = (
        bool(state.collected.interesse_agendamento)
        or update_intent == "agendamento"
        or "agendamento" in update_topics
    )
    has_single_focus = (
        bool(state.last_card_external_id)
        or len(state.vehicles_shown or []) == 1
    )
    focus_ok = bool(state.collected.veiculo_interesse_confirmado) or has_single_focus

    if quer_agendar and focus_ok:
        # Auto-book quando lead deu hora explícita
        if update_preferencia_hora:
            try:
                exact = await find_exact_slot(
                    dia=update_preferencia_dia,
                    hora=update_preferencia_hora,
                )
                if exact:
                    out["auto_book_slot_iso"] = exact.iso
                    out["auto_book_requested_hora"] = update_preferencia_hora
            except Exception as e:
                log.error("find_exact_slot_failed", err=str(e))

        try:
            slots, fallback = await propose_slots(
                dia=update_preferencia_dia,
                periodo=update_preferencia_periodo,
                hora=update_preferencia_hora,
                limit=3,
            )
            out["slots"] = [{"iso": s.iso, "label": s.label_pt()} for s in slots]
            if fallback:
                out["slots_fallback"] = {
                    "pref_dia": update_preferencia_dia,
                    "pref_periodo": update_preferencia_periodo,
                    "motivo": "sem disponibilidade na preferência do lead",
                }
        except Exception as e:
            log.error("propose_slots_failed", err=str(e))
            out["slots"] = []
    elif quer_agendar and not focus_ok:
        out["agendamento_gate"] = {"motivo": "veiculo_interesse_confirmado=false"}

    # Vehicle in focus: ficha completa pra anti-alucinação (Patricia consome).
    # Prioridade: last_card_external_id (card único renderizado) > último de vehicles_shown.
    focus_eid: str | None = None
    if state.last_card_external_id:
        focus_eid = state.last_card_external_id
    elif state.vehicles_shown:
        focus_eid = state.vehicles_shown[-1]

    if focus_eid:
        try:
            details = await get_vehicle_details(focus_eid)
            if details:
                out["vehicle_in_focus"] = details
        except Exception as e:
            log.error("vehicle_details_failed", err=str(e))

    # FAQ — dispatch determinístico quando lead tem dúvida operacional.
    # Mais confiável que esperar Patricia chamar tool consultar_faq() —
    # injeta o YAML direto no payload pra ela usar como fonte de verdade.
    if (
        update_intent == "duvida"
        or "duvida_operacional" in update_topics
    ):
        try:
            out["faq_yaml"] = await get_faq_raw()
        except Exception as e:
            log.error("faq_fetch_failed", err=str(e))
            out["faq_yaml"] = ""

    # Tom do turno
    sentiment = getattr(state, "last_sentiment", "neutro") or "neutro"
    if sentiment == "irritado":
        tom = "empatico_calmo"
    elif sentiment == "negativo":
        tom = "empatico_acolhedor"
    elif sentiment == "positivo":
        tom = "entusiasmado_moderado"
    elif state.stage in ("fechamento",):
        tom = "objetivo_confiante"
    else:
        tom = "descontraido"
    out["tom_turno"] = tom

    # Acknowledgment hint
    ack: dict[str, Any] = {}
    last_low = (last_message or "").lower()
    motivo = (state.collected.motivo_compra_ou_troca or "").strip()
    troca = state.collected.troca_completa
    if motivo and motivo[:30].lower() in last_low:
        ack["motivo"] = motivo
    if troca and troca.quitado is False and ("financ" in last_low or "quitad" in last_low):
        ack["situacao_troca"] = "troca não quitada / possivelmente financiada"
    if state.collected.nome and state.collected.nome.lower() in last_low and len(last_low) < 30:
        ack["acabou_de_dar_nome"] = state.collected.nome
    if ack:
        out["acknowledge_hint"] = ack

    return out


# --- Sender ---------------------------------------------------------------


async def _send_bubble(
    *, contact_id: str, conversation_id: str | None, text: str
) -> None:
    await ghl_conv.send_message(
        contact_id=contact_id,
        conversation_id=conversation_id,
        message=text,
    )


async def _send_photo(
    *, contact_id: str, conversation_id: str | None, url: str
) -> None:
    await ghl_conv.send_message(
        contact_id=contact_id,
        conversation_id=conversation_id,
        attachments=[url],
    )


async def _send_photos_parallel(
    *, contact_id: str, conversation_id: str | None, urls: list[str]
) -> int:
    if not urls:
        return 0
    results = await asyncio.gather(
        *(
            _send_photo(contact_id=contact_id, conversation_id=conversation_id, url=u)
            for u in urls
        ),
        return_exceptions=True,
    )
    ok = 0
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            log.error("photo_send_failed", idx=i, url=urls[i], err=str(r))
        else:
            ok += 1
    log.info("photos_sent", total=len(urls), ok=ok)
    return ok


async def _send_bubbles(
    *,
    contact_id: str,
    conversation_id: str | None,
    bubbles: list[str],
    photos: list[str] | None = None,
) -> None:
    """1) Fotos paralelo (se houver). 2) Wait 1s. 3) Bolhas sequencial com sleeps."""
    if photos:
        await _send_photos_parallel(
            contact_id=contact_id, conversation_id=conversation_id, urls=photos
        )
        await asyncio.sleep(1.0)
    for i, b in enumerate(bubbles):
        try:
            await _send_bubble(contact_id=contact_id, conversation_id=conversation_id, text=b)
        except Exception as e:
            log.error("bubble_send_failed", idx=i, err=str(e))
            continue
        if i < len(bubbles) - 1:
            sleep_s = random.uniform(settings.responder_sleep_min, settings.responder_sleep_max)
            await asyncio.sleep(sleep_s)


# --- Pipeline -------------------------------------------------------------


async def _fetch_history(contact_id: str) -> tuple[list[dict], str | None]:
    try:
        search = await ghl_conv.search_conversations(contact_id)
        convs = search.get("conversations") or []
        if not convs:
            return [], None
        conv_id = convs[0].get("id")
        if not conv_id:
            return [], None
        msgs_resp = await ghl_conv.get_messages(conv_id)
        msgs_block = msgs_resp.get("messages") or {}
        msgs = msgs_block.get("messages") if isinstance(msgs_block, dict) else msgs_block
        return (msgs or []), conv_id
    except Exception as e:
        log.warning("history_fetch_failed", err=str(e))
        return [], None


async def _resolve_photos_to_send(
    *,
    enviar_fotos_de: str | None,
    state: SessionState,
) -> tuple[list[str], dict | None]:
    """Resolve URLs de fotos a enviar quando EstoqueExpert decidiu mostrar fotos.

    Returns (urls, vehicle_dict). vehicle_dict é usado pra atualizar foco.
    """
    if not enviar_fotos_de:
        return [], None
    try:
        payload = await build_photo_payload_by_id(external_id=str(enviar_fotos_de), state=state)
    except Exception as e:
        log.error("photo_payload_failed", err=str(e), external_id=enviar_fotos_de)
        return [], None
    if not payload.get("available") or not payload.get("images"):
        return [], None
    return list(payload["images"]), payload.get("vehicle") or {}


async def _run_turn(contact_id: str, last_message: str) -> None:
    state = await session_repo.load_or_new(contact_id)
    if state.terminal_reason:
        log.info("turn_skipped_terminal", contact_id=contact_id, reason=state.terminal_reason)
        return

    history, conversation_id = await _fetch_history(contact_id)

    try:
        update = await run_updater(history=history, state=state, last_message=last_message)
    except Exception as e:
        log.error("updater_failed_terminal", contact_id=contact_id, err=str(e))
        state.stage = "fechado"
        state.terminal_reason = "handoff_erro"
        try:
            await encaminhar_para_vendedor(
                contact_id=contact_id,
                state=state,
                terminal_reason="handoff_erro",
                handoff_reason=f"updater LLM falhou: {type(e).__name__}: {e}",
            )
        except Exception as e2:
            log.error("terminal_dispatch_failed", err=str(e2))
        try:
            await session_repo.save(contact_id, state)
        except Exception as e3:
            log.error("state_save_failed", err=str(e3))
        return

    new_state = merge_into_state(state, update)

    next_q = plan_next_question(state=new_state, update=update, history=history)
    log.info(
        "next_question_planned",
        field=next_q.field,
        intent=next_q.intent,
        skip=next_q.skip_funnel_reason,
    )

    dispatch = await _dispatch_deterministic(
        state=new_state,
        update_intent=update.intent,
        update_topics=list(update.topics or []),
        update_preferencia_dia=(update.preferencia_horario.dia if update.preferencia_horario else None),
        update_preferencia_periodo=(update.preferencia_horario.periodo if update.preferencia_horario else None),
        update_preferencia_hora=(update.preferencia_horario.hora if update.preferencia_horario else None),
        last_message=last_message,
    )

    # GUARD: strip premature qualificado_agendado terminal
    if (
        update.terminal_reason == "qualificado_agendado"
        and not update.chosen_slot_iso
        and not new_state.appointment
    ):
        log.warning(
            "stripped_premature_terminal",
            contact_id=contact_id,
            reason="qualificado_agendado sem booking real",
        )
        update.terminal_reason = None

    # Booking: lead_pick (chosen_slot_iso) ou auto_match (auto_book_slot_iso)
    slot_to_book = update.chosen_slot_iso or dispatch.get("auto_book_slot_iso")
    booking_source = "lead_pick" if update.chosen_slot_iso else "auto_match"
    booking_result: dict | None = None
    if slot_to_book:
        try:
            modelo = new_state.collected.veiculo_interesse
            booked = await book_appointment(
                contact_id=contact_id,
                slot_iso=slot_to_book,
                lead_name=new_state.collected.nome,
                modelo=modelo,
            )
            new_state.appointment = {
                "slot_iso": slot_to_book,
                "id": (booked.get("appointment") or {}).get("id") or booked.get("id"),
                "modelo": modelo,
            }
            booking_result = {
                "ok": True,
                "slot": slot_to_book,
                "source": booking_source,
            }
            log.info(
                "auto_book_success" if booking_source == "auto_match" else "lead_pick_book_success",
                contact_id=contact_id, slot=slot_to_book,
            )
            if not update.terminal_reason:
                update.terminal_reason = "qualificado_agendado"
        except Exception as e:
            log.error("book_appointment_failed", err=str(e), source=booking_source)
            booking_result = {"ok": False, "error": str(e), "source": booking_source}
            if not update.terminal_reason:
                update.terminal_reason = "handoff_erro"
                update.handoff_reason = f"conflito ao bookar slot: {e}"

    # SDR Team Agno (Patricia leader + EstoqueExpert member)
    try:
        team_out = await run_team_turn(
            state=new_state,
            update=update,
            next_question=next_q,
            history=history,
            last_message=last_message,
            tom_turno=dispatch.get("tom_turno", "descontraido"),
            acknowledge_hint=dispatch.get("acknowledge_hint"),
            slots=dispatch.get("slots"),
            vehicle_in_focus=dispatch.get("vehicle_in_focus"),
            booking_result=booking_result,
            faq_yaml=dispatch.get("faq_yaml"),
        )
    except Exception as e:
        log.error("team_failed_terminal", contact_id=contact_id, err=str(e))
        new_state.stage = "fechado"
        new_state.terminal_reason = "handoff_erro"
        try:
            await encaminhar_para_vendedor(
                contact_id=contact_id,
                state=new_state,
                terminal_reason="handoff_erro",
                handoff_reason=f"Team Agno falhou: {type(e).__name__}: {e}",
            )
        except Exception as e2:
            log.error("terminal_dispatch_failed", err=str(e2))
        try:
            await session_repo.save(contact_id, new_state)
        except Exception as e3:
            log.error("state_save_failed", err=str(e3))
        return

    bubbles: list[str] = list(team_out["bubbles"])
    rendered_ids: list[str] = list(team_out["rendered_vehicle_ids"])
    inv_decision = team_out.get("inventory_decision")

    # Guard determinístico: 1 veículo apresentado -> última bolha singular
    if len(rendered_ids) == 1 and bubbles:
        bubbles[-1] = _enforce_singular_question(bubbles[-1])

    # Atualiza vehicles_shown / last_card_external_id com cards renderizados
    for eid in rendered_ids:
        if eid not in new_state.vehicles_shown:
            new_state.vehicles_shown.append(eid)
    if len(rendered_ids) == 1:
        new_state.last_card_external_id = rendered_ids[0]

    # Fotos (quando EstoqueExpert pediu enviar_fotos_de)
    photo_urls: list[str] = []
    if inv_decision and getattr(inv_decision, "enviar_fotos_de", None):
        photo_urls, vehicle = await _resolve_photos_to_send(
            enviar_fotos_de=inv_decision.enviar_fotos_de, state=new_state
        )
        if vehicle and vehicle.get("external_id"):
            vid = str(vehicle["external_id"])
            if vid not in new_state.vehicles_shown:
                new_state.vehicles_shown.append(vid)
            new_state.last_card_external_id = vid

    # Send sob shield: imune a preempção
    await asyncio.shield(
        _send_bubbles(
            contact_id=contact_id,
            conversation_id=conversation_id,
            bubbles=bubbles,
            photos=photo_urls,
        )
    )

    if update.terminal_reason:
        new_state.stage = "fechado"
        new_state.terminal_reason = update.terminal_reason
        log.info("turn_terminal", contact_id=contact_id, reason=update.terminal_reason)
        if update.terminal_reason in TERMINAL_REASONS:
            try:
                await encaminhar_para_vendedor(
                    contact_id=contact_id,
                    state=new_state,
                    terminal_reason=update.terminal_reason,
                    handoff_reason=update.handoff_reason,
                )
            except Exception as e:
                log.error("terminal_dispatch_failed", err=str(e))

    # Anti-repetição rolling window
    if next_q.field and next_q.intent == "funil":
        push_asked_field(new_state, next_q.field)

    try:
        await session_repo.save(contact_id, new_state)
    except Exception as e:
        log.error("state_save_failed", err=str(e))

    TURNS_TOTAL.labels(stage=update.stage, intent=update.intent).inc()


async def process_turn(contact_id: str, last_message: str) -> asyncio.Task:
    """Entrypoint: cancela turno anterior do mesmo contato e dispara um novo."""
    cancel_existing(contact_id)
    task = asyncio.create_task(_run_turn(contact_id, last_message), name=f"turn:{contact_id}")
    _set_task(contact_id, task)
    return task


def get_active_task(contact_id: str) -> asyncio.Task | None:
    return _TASKS.get(contact_id)
