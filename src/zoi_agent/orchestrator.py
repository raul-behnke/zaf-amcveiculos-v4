"""Per-contact orchestrator: preempção via Task table + pipeline updater->dispatch->responder->send.

Pipeline por turno:
  1) load session_state (DB)
  2) se terminal_reason setado: ignora (não responde)
  3) fetch histórico GHL (limit 100)
  4) run_updater -> StateUpdate
  5) merge_into_state
  6) tool dispatch baseado em update.intent_secundario
  7) run_responder com tool_outputs -> bubbles[]
  8) shield(send_bubbles)  -- não é cancelado por preempção
  9) save state (e marca terminal se update.terminal_reason)
"""
from __future__ import annotations

import asyncio
import random
from typing import Any

from zoi_agent.agent.responder import run_responder
from zoi_agent.agent.schemas import SessionState
from zoi_agent.agent.templates import build_vehicle_blocks, build_vehicle_blocks_with_ids
from zoi_agent.agent.updater import merge_into_state, run_updater
from zoi_agent.config import settings
from zoi_agent.db import sessions as session_repo
from zoi_agent.ghl import conversations as ghl_conv
from zoi_agent.logging import get_logger
from zoi_agent.metrics import TURNS_TOTAL
from zoi_agent.tools.calendar import book_appointment, propose_slots
from zoi_agent.tools.faq import get_faq_raw
from zoi_agent.tools.handoff import encaminhar_para_vendedor
from zoi_agent.tools.inventory import search_inventory
from zoi_agent.tools.origem import buscar_veiculo_interesse_origem
from zoi_agent.tools.photos import build_photo_payload
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
            # case-insensitive replace preservando capitalização inicial
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
        # remove só se ainda for o mesmo
        cur = _TASKS.get(contact_id)
        if cur is t:
            _TASKS.pop(contact_id, None)

    task.add_done_callback(_cleanup)


def cancel_existing(contact_id: str) -> None:
    prev = _TASKS.get(contact_id)
    if prev and not prev.done():
        log.info("turn_preempted", contact_id=contact_id)
        prev.cancel()


# --- Tool dispatch --------------------------------------------------------


async def _dispatch_tools(
    *,
    update_intent_sec: str | None,
    last_message: str,
    state,
) -> dict[str, Any]:
    out: dict[str, Any] = {}

    # PRIORITÁRIO: ainda não mostramos nenhum veículo + temos origem do CRM
    # -> traz matches do estoque ANTES de qualificar (PLAN §5 + §16 C4).
    # Gate simplificado: usa vehicles_shown vazio como derivação de "lead
    # nunca viu catálogo nosso". Sem flag separado de origem_apresentada.
    if state.veiculo_origem and not state.vehicles_shown:
        try:
            origem = await buscar_veiculo_interesse_origem(state)
            if origem:
                out["origem_matches"] = origem
        except Exception as e:
            log.error("origem_dispatch_failed", err=str(e))

    if update_intent_sec == "duvida_operacional":
        try:
            out["faq_yaml"] = await get_faq_raw()
        except Exception as e:
            log.error("faq_fetch_failed", err=str(e))
            out["faq_yaml"] = ""
    if update_intent_sec == "ver_outros_carros":
        try:
            # Query âncora: combina o foco atual (state.collected.veiculo_interesse)
            # com a fala do lead. Resolve "Oque mais?" (vago) -> mantém categoria.
            # Se ambos existirem, mini-LLM vê os dois e prioriza coerência.
            anchor = state.collected.veiculo_interesse or ""
            query = f"{anchor} {last_message}".strip() if anchor else last_message
            res = await search_inventory(
                query,
                exclude_ids=list(state.vehicles_shown or []),
            )
            out["search_results"] = res.model_dump()
        except Exception as e:
            log.error("search_inventory_failed", err=str(e))
            out["search_results"] = {"error": str(e)}

    # Pre-render templates determinísticos: prepende ao envio antes das bolhas
    # do responder. Reduz token, mantém visual consistente.
    pre_bubbles: list[str] = []
    rendered_ids: list[str] = []
    if out.get("origem_matches"):
        m = (out["origem_matches"] or {}).get("matches") or {}
        exatos = m.get("exatos") or []
        parecidos = [p.get("vehicle") for p in (m.get("parecidos") or []) if p.get("vehicle")]
        bs, ids = build_vehicle_blocks_with_ids(exatos=exatos, parecidos=parecidos)
        pre_bubbles.extend(bs)
        rendered_ids.extend(ids)
    elif out.get("search_results") and not out["search_results"].get("error"):
        sr = out["search_results"]
        exatos = sr.get("exatos") or []
        parecidos = [p.get("vehicle") for p in (sr.get("parecidos") or []) if p.get("vehicle")]
        bs, ids = build_vehicle_blocks_with_ids(exatos=exatos, parecidos=parecidos)
        pre_bubbles.extend(bs)
        rendered_ids.extend(ids)
    if pre_bubbles:
        out["pre_bubbles"] = pre_bubbles
        out["rendered_vehicle_ids"] = rendered_ids
        out["vehicles_presented_count"] = len(rendered_ids)
    # Gate duplo de agendamento (PLAN §11):
    # interesse_agendamento=true AND veiculo_interesse_confirmado=true
    quer_agendar = bool(state.collected.interesse_agendamento)
    focus_ok = bool(state.collected.veiculo_interesse_confirmado)
    if quer_agendar and focus_ok:
        pref = None
        # preferencia vem do update; mas só vemos isso no dispatcher novo (não temos
        # update aqui). Vamos propor sem filtro de pref e o responder ordena.
        try:
            slots = await propose_slots(limit=3)
            out["slots"] = [{"iso": s.iso, "label": s.label_pt()} for s in slots]
        except Exception as e:
            log.error("propose_slots_failed", err=str(e))
            out["slots"] = []
    elif quer_agendar and not focus_ok:
        # C19: lead quer agendar mas sem foco -> agente puxa foco antes
        out["agendamento_gate"] = {"motivo": "veiculo_interesse_confirmado=false"}

    if update_intent_sec == "pedido_foto":
        try:
            out["photos"] = await build_photo_payload(
                last_message=last_message, state=state
            )
        except Exception as e:
            log.error("photos_payload_failed", err=str(e))
            out["photos"] = {
                "available": False, "vehicle": None, "images": [],
                "single_image_only": False, "will_send_count": 0,
            }
    return out


