"""Terminal action: remove tag + nota §10 consolidada + workflow.

PLAN §5/§10/§12. Aplica-se a TODOS os terminal_reasons:
  - qualificado_agendado
  - qualificado_sem_agenda
  - handoff_solicitado
  - handoff_erro

Tolerante a falhas parciais: cada passo isolado.
"""
from __future__ import annotations

from zoi_agent.agent.schemas import SessionState
from zoi_agent.config import settings
from zoi_agent.db.events import emit_event
from zoi_agent.ghl import contacts as gc
from zoi_agent.ghl import workflows as gw
from zoi_agent.logging import get_logger
from zoi_agent.metrics import HANDOFF_TOTAL, QUALIFICADOS_TOTAL
from zoi_agent.tools.terminal import build_consolidated_note

_QUALIFICADO_REASONS = {"qualificado_agendado", "qualificado_sem_agenda"}

log = get_logger(__name__)


async def encaminhar_para_vendedor(
    *,
    contact_id: str,
    state: SessionState,
    terminal_reason: str,
    handoff_reason: str | None = None,
    observacoes: str | None = None,
) -> dict[str, bool]:
    """Executa: (1) remove tag agente-ia, (2) cria nota §10, (3) add ao workflow.
    Retorna {tag_removed, note_created, workflow_added}. Caller grava terminal no state."""
    result = {"tag_removed": False, "note_created": False, "workflow_added": False}
    tag = settings.ghl_tag_agent_gate
    workflow_id = settings.ghl_handoff_workflow_id

    note_body = build_consolidated_note(
        state=state,
        terminal_reason=terminal_reason,  # type: ignore[arg-type]
        handoff_reason=handoff_reason,
        observacoes=observacoes,
    )

    try:
        await gc.remove_tag(contact_id, [tag])
        result["tag_removed"] = True
    except Exception as e:
        log.error("terminal_remove_tag_failed", contact_id=contact_id, err=str(e))

    try:
        await gc.add_note(contact_id, note_body)
        result["note_created"] = True
    except Exception as e:
        log.error("terminal_note_failed", contact_id=contact_id, err=str(e))

    try:
        await gw.add_to_workflow(contact_id, workflow_id)
        result["workflow_added"] = True
    except Exception as e:
        log.error("terminal_workflow_failed", contact_id=contact_id, err=str(e))

    HANDOFF_TOTAL.labels(reason=terminal_reason).inc()
    if terminal_reason == "qualificado_agendado":
        QUALIFICADOS_TOTAL.labels(com_agenda="sim").inc()
    elif terminal_reason == "qualificado_sem_agenda":
        QUALIFICADOS_TOTAL.labels(com_agenda="nao").inc()
    log.info(
        "terminal_action_done",
        contact_id=contact_id,
        terminal_reason=terminal_reason,
        handoff_reason=(handoff_reason or "")[:80],
        **result,
    )

    # Telemetria: qualificado_* -> CONVERSATION_COMPLETED; handoff_* -> HANDOFF_CREATED.
    is_qualificado = terminal_reason in _QUALIFICADO_REASONS
    await emit_event(
        event_type="CONVERSATION_COMPLETED" if is_qualificado else "HANDOFF_CREATED",
        contact_id=contact_id,
        conversation_id=state.conversation_id,
        payload={
            "terminal_reason": terminal_reason,
            "handoff_reason": handoff_reason,
            "com_agenda": bool(state.appointment) if is_qualificado else None,
            "workflow_added": result["workflow_added"],
        },
    )
    return result
