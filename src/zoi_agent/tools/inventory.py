"""Inventory tool: load JSON stock from GHL custom value, filter, search."""
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field
from unidecode import unidecode

from zoi_agent.cache import TTLCache
from zoi_agent.config import settings
from zoi_agent.ghl.custom_values import extract_value, get_custom_value
from zoi_agent.llm import parse_structured
from zoi_agent.logging import get_logger

log = get_logger(__name__)


# --- Schemas ---------------------------------------------------------------


class InventoryFilters(BaseModel):
    """Filtros estruturados extraídos de query natural."""

    marca: list[str] | None = Field(default=None, description="Marcas, ex: ['Renault','Fiat']")
    modelo: list[str] | None = Field(default=None, description="Modelos, ex: ['Logan','Duster']")
    carroceria: list[str] | None = Field(default=None, description="Carrocerias: SUV, Sedan, Hatch, Picape, etc.")
    cambio: Literal["Manual", "Mecânico", "Automático", "CVT", "Automatizado"] | None = None
    combustivel: list[str] | None = Field(default=None, description="Flex, Gasolina, Diesel, Elétrico, Híbrido")
    cor: list[str] | None = None
    ano_min: int | None = None
    ano_max: int | None = None
    preco_min: float | None = None
    preco_max: float | None = None
    km_max: int | None = None
    portas: int | None = None
    opcionais: list[str] | None = Field(default=None, description="Opcionais desejados (substring match)")
    keywords: list[str] | None = Field(default=None, description="Palavras-chave livres para casar em título/descrição")
    sort_by: Literal["preco_asc", "preco_desc", "ano_desc", "km_asc", None] | None = None
    limit: int = Field(default=10)


class SimilarVehicle(BaseModel):
    external_id: str
    motivo: str


class SimilarSelection(BaseModel):
    parecidos: list[SimilarVehicle]


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


class SearchResult(BaseModel):
    exatos: list[VehicleSummary]
    parecidos: list[dict]  # {"vehicle": VehicleSummary, "motivo": str}
    total: int


# --- Load + cache ----------------------------------------------------------


def _normalize_vehicle(v: dict) -> dict:
    """Normaliza schema da Custom Value do GHL (AMC-Stock pt-BR) pro shape
    interno usado por apply_filters / summarize / templates.

    Schema de entrada (real): id, titulo, marca, modelo, versao, categoria,
    preco, ano_modelo, ano_fabricacao, quilometragem, combustivel, cambio,
    cor, portas, acessorios, imagem_principal, imagens, descricao_resumida.
    """
    out = dict(v)
    # id (int) -> external_id (str) usado como chave em todos os caminhos
    if "external_id" not in out and v.get("id") is not None:
        out["external_id"] = str(v["id"])
    # ano_modelo -> ano (apply_filters/summarize esperam ano)
    if "ano" not in out:
        out["ano"] = v.get("ano_modelo") or v.get("ano_fabricacao")
    # acessorios -> opcionais (apply_filters + render)
    if "opcionais" not in out:
        out["opcionais"] = v.get("acessorios") or []
    # categoria -> carroceria (apply_filters checa carroceria/tipo_veiculo)
    if "carroceria" not in out and v.get("categoria"):
        out["carroceria"] = v["categoria"]
    # descricao_resumida -> descricao (keyword blob)
    if "descricao" not in out:
        out["descricao"] = v.get("descricao_resumida") or v.get("texto_busca_ia") or ""
    # Garante que imagens contenha imagem_principal como primeira
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
        # "veiculos" é o schema real do AMC-Stock (pt-BR); demais são fallbacks.
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
    # Normaliza schema heterogêneo (id->external_id, ano_modelo->ano, etc.)
    items = [_normalize_vehicle(x) for x in items if isinstance(x, dict)]
    # Filtra inativos se status presente
    items = [x for x in items if (x.get("status") or "ATIVO").upper() == "ATIVO"]
    log.info("inventory_loaded", n=len(items))
    return items


_inventory_cache: TTLCache[list[dict]] = TTLCache(
    ttl_seconds=settings.stock_cache_ttl_seconds, loader=_fetch_inventory
)


async def load_inventory() -> list[dict]:
    return await _inventory_cache.get()


def invalidate_inventory_cache() -> None:
    _inventory_cache.invalidate()


# --- Normalização ----------------------------------------------------------


def norm(s: str | None) -> str:
    if not s:
        return ""
    return unidecode(str(s)).lower().strip()


def _norm_list(items: list[str] | None) -> list[str]:
    return [norm(x) for x in items] if items else []


# --- apply_filters ---------------------------------------------------------