# --- Sender ---------------------------------------------------------------


async def _send_bubble(
    *, contact_id: str, conversation_id: str | None, text: str
) -> None:
    """Envia 1 bolha. Falhas tratadas em ordem superior."""
    await ghl_conv.send_message(
        contact_id=contact_id,
        conversation_id=conversation_id,
        message=text,
    )


async def _send_photo(
    *, contact_id: str, conversation_id: str | None, url: str
) -> None:
    """Envia 1 foto (sem texto). PLAN §5: type SMS, attachments=[url]."""
    await ghl_conv.send_message(
        contact_id=contact_id,
        conversation_id=conversation_id,
        attachments=[url],
    )


async def _send_photos_parallel(
    *, contact_id: str, conversation_id: str | None, urls: list[str]
) -> int:
    """Envia N fotos via asyncio.gather (paralelo). PLAN: return_exceptions=True.
    Retorna quantas foram enviadas com sucesso."""
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
    """1) Fotos paralelo (se houver). 2) Wait 1s. 3) Bolhas sequencial com sleeps 0.6-1.2s.
    Pula bolhas que falham persistentemente (tenacity do client já tenta 3x)."""
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
    """Busca histórico via GHL. Retorna (messages, conversation_id)."""
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
        # GHL costuma retornar {"messages": [...]} dentro do bloco
        msgs = msgs_block.get("messages") if isinstance(msgs_block, dict) else msgs_block
        return (msgs or []), conv_id
    except Exception as e:
        log.warning("history_fetch_failed", err=str(e))
        return [], None


