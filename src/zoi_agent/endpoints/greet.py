"""POST /sessions/{contactId}/greet — síncrono, idempotente."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Path

from zoi_agent.agent.schemas import SessionState, VeiculoOrigem
from zoi_agent.config import settings
from zoi_agent.db import sessions as session_repo
from zoi_agent.db.events import emit_event
from zoi_agent.ghl import contacts as ghl_contacts
from zoi_agent.ghl import conversations as ghl_conv
from zoi_agent.logging import get_logger
from zoi_agent.security import require_secret

router = APIRouter()
log = get_logger(__name__)


GREET_SEM_VEICULO = (
    "Olá! 👋 Bem-vindo à AMC Veículos.\n"
    "Como posso te ajudar hoje? Está procurando algum carro específico?"
)

GREET_COM_VEICULO_TMPL = (
    "Olá! 👋 Bem-vindo à AMC Veículos.\n"
    "Vi que você demonstrou interesse no {veiculo} 🚗\n"
    "Posso te passar mais informações sobre ele?"
)


def _build_message(veiculo: str | None) -> str:
    if veiculo and veiculo.strip():
        return GREET_COM_VEICULO_TMPL.format(veiculo=veiculo.strip())
    return GREET_SEM_VEICULO


@router.post("/sessions/{contact_id}/greet", dependencies=[Depends(require_secret)])
async def greet(contact_id: str = Path(..., min_length=1)) -> dict:
    log.info("greet_start", contact_id=contact_id)

    # 1) state local
    state = await session_repo.load_or_new(contact_id)

    # 2) busca contato (precisamos pra ler ambos os custom fields)
    try:
        contact_resp = await ghl_contacts.get_contact(contact_id)
    except Exception as e:
        log.error("greet_contact_fetch_failed", err=str(e))
        raise HTTPException(status_code=502, detail="ghl contact fetch failed") from e
    contact_body = contact_resp.get("contact", contact_resp)

    saud_value = ghl_contacts.read_custom_field_value(
        contact_resp, settings.ghl_field_saudacao_prevendas
    )
    saud_sim = (saud_value or "").strip().upper() == "SIM"

    # 3) idempotência
    if state.greeted or saud_sim:
        log.info(
            "greet_skipped_idempotent",
            contact_id=contact_id,
            state_greeted=state.greeted,
            saud_sim=saud_sim,
        )
        return {"status": "ok", "skipped": True, "reason": "already_greeted"}

    veiculo = ghl_contacts.read_custom_field_value(
        contact_resp, settings.ghl_field_veiculo_interesse
    )
    veiculo_str = (veiculo or "").strip() or None

    msg = _build_message(veiculo_str)

    # 4) envia (síncrono — só 200 após send OK)
    try:
        await ghl_conv.send_message(
            contact_id=contact_id, message=msg, message_type="SMS"
        )
    except Exception as e:
        log.error("greet_send_failed", err=str(e))
        raise HTTPException(status_code=502, detail="ghl send failed") from e

    # 5) marca custom field SAUDAÇÃO=SIM
    try:
        await ghl_contacts.update_custom_field(
            contact_id, settings.ghl_field_saudacao_prevendas, "SIM"
        )
    except Exception as e:
        log.error("greet_mark_failed", err=str(e))
        # send já foi: não levanta, só registra. Próxima chamada vai re-marcar se preciso.

    # 6) persiste state
    new_state = SessionState(
        stage="abertura",
        greeted=True,
        veiculo_origem=VeiculoOrigem(texto=veiculo_str) if veiculo_str else None,
    )
    try:
        await session_repo.save(contact_id, new_state)
    except Exception as e:
        log.error("greet_state_save_failed", err=str(e))

    await emit_event(
        event_type="CONVERSATION_STARTED",
        contact_id=contact_id,
        payload={"veiculo_origem": veiculo_str, "com_veiculo": bool(veiculo_str)},
    )

    log.info(
        "greet_sent",
        contact_id=contact_id,
        com_veiculo=bool(veiculo_str),
        veiculo=veiculo_str,
    )
    return {
        "status": "ok",
        "skipped": False,
        "com_veiculo": bool(veiculo_str),
        "veiculo": veiculo_str,
    }
