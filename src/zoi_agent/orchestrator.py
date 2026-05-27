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
from zoi_agent.agent.updater import merge_into_state, run_updater
from zoi_agent.config import settings
from zoi_agent.db import sessions as session_repo
from zoi_agent.ghl import conversations as ghl_conv
from zoi_agent.logging import get_logger
from zoi_agent.metrics import TURNS_TOTAL
from zoi_agent.tools.faq import get_faq_raw
from zoi_agent.tools.inventory import search_inventory
from zoi_agent.tools.photos import build_photo_payload

log = get_logger(__name__)


# --- Task table (preempção por contactId) ---------------------------------

_TASKS: dict[str, asyncio.Task] = {}


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
    if update_intent_sec == "duvida_operacional":
        try:
            out["faq_yaml"] = await get_faq_raw()
        except Exception as e:
            log.error("faq_fetch_failed", err=str(e))
            out["faq_yaml"] = ""
    if update_intent_sec == "ver_outros_carros":
        try:
            res = await search_inventory(last_message)
            out["search_results"] = res.model_dump()
        except Exception as e:
            log.error("search_inventory_failed", err=str(e))
            out["search_results"] = {"error": str(e)}
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

    update = await run_updater(history=history, state=state, last_message=last_message)
    new_state = merge_into_state(state, update)

    tools = await _dispatch_tools(
        update_intent_sec=update.intent_secundario,
        last_message=last_message,
        state=new_state,
    )

    bubbles = await run_responder(
        state=new_state,
        update=update,
        history=history,
        last_message=last_message,
        tool_outputs=tools,
    )

    # Fotos a enviar (paralelo) + bolhas
    photo_urls: list[str] = []
    photos_payload = tools.get("photos") or {}
    if photos_payload.get("images"):
        photo_urls = list(photos_payload["images"])
        # Marca vehicle como mostrado
        vid = (photos_payload.get("vehicle") or {}).get("external_id")
        if vid and vid not in new_state.vehicles_shown:
            new_state.vehicles_shown.append(vid)

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
