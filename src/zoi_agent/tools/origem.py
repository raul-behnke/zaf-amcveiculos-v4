"""PLAN §5: buscar_veiculo_interesse_origem.

Lê state.veiculo_origem.texto e dispara search_inventory pra trazer matches
do estoque. Resolve a frustração inicial do lead: 1ª resposta entrega VALOR
(modelos reais) antes de qualificar dados pessoais.

Gate de disparo: state.vehicles_shown vazio (ninguém viu nada ainda) E
state.veiculo_origem existe. Semântica simplificada: se já mostramos qualquer
veículo, não re-apresentamos a origem proativamente.
"""
from __future__ import annotations

from zoi_agent.agent.schemas import SessionState
from zoi_agent.logging import get_logger
from zoi_agent.tools.inventory import search_inventory

log = get_logger(__name__)


async def buscar_veiculo_interesse_origem(state: SessionState) -> dict | None:
    """Retorna {texto_origem, matches: SearchResult.model_dump()} se origem
    existe e ainda não foi apresentada. None caso contrário."""
    if state.vehicles_shown:
        log.info("origem_skip_already_shown", shown=len(state.vehicles_shown))
        return None
    if not state.veiculo_origem or not state.veiculo_origem.texto:
        return None

    texto = state.veiculo_origem.texto
    try:
        result = await search_inventory(texto)
    except Exception as e:
        log.error("origem_search_failed", texto=texto, err=str(e))
        return None

    log.info(
        "origem_matches_found",
        texto=texto,
        exatos=len(result.exatos),
        parecidos=len(result.parecidos),
    )
    return {"texto_origem": texto, "matches": result.model_dump()}


def collect_external_ids(payload: dict) -> list[str]:
    """Extrai external_ids dos matches retornados pelo buscar_veiculo_interesse_origem.
    Usado pelo orchestrator pra atualizar vehicles_shown."""
    if not payload:
        return []
    matches = payload.get("matches") or {}
    ids: list[str] = []
    for v in matches.get("exatos") or []:
        if v.get("external_id"):
            ids.append(v["external_id"])
    for p in matches.get("parecidos") or []:
        v = p.get("vehicle") or {}
        if v.get("external_id"):
            ids.append(v["external_id"])
    return ids
