"""Inventory tool: load JSON stock from GHL custom value, expose lookup helpers.

Versão pós-migração Agno: as funções de busca/filtro/similar removidas
(search_inventory, extract_filters, select_similar, apply_filters,
_EXTRACT_FILTERS_SYSTEM, _SELECT_SIMILAR_SYSTEM, InventoryFilters,
SimilarVehicle, SimilarSelection, SearchResult). EstoqueExpert (Agno Agent)
recebe inventário inteiro no prompt e raciocina sobre ele — não há busca
determinística mais.

Mantém:
  - load_inventory / _fetch_inventory / cache
  - _normalize_vehicle (schema heterogêneo GHL)
  - get_vehicle_details (tool do EstoqueExpert)
  - summarize / VehicleSummary (pra templates de cards)
  - norm helper
"""
from __future__ import annotations

import json

from pydantic import BaseModel
from unidecode import unidecode

from zoi_agent.cache import TTLCache
from zoi_agent.config import settings
from zoi_agent.ghl.custom_values import extract_value, get_custom_value
from zoi_agent.logging import get_logger

log = get_logger(__name__)


# --- Schemas ---------------------------------------------------------------


class VehicleSummary(BaseModel):
    titulo: str
    marca: str
    modelo: str
    ano: int | None
    preco: float | None
    quilometragem: int | None
    cambio: str | None
    cor: str | None
    opcionais: list[str]
    imagem: str | None
    external_id: str


# --- Load + cache ----------------------------------------------------------


def _normalize_vehicle(v: dict) -> dict:
    """Normaliza schema da Custom Value do GHL (AMC-Stock pt-BR ou en novo)
    pro shape interno usado por templates e EstoqueExpert.

    Schema de entrada: id, titulo, marca, modelo, versao, categoria, preco,
    ano_modelo/ano_fabricacao, quilometragem, combustivel, cambio, cor, portas,
    acessorios/opcionais, imagem_principal, imagens, descricao_resumida.
    """
    out = dict(v)
    if "external_id" not in out and v.get("id") is not None:
        out["external_id"] = str(v["id"])
    if "ano" not in out:
        out["ano"] = v.get("ano_modelo") or v.get("ano_fabricacao")
    if "opcionais" not in out:
        out["opcionais"] = v.get("acessorios") or []
    if "carroceria" not in out and v.get("categoria"):
        out["carroceria"] = v["categoria"]
    if "descricao" not in out:
        out["descricao"] = v.get("descricao_resumida") or v.get("texto_busca_ia") or ""
    imgs = list(v.get("imagens") or [])
    principal = v.get("imagem_principal")
    if principal and (not imgs or imgs[0] != principal):
        imgs = [principal] + [i for i in imgs if i != principal]
    out["imagens"] = imgs
    return out


async def _fetch_inventory() -> list[dict]:
    cv = await get_custom_value(settings.ghl_stock_custom_value_id)
    raw = extract_value(cv) or ""
    if not raw:
        log.warning("inventory_empty")
        return []
    data = json.loads(raw)
    if isinstance(data, dict):
        items = (
            data.get("veiculos")
            or data.get("vehicles")
            or data.get("items")
            or data.get("data")
            or []
        )
    elif isinstance(data, list):
        items = data
    else:
        items = []
    items = [_normalize_vehicle(x) for x in items if isinstance(x, dict)]
    # Filtra inativos. GHL aceita variações: ATIVO (pt legado), ACTIVE (novo
    # schema en), AVAILABLE, PUBLICADO. Trata todas como "ativo".
    _active_statuses = {"ATIVO", "ATIVOS", "ACTIVE", "AVAILABLE", "PUBLICADO"}
    items = [
        x for x in items
        if (x.get("status") or "ATIVO").upper() in _active_statuses
    ]
    log.info("inventory_loaded", n=len(items))
    return items


_inventory_cache: TTLCache[list[dict]] = TTLCache(
    ttl_seconds=settings.stock_cache_ttl_seconds, loader=_fetch_inventory
)


async def load_inventory() -> list[dict]:
    return await _inventory_cache.get()


def invalidate_inventory_cache() -> None:
    _inventory_cache.invalidate()


# --- Helpers ---------------------------------------------------------------


def norm(s: str | None) -> str:
    if not s:
        return ""
    return unidecode(str(s)).lower().strip()


def summarize(v: dict) -> VehicleSummary:
    imgs = v.get("imagens") or []
    return VehicleSummary(
        titulo=v.get("titulo", ""),
        marca=v.get("marca", ""),
        modelo=v.get("modelo", ""),
        ano=v.get("ano"),
        preco=v.get("preco"),
        quilometragem=v.get("quilometragem"),
        cambio=v.get("cambio"),
        cor=v.get("cor"),
        opcionais=(v.get("opcionais") or [])[:5],
        imagem=imgs[0] if imgs else None,
        external_id=v.get("external_id", ""),
    )


# --- get_vehicle_details (tool do EstoqueExpert) --------------------------


async def get_vehicle_details(external_id: str) -> dict | None:
    inv = await load_inventory()
    for v in inv:
        if v.get("external_id") == external_id:
            return v
    return None