def _vehicle_text_blob(v: dict) -> str:
    parts = [
        v.get("titulo"),
        v.get("marca"),
        v.get("modelo"),
        v.get("versao"),
        v.get("carroceria"),
        v.get("tipo_veiculo"),
        v.get("categoria"),
        v.get("cambio"),
        v.get("combustivel"),
        v.get("cor"),
        v.get("descricao"),
    ]
    parts.extend(v.get("opcionais") or [])
    return norm(" ".join(str(p) for p in parts if p))


def apply_filters(inventory: list[dict], f: InventoryFilters) -> list[dict]:
    """Strict filter: unidecode + lower + substring. Sem fuzzy."""
    if not inventory:
        return []

    marcas = _norm_list(f.marca)
    modelos = _norm_list(f.modelo)
    carrocerias = _norm_list(f.carroceria)
    cambio_norm = norm(f.cambio) if f.cambio else None
    combustiveis = _norm_list(f.combustivel)
    cores = _norm_list(f.cor)
    opcionais = _norm_list(f.opcionais)
    keywords = _norm_list(f.keywords)

    out: list[dict] = []
    for v in inventory:
        if marcas and not any(m in norm(v.get("marca")) for m in marcas):
            continue
        if modelos and not any(m in norm(v.get("modelo")) for m in modelos):
            continue
        if carrocerias:
            cb = norm(v.get("carroceria"))
            tv = norm(v.get("tipo_veiculo"))
            if not any(c in cb or c in tv for c in carrocerias):
                continue
        if cambio_norm:
            cv = norm(v.get("cambio"))
            if cambio_norm == "automatico":
                # "Mecânico" ≠ automático; aceita CVT, Automatizado, Automático
                if not any(t in cv for t in ("automat", "cvt")):
                    continue
            elif cambio_norm in ("manual", "mecanico"):
                if not any(t in cv for t in ("manual", "mecanic")):
                    continue
            else:
                if cambio_norm not in cv:
                    continue
        if combustiveis and not any(c in norm(v.get("combustivel")) for c in combustiveis):
            continue
        if cores and not any(c in norm(v.get("cor")) for c in cores):
            continue
        if f.ano_min is not None and (v.get("ano") or 0) < f.ano_min:
            continue
        if f.ano_max is not None and (v.get("ano") or 0) > f.ano_max:
            continue
        if f.preco_min is not None and (v.get("preco") or 0) < f.preco_min:
            continue
        if f.preco_max is not None and (v.get("preco") or 0) > f.preco_max:
            continue
        if f.km_max is not None and (v.get("quilometragem") or 0) > f.km_max:
            continue
        if f.portas is not None and (v.get("portas") or 0) != f.portas:
            continue
        if opcionais:
            opc_norm = [norm(o) for o in (v.get("opcionais") or [])]
            if not all(any(want in have for have in opc_norm) for want in opcionais):
                continue
        if keywords:
            blob = _vehicle_text_blob(v)
            if not all(k in blob for k in keywords):
                continue
        out.append(v)

    if f.sort_by == "preco_asc":
        out.sort(key=lambda x: x.get("preco") or 0)
    elif f.sort_by == "preco_desc":
        out.sort(key=lambda x: x.get("preco") or 0, reverse=True)
    elif f.sort_by == "ano_desc":
        out.sort(key=lambda x: x.get("ano") or 0, reverse=True)
    elif f.sort_by == "km_asc":
        out.sort(key=lambda x: x.get("quilometragem") or 0)

    return out


# --- LLM filters extraction -----------------------------------------------


_EXTRACT_FILTERS_SYSTEM = """\
Você extrai filtros estruturados a partir de uma busca de cliente por veículo usado.

REGRAS:
- Use somente o que o cliente disse. Não invente.
- "SUV" → carroceria=["SUV"]. "sedan" → carroceria=["Sedan"]. "hatch" → carroceria=["Hatch"].
  "picape"/"caminhonete" → carroceria=["Picape"].
- "automático", "automática", "câmbio auto" → cambio="Automático".
  "manual", "mecânico" → cambio="Manual".
- "até 80 mil", "até 80k", "no máximo 80000" → preco_max=80000.
- "até 50 mil km", "máx 50000 km" → km_max=50000.
- "novo", "ano 2023+" → ano_min=2023.
- Marca/modelo só se citados explicitamente.
- keywords: palavras que devem aparecer em título/descrição (use com parcimônia).
- limit: 10 se não dito.
- sort_by: deixe null a menos que o cliente peça ordenação ("mais barato" → preco_asc).
"""