async def _run_turn(contact_id: str, last_message: str) -> None:
    state = await session_repo.load_or_new(contact_id)
    if state.terminal_reason:
        log.info("turn_skipped_terminal", contact_id=contact_id, reason=state.terminal_reason)
        return

    history, conversation_id = await _fetch_history(contact_id)

    try:
        update = await run_updater(history=history, state=state, last_message=last_message)
    except Exception as e:
        # C23/PLAN §13: 3-retry da tenacity já se exauriu -> handoff_erro
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

    tools = await _dispatch_tools(
        update_intent_sec=update.intent_secundario,
        last_message=last_message,
        state=new_state,
    )

    # Booking: lead aceitou slot proposto -> book ANTES do responder
    if update.chosen_slot_iso:
        try:
            modelo = new_state.collected.veiculo_interesse
            booked = await book_appointment(
                contact_id=contact_id,
                slot_iso=update.chosen_slot_iso,
                lead_name=new_state.collected.nome,
                modelo=modelo,
            )
            new_state.appointment = {
                "slot_iso": update.chosen_slot_iso,
                "id": (booked.get("appointment") or {}).get("id") or booked.get("id"),
                "modelo": modelo,
            }
            tools["booking"] = {"ok": True, "slot": update.chosen_slot_iso}
            # Promove terminal_reason se updater não tiver setado
            if not update.terminal_reason:
                update.terminal_reason = "qualificado_agendado"
        except Exception as e:
            log.error("book_appointment_failed", err=str(e))
            tools["booking"] = {"ok": False, "error": str(e)}
            # PLAN §11 conflito de slot -> handoff_erro
            if not update.terminal_reason:
                update.terminal_reason = "handoff_erro"
                update.handoff_reason = f"conflito ao bookar slot: {e}"

    try:
        bubbles = await run_responder(
            state=new_state,
            update=update,
            history=history,
            last_message=last_message,
            tool_outputs=tools,
        )
    except Exception as e:
        # PLAN §13: responder esgotou retries -> handoff_erro
        log.error("responder_failed_terminal", contact_id=contact_id, err=str(e))
        new_state.stage = "fechado"
        new_state.terminal_reason = "handoff_erro"
        try:
            await encaminhar_para_vendedor(
                contact_id=contact_id,
                state=new_state,
                terminal_reason="handoff_erro",
                handoff_reason=f"responder LLM falhou: {type(e).__name__}: {e}",
            )
        except Exception as e2:
            log.error("terminal_dispatch_failed", err=str(e2))
        try:
            await session_repo.save(contact_id, new_state)
        except Exception as e3:
            log.error("state_save_failed", err=str(e3))
        return

    # Prepende bolhas pré-renderizadas (templates Python) antes das do responder.
    # Mantém ordem: [pre_bubbles..., bolha de pergunta do responder].
    pre_bubbles = tools.get("pre_bubbles") or []
    if pre_bubbles:
        bubbles = list(pre_bubbles) + list(bubbles)
        bubbles = bubbles[: settings.responder_max_bubbles + len(pre_bubbles)]

    # Guard determinístico: quando vehicles_presented_count == 1, reescreve a
    # última bolha se o LLM teimou em usar pergunta no plural ("algum desses",
    # "qual desses", etc). Garante contrato independentemente do LLM.
    presented = int(tools.get("vehicles_presented_count") or 0)
    if presented == 1 and bubbles:
        bubbles[-1] = _enforce_singular_question(bubbles[-1])

    # Fotos a enviar (paralelo) + bolhas
    photo_urls: list[str] = []
    photos_payload = tools.get("photos") or {}
    if photos_payload.get("images"):
        photo_urls = list(photos_payload["images"])
        # Marca vehicle como mostrado
        vid = (photos_payload.get("vehicle") or {}).get("external_id")
        if vid and vid not in new_state.vehicles_shown:
            new_state.vehicles_shown.append(vid)

    # vehicles_shown só recebe IDs efetivamente RENDERIZADOS em bolhas (não
    # candidatos da busca). last_card_external_id setado só quando 1 card único.
    rendered_ids = tools.get("rendered_vehicle_ids") or []
    if rendered_ids:
        for eid in rendered_ids:
            if eid not in new_state.vehicles_shown:
                new_state.vehicles_shown.append(eid)
        new_state.last_card_external_id = rendered_ids[0] if len(rendered_ids) == 1 else None
    else:
        # Turno sem render de veículo: limpa o card-único anterior.
        new_state.last_card_external_id = None

    # NOTA: removido flag origem_apresentada. Semântica derivada de
    # state.vehicles_shown não-vazio (a inserção acima já cuida disso).

    # Send phase sob shield: não pode ser cancelado por nova preempção no meio.
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

    try:
        await session_repo.save(contact_id, new_state)
    except Exception as e:
        log.error("state_save_failed", err=str(e))

    TURNS_TOTAL.labels(stage=update.stage, intent=update.intent).inc()


async def process_turn(contact_id: str, last_message: str) -> asyncio.Task:
    """Entrypoint: cancela turno anterior do mesmo contato e dispara um novo.
    Retorna a Task (útil em testes; em prod o webhook não precisa esperar)."""
    cancel_existing(contact_id)
    task = asyncio.create_task(_run_turn(contact_id, last_message), name=f"turn:{contact_id}")
    _set_task(contact_id, task)
    return task


def get_active_task(contact_id: str) -> asyncio.Task | None:
    return _TASKS.get(contact_id)
