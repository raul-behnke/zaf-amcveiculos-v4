"""Emissão de eventos canônicos (`agent_events`) + tabela de preços da frota.

Envelope CANÔNICO v1 (CONTRATO_EVENTOS_CANONICO.md §2): cada evento carrega
event_id (uuid4, idempotência), schema_version, client, agent, contact_id,
conversation_id, occurred_at (UTC) e payload.

Custo (decisão de frota): calculado NO AGENTE em USD e BRL via `pricing` local
(forma canônica §4: linha por model+kind, price_usd por 1M tokens / por minuto).
Tokens crus seguem no payload p/ o Hub recalcular e reconciliar.

Tudo tolerante a falha: telemetria NUNCA derruba o turno do lead.
"""
from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import select

from zoi_agent.config import settings
from zoi_agent.db.engine import get_session_factory
from zoi_agent.db.models import AgentEvent, Pricing
from zoi_agent.logging import get_logger

log = get_logger(__name__)

SCHEMA_VERSION = 1

# Câmbio e versão de pricing do seed. Versionável via tabela `pricing`.
_USD_BRL_RATE = "5.40"
_PRICING_VERSION = "2026-06-17"

# Preços OpenAI (USD por 1M tokens; audio_minute = USD por minuto). Contrato §4.
# gpt-4o: $2.50/1M in, $10.00/1M out | gpt-4o-mini: $0.15/1M in, $0.60/1M out
# whisper-1: $0.006/min
_PRICING_SEED: list[dict] = [
    {"model": "gpt-4o", "kind": "input", "price_usd": "2.50"},
    {"model": "gpt-4o", "kind": "output", "price_usd": "10.00"},
    {"model": "gpt-4o-mini", "kind": "input", "price_usd": "0.15"},
    {"model": "gpt-4o-mini", "kind": "output", "price_usd": "0.60"},
    {"model": "whisper-1", "kind": "audio_minute", "price_usd": "0.006"},
]

# cache: {(model, kind): Pricing}
_PRICE_CACHE: dict[tuple[str, str], Pricing] = {}
_PRICE_CACHE_TS: float = 0.0
_PRICE_TTL = 300.0


@dataclass
class CostResult:
    cost_usd: Decimal
    cost_brl: Decimal
    usd_brl_rate: Decimal | None
    pricing_version: str | None


async def seed_pricing() -> None:
    """Insere preços-base (por model+kind) se ausentes. Idempotente. Startup."""
    factory = get_session_factory()
    async with factory() as s:
        async with s.begin():
            existing = {
                (row[0], row[1])
                for row in (await s.execute(select(Pricing.model, Pricing.kind))).all()
            }
            for p in _PRICING_SEED:
                if (p["model"], p["kind"]) in existing:
                    continue
                s.add(
                    Pricing(
                        model=p["model"],
                        kind=p["kind"],
                        effective_from=date(2024, 1, 1),
                        price_usd=Decimal(p["price_usd"]),
                        usd_brl_rate=Decimal(_USD_BRL_RATE),
                        pricing_version=_PRICING_VERSION,
                    )
                )
    _PRICE_CACHE.clear()


async def _load_pricing() -> dict[tuple[str, str], Pricing]:
    global _PRICE_CACHE_TS
    now = time.monotonic()
    if not _PRICE_CACHE or (now - _PRICE_CACHE_TS) > _PRICE_TTL:
        factory = get_session_factory()
        async with factory() as s:
            rows = (await s.execute(select(Pricing))).scalars().all()
        latest: dict[tuple[str, str], Pricing] = {}
        for r in rows:
            key = (r.model, r.kind)
            cur = latest.get(key)
            if cur is None or r.effective_from >= cur.effective_from:
                latest[key] = r
        _PRICE_CACHE.clear()
        _PRICE_CACHE.update(latest)
        _PRICE_CACHE_TS = now
    return _PRICE_CACHE


async def compute_cost(
    *,
    model: str,
    tokens_input: int = 0,
    tokens_output: int = 0,
    reasoning_tokens: int | None = None,
    audio_seconds: float | None = None,
) -> CostResult:
    """Fórmula canônica (contrato §4) — idêntica à do Hub p/ reconciliação.

    LLM:  cost_usd = in/1e6*price(input) + out/1e6*price(output)
                   + reasoning/1e6*price(reasoning)
    Whisper: cost_usd = ceil(audio_seconds/60) * price(audio_minute)
    cost_brl = cost_usd * usd_brl_rate
    """
    pricing = await _load_pricing()
    cost_usd = Decimal(0)
    rate: Decimal | None = None
    version: str | None = None

    def _row(kind: str) -> Pricing | None:
        return pricing.get((model, kind))

    if audio_seconds is not None:
        row = _row("audio_minute")
        if row is not None:
            minutes = Decimal(math.ceil(audio_seconds / 60))
            cost_usd += minutes * row.price_usd
            rate, version = row.usd_brl_rate, row.pricing_version
    else:
        for kind, toks in (
            ("input", tokens_input),
            ("output", tokens_output),
            ("reasoning", reasoning_tokens or 0),
        ):
            if not toks:
                continue
            row = _row(kind)
            if row is None:
                continue
            cost_usd += (Decimal(toks) / Decimal(1_000_000)) * row.price_usd
            if rate is None:
                rate, version = row.usd_brl_rate, row.pricing_version

    if rate is None:
        log.warning("pricing_missing", model=model)
        return CostResult(Decimal(0), Decimal(0), None, None)

    cost_usd = cost_usd.quantize(Decimal("0.000001"))
    cost_brl = (cost_usd * rate).quantize(Decimal("0.000001"))
    return CostResult(cost_usd, cost_brl, rate, version)


async def emit_event(
    *,
    event_type: str,
    contact_id: str,
    conversation_id: str | None = None,
    component: str | None = None,
    model: str | None = None,
    tokens_input: int | None = None,
    tokens_output: int | None = None,
    tokens_total: int | None = None,
    reasoning_tokens: int | None = None,
    cost: CostResult | None = None,
    payload: dict | None = None,
    occurred_at: datetime | None = None,
) -> None:
    """Grava um evento no envelope canônico v1. Tolerante a falha (loga e segue)."""
    if not settings.telemetry_events_enabled:
        return
    try:
        ev = AgentEvent(
            event_id=str(uuid.uuid4()),
            schema_version=SCHEMA_VERSION,
            event_type=event_type,
            client=settings.client,
            agent=settings.agent_name,
            contact_id=contact_id,
            conversation_id=conversation_id,
            occurred_at=occurred_at or datetime.now(timezone.utc),
            payload=payload or {},
            component=component,
            model=model,
            tokens_input=tokens_input,
            tokens_output=tokens_output,
            tokens_total=tokens_total,
            reasoning_tokens=reasoning_tokens,
            cost_usd=cost.cost_usd if cost else None,
            cost_brl=cost.cost_brl if cost else None,
            usd_brl_rate=cost.usd_brl_rate if cost else None,
            pricing_version=cost.pricing_version if cost else None,
        )
        factory = get_session_factory()
        async with factory() as s:
            async with s.begin():
                s.add(ev)
    except Exception as e:
        log.error("emit_event_failed", event_type=event_type, err=str(e))
