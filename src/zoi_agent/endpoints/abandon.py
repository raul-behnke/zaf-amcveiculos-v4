"""POST /sessions/{contactId}/abandon — fecha sessão no DB.

PLAN §8/§10/§17.S14: sem nota, sem workflow. CRM trata o abandono.
Idempotente: se sessão já terminal ou inexistente, 200 sem ação.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Path

from zoi_agent.agent.schemas import SessionState
from zoi_agent.db import sessions as session_repo
from zoi_agent.db.events import emit_event
from zoi_agent.logging import get_logger
from zoi_agent.metrics import ABANDONED_TOTAL
from zoi_agent.security import require_secret

router = APIRouter()
log = get_logger(__name__)


async def _emit_abandoned(contact_id: str, conversation_id: str | None) -> None:
    ABANDONED_TOTAL.inc()
    await emit_event(
        event_type="CONVERSATION_ABANDONED",
        contact_id=contact_id,
        conversation_id=conversation_id,
    )


@router.post("/sessions/{contact_id}/abandon", dependencies=[Depends(require_secret)])
async def abandon(contact_id: str = Path(..., min_length=1)) -> dict:
    state = await session_repo.load(contact_id)
    if state is None:
        log.info("abandon_no_session", contact_id=contact_id)
        # Cria stub fechado pra evitar reabertura
        stub = SessionState(stage="fechado", terminal_reason="abandonado")
        await session_repo.save(contact_id, stub)
        await _emit_abandoned(contact_id, None)
        return {"status": "ok", "created_terminal": True}

    if state.terminal_reason:
        log.info("abandon_already_terminal", contact_id=contact_id, reason=state.terminal_reason)
        return {"status": "ok", "skipped": True, "reason": state.terminal_reason}

    state.stage = "fechado"
    state.terminal_reason = "abandonado"
    await session_repo.save(contact_id, state)
    await _emit_abandoned(contact_id, state.conversation_id)
    log.info("abandon_closed", contact_id=contact_id)
    return {"status": "ok", "skipped": False}