async def extract_filters(query: str) -> InventoryFilters:
    return await parse_structured(
        model=settings.openai_model_inventory_extractor,
        schema=InventoryFilters,
        system=_EXTRACT_FILTERS_SYSTEM,
        user=query,
        component="inventory.extract_filters",
    )


# --- LLM similar selection ------------------------------------------------


_SELECT_SIMILAR_SYSTEM = """\
Você seleciona veículos PARECIDOS quando não há resultados exatos para a busca do cliente.

REGRAS:
- Receba o pedido original do cliente e uma lista de veículos do estoque (JSON resumido).
- SEMPRE retorne até `limit` veículos se houver inventário disponível — o lead
  precisa ver opções, não receber resposta vazia. Mesmo sem casamento perfeito,
  ranqueie os mais próximos por:
  1) mesmo segmento/carroceria (sedan↔sedan, SUV↔SUV, etc.),
  2) faixa de preço próxima,
  3) ano e km semelhantes,
  4) mesma marca (último critério).
- Para cada um, escreva um `motivo` curto e honesto em pt-BR (1 frase),
  posicionando como ALTERNATIVA real ao que o lead pediu (ex.: "não temos Sentra,
  mas este Logan é sedan econômico na mesma faixa").
- Só retorne lista vazia se a lista de candidatos vier vazia.
- Retorne external_id exatamente como veio no input.
"""


async def select_similar(
    query: str, candidatos: list[dict], limit: int
) -> list[SimilarVehicle]:
    if not candidatos:
        return []
    resumo = [
        {
            "external_id": v["external_id"],
            "titulo": v.get("titulo"),
            "marca": v.get("marca"),
            "modelo": v.get("modelo"),
            "ano": v.get("ano"),
            "preco": v.get("preco"),
            "carroceria": v.get("carroceria"),
            "cambio": v.get("cambio"),
            "combustivel": v.get("combustivel"),
            "quilometragem": v.get("quilometragem"),
        }
        for v in candidatos
    ]
    user = (
        f"Pedido do cliente: {query!r}\n"
        f"Limit: {limit}\n"
        f"Veículos disponíveis (JSON):\n{json.dumps(resumo, ensure_ascii=False)}"
    )
    sel = await parse_structured(
        model=settings.openai_model_inventory_extractor,
        schema=SimilarSelection,
        system=_SELECT_SIMILAR_SYSTEM,
        user=user,
        component="inventory.select_similar",
    )
    return sel.parecidos[:limit]


# --- Summary helpers ------------------------------------------------------


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


def _index_by_id(inv: list[dict]) -> dict[str, dict]:
    return {v["external_id"]: v for v in inv if v.get("external_id")}


# --- search_inventory -----------------------------------------------------


async def search_inventory(
    query: str,
    *,
    exclude_ids: list[str] | None = None,
) -> SearchResult:
    """Busca no estoque com filtros extraídos por mini-LLM.

    `exclude_ids` remove veículos já apresentados (state.vehicles_shown) tanto
    dos exatos quanto dos candidatos a parecidos. Evita repetir veículo numa
    nova rodada de "tem mais?" / "outro?".
    """
    inv = await load_inventory()
    filters = await extract_filters(query)
    log.info(
        "inventory_filters",
        query=query,
        filters=filters.model_dump(exclude_none=True),
        exclude=len(exclude_ids or []),
    )

    excl: set[str] = set(exclude_ids or [])
    exatos_raw = [v for v in apply_filters(inv, filters) if v.get("external_id") not in excl]
    limit = filters.limit or settings.inventory_search_limit

    if len(exatos_raw) >= limit:
        return SearchResult(
            exatos=[summarize(v) for v in exatos_raw[:limit]],
            parecidos=[],
            total=len(exatos_raw),
        )

    # Restringe candidatos a parecidos: tudo que não está em exatos nem excl
    exatos_ids = {v["external_id"] for v in exatos_raw}
    candidatos = [
        v for v in inv
        if v.get("external_id") not in exatos_ids
        and v.get("external_id") not in excl
    ]
    remaining = limit - len(exatos_raw)
    similares = await select_similar(query, candidatos, remaining)
    idx = _index_by_id(inv)
    parecidos_out = []
    for s in similares:
        v = idx.get(s.external_id)
        if not v:
            continue
        parecidos_out.append({"vehicle": summarize(v).model_dump(), "motivo": s.motivo})

    return SearchResult(
        exatos=[summarize(v) for v in exatos_raw],
        parecidos=parecidos_out,
        total=len(exatos_raw) + len(parecidos_out),
    )


# --- get_vehicle_details --------------------------------------------------


async def get_vehicle_details(external_id: str) -> dict | None:
    inv = await load_inventory()
    for v in inv:
        if v.get("external_id") == external_id:
            return v
    return None
