"""Resolve veículo alvo + payload de fotos pro orquestrador.

Estratégia de seleção do alvo (em ordem):
  1) Match textual da última mensagem contra modelos do estoque (substring unidecode/lower)
  2) state.last_card_external_id (card único recém-renderizado)
  3) Vehicle focus definido no state.collected.veiculo_interesse
  4) state.veiculo_origem.texto (match no inventory)
  5) Último external_id em vehicles_shown
"""
from __future__ import annotations

from typing import Any

from zoi_agent.agent.schemas import SessionState
from zoi_agent.logging import get_logger
from zoi_agent.tools.inventory import (
    get_vehicle_details,
    load_inventory,
    norm,
)

log = get_logger(__name__)


def _find_by_keyword(last_message: str, inventory: list[dict]) -> dict | None:
    text = norm(last_message)
    if not text:
        return None
    # Prioriza match em modelo, depois marca, depois título.
    for v in inventory:
        if norm(v.get("modelo")) and norm(v.get("modelo")) in text:
            return v
    for v in inventory:
        if norm(v.get("marca")) and norm(v.get("marca")) in text:
            return v
    for v in inventory:
        if norm(v.get("titulo")) and norm(v.get("titulo")) in text:
            return v
    return None


def _find_by_external_id(external_id: str, inventory: list[dict]) -> dict | None:
    for v in inventory:
        if v.get("external_id") == external_id:
            return v
    return None


async def pick_target_vehicle(
    *, last_message: str, state: SessionState
) -> dict | None:
    inventory = await load_inventory()
    if not inventory:
        return None

    # 1) keyword na mensagem
    v = _find_by_keyword(last_message, inventory)
    if v:
        log.info("photo_target_keyword", external_id=v.get("external_id"))
        return v

    # 2) card único recém-renderizado (sinal mais forte que vehicles_shown)
    if state.last_card_external_id:
        v = _find_by_external_id(state.last_card_external_id, inventory)
        if v:
            log.info("photo_target_last_card", external_id=v.get("external_id"))
            return v

    # 3) veiculo_interesse texto livre (foco definido)
    if state.collected.veiculo_interesse_confirmado and state.collected.veiculo_interesse:
        v = _find_by_keyword(state.collected.veiculo_interesse, inventory)
        if v:
            log.info("photo_target_focus", external_id=v.get("external_id"))
            return v

    # 4) veiculo de origem (lead chegou anchored num modelo)
    if state.veiculo_origem and state.veiculo_origem.texto:
        v = _find_by_keyword(state.veiculo_origem.texto, inventory)
        if v:
            log.info("photo_target_origem", external_id=v.get("external_id"))
            return v

    # 5) último vehicles_shown (fallback fraco)
    if state.vehicles_shown:
        v = _find_by_external_id(state.vehicles_shown[-1], inventory)
        if v:
            log.info("photo_target_last_shown", external_id=v.get("external_id"))
            return v

    log.info("photo_target_not_found", last_message=last_message[:60])
    return None


async def build_photo_payload(
    *, last_message: str, state: SessionState
) -> dict[str, Any]:
    """Retorna dict consumível pelo responder e orchestrator:
      {
        "available": bool,         # houve veículo alvo
        "vehicle": {titulo, external_id, ano, preco, ...} | None,
        "images": [url, ...],      # vazia se <2 imagens
        "single_image_only": bool, # quando alvo tem apenas 1 imagem
        "will_send_count": int,    # 0 ou len(images)
      }
    """
    v = await pick_target_vehicle(last_message=last_message, state=state)
    if not v:
        return {
            "available": False,
            "vehicle": None,
            "images": [],
            "single_image_only": False,
            "will_send_count": 0,
        }
    imgs = v.get("imagens") or []
    single_only = len(imgs) == 1
    send_imgs: list[str] = [] if len(imgs) < 2 else imgs
    return {
        "available": True,
        "vehicle": {
            "external_id": v.get("external_id"),
            "titulo": v.get("titulo"),
            "marca": v.get("marca"),
            "modelo": v.get("modelo"),
            "ano": v.get("ano"),
            "preco": v.get("preco"),
            "cambio": v.get("cambio"),
            "quilometragem": v.get("quilometragem"),
        },
        "images": send_imgs,
        "single_image_only": single_only,
        "will_send_count": len(send_imgs),
    }
