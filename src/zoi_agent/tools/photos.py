"""Resolve veículo alvo + payload de fotos pro orquestrador.

Estratégia de seleção do alvo (em ordem):
  1) Match textual da última mensagem contra modelos do estoque (substring unidecode/lower)
  2) state.last_card_external_id (card único recém-renderizado)
  3) Vehicle focus definido no state.collected.veiculo_interesse
  4) state.veiculo_origem.texto (match no inventory)
  5) Último external_id em vehicles_shown
"""
from __future__ import annotations

from difflib import SequenceMatcher
from typing import Any

from zoi_agent.agent.schemas import SessionState
from zoi_agent.logging import get_logger
from zoi_agent.tools.inventory import (
    load_inventory,
    norm,
)

_FUZZY_THRESHOLD = 0.75


def _fuzzy_modelo_in_text(modelo_norm: str, text: str) -> bool:
    """True se algum token do texto bate fuzzy com o modelo (cobre typos
    tipo 'Crize' -> 'cruze'). Tokens com <4 chars são ignorados pra evitar
    falsos positivos (ex: 'do', 'um')."""
    if not modelo_norm or len(modelo_norm) < 4:
        return False
    for tok in text.split():
        if len(tok) < 4:
            continue
        if SequenceMatcher(None, modelo_norm, tok).ratio() >= _FUZZY_THRESHOLD:
            return True
    return False

log = get_logger(__name__)


_YEAR_RE = __import__("re").compile(r"\b(19[8-9]\d|20[0-3]\d)\b")


def _extract_year(text: str) -> int | None:
    m = _YEAR_RE.search(text or "")
    return int(m.group(0)) if m else None


def _find_by_keyword(
    last_message: str,
    inventory: list[dict],
    *,
    fallback_modelo: str | None = None,
    candidates_subset: list[dict] | None = None,
) -> dict | None:
    """Casa veículo na fala do lead. Considera modelo + marca + título e usa
    ANO como discriminador. Quando lead diz só o ano ("Tem fotos do 2014?"),
    cruza com `fallback_modelo` (foco atual) pra encontrar o veículo exato.
    """
    text = norm(last_message)
    if not text:
        return None
    year = _extract_year(text)

    # Helper: dado modelo_normalizado, retorna o veículo do estoque que casa
    # (com ano se informado, ou o 1º que match).
    def _match_modelo_ano(modelo_norm: str, want_year: int | None) -> dict | None:
        if want_year is not None:
            for v in inventory:
                if norm(v.get("modelo")) == modelo_norm and v.get("ano") == want_year:
                    return v
        # sem ano ou sem match exato: primeiro do modelo
        for v in inventory:
            if norm(v.get("modelo")) == modelo_norm:
                return v
        return None

    # 1) Modelo aparece no texto: cruza com ano se informado.
    for v in inventory:
        m = norm(v.get("modelo"))
        if m and m in text:
            chosen = _match_modelo_ano(m, year)
            if chosen:
                return chosen

    # 1b) Fuzzy match contra subset shown (typos tipo "Crize" -> "Cruze").
    #     Restrito ao que o lead já viu pra evitar match em estoque inteiro.
    if candidates_subset:
        for v in candidates_subset:
            m = norm(v.get("modelo"))
            if m and _fuzzy_modelo_in_text(m, text):
                chosen = _match_modelo_ano(m, year) or v
                log.info(
                    "photo_target_fuzzy_shown",
                    external_id=chosen.get("external_id"),
                    modelo=m,
                )
                return chosen

    # 1c) Fuzzy contra inventário completo (último recurso pré-fallbacks).
    for v in inventory:
        m = norm(v.get("modelo"))
        if m and _fuzzy_modelo_in_text(m, text):
            chosen = _match_modelo_ano(m, year) or v
            log.info(
                "photo_target_fuzzy_inventory",
                external_id=chosen.get("external_id"),
                modelo=m,
            )
            return chosen

    # 2) Só ano no texto + fallback_modelo (foco/contexto) -> exact match.
    if year is not None and fallback_modelo:
        fm = norm(fallback_modelo)
        for v in inventory:
            vm = norm(v.get("modelo"))
            if vm and (vm in fm or fm in vm) and v.get("ano") == year:
                return v

    # 3) Só ano no texto + subset (vehicles_shown) -> match por ano dentro do
    #    que já foi apresentado.
    if year is not None and candidates_subset:
        for v in candidates_subset:
            if v.get("ano") == year:
                return v

    # 4) Marca no texto.
    for v in inventory:
        if norm(v.get("marca")) and norm(v.get("marca")) in text:
            return v

    # 5) Título no texto.
    for v in inventory:
        if norm(v.get("titulo")) and norm(v.get("titulo")) in text:
            return v

    # 6) Só ano sem mais nada -> primeiro veículo com esse ano.
    if year is not None:
        for v in inventory:
            if v.get("ano") == year:
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

    # 1) keyword na mensagem (com ano + fallback_modelo do foco + subset shown)
    shown_subset = [v for v in inventory if v.get("external_id") in (state.vehicles_shown or [])]
    v = _find_by_keyword(
        last_message,
        inventory,
        fallback_modelo=state.collected.veiculo_interesse,
        candidates_subset=shown_subset,
    )
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


def _payload_for_vehicle(v: dict) -> dict[str, Any]:
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


async def build_photo_payload_by_id(
    *, external_id: str, state: SessionState
) -> dict[str, Any]:
    """Pega o veículo pelo external_id confiável (vindo do updater LLM)
    e monta o mesmo payload que build_photo_payload. Sem heurística textual."""
    inventory = await load_inventory()
    if not inventory:
        return {
            "available": False, "vehicle": None, "images": [],
            "single_image_only": False, "will_send_count": 0,
        }
    v = _find_by_external_id(str(external_id), inventory)
    if not v:
        log.warning("photo_target_id_not_found", external_id=external_id)
        return {
            "available": False, "vehicle": None, "images": [],
            "single_image_only": False, "will_send_count": 0,
        }
    log.info("photo_target_by_id", external_id=v.get("external_id"))
    return _payload_for_vehicle(v)


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
