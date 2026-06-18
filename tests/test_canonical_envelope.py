"""Validação do envelope CANÔNICO v1 (CONTRATO_EVENTOS_CANONICO.md).

Exercita compute_cost + emit_event reais, capturando o AgentEvent construído
(sem Postgres) via fake session factory. Cobre a validação exigida na rodada v2:
event_id único, schema_version=1, client="amc", cost_brl > 0.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from zoi_agent.db import events as ev
from zoi_agent.db.models import Pricing


@pytest.fixture
def captured(monkeypatch):
    """Captura AgentEvent(s) adicionados, sem tocar no banco. Stuba pricing."""
    bucket: list = []

    class _FakeSession:
        def add(self, obj):
            bucket.append(obj)

        def begin(self):
            outer = self

            class _Tx:
                async def __aenter__(self):
                    return outer

                async def __aexit__(self, *a):
                    return False

            return _Tx()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(ev, "get_session_factory", lambda: (lambda: _FakeSession()))

    # pricing canônico em memória (model+kind, USD por 1M / por minuto)
    rows = {
        ("gpt-4o", "input"): Pricing(model="gpt-4o", kind="input", effective_from=date(2024, 1, 1),
                                     price_usd=Decimal("2.50"), usd_brl_rate=Decimal("5.40"),
                                     pricing_version="2026-06-17"),
        ("gpt-4o", "output"): Pricing(model="gpt-4o", kind="output", effective_from=date(2024, 1, 1),
                                      price_usd=Decimal("10.00"), usd_brl_rate=Decimal("5.40"),
                                      pricing_version="2026-06-17"),
        ("whisper-1", "audio_minute"): Pricing(model="whisper-1", kind="audio_minute",
                                               effective_from=date(2024, 1, 1), price_usd=Decimal("0.006"),
                                               usd_brl_rate=Decimal("5.40"), pricing_version="2026-06-17"),
    }

    async def _fake_load():
        return rows

    monkeypatch.setattr(ev, "_load_pricing", _fake_load)
    return bucket


@pytest.mark.asyncio
async def test_llm_cost_usd_and_brl(captured) -> None:
    # gpt-4o: 1000 in, 500 out -> usd = 1000/1e6*2.50 + 500/1e6*10 = 0.0025 + 0.005 = 0.0075
    cost = await ev.compute_cost(model="gpt-4o", tokens_input=1000, tokens_output=500)
    assert cost.cost_usd == Decimal("0.007500")
    assert cost.cost_brl == Decimal("0.040500")  # 0.0075 * 5.40
    assert cost.usd_brl_rate == Decimal("5.40")
    assert cost.pricing_version == "2026-06-17"


@pytest.mark.asyncio
async def test_whisper_cost_ceil_minutes(captured) -> None:
    # 90s -> ceil(90/60)=2 min -> usd = 2*0.006 = 0.012 ; brl = 0.0648
    cost = await ev.compute_cost(model="whisper-1", audio_seconds=90.0)
    assert cost.cost_usd == Decimal("0.012000")
    assert cost.cost_brl == Decimal("0.064800")


@pytest.mark.asyncio
async def test_llm_call_envelope(captured) -> None:
    cost = await ev.compute_cost(model="gpt-4o", tokens_input=2000, tokens_output=700)
    await ev.emit_event(
        event_type="LLM_CALL",
        contact_id="ghl_abc",
        conversation_id="ghl_conv_1",
        component="patricia",
        model="gpt-4o",
        tokens_input=2000,
        tokens_output=700,
        tokens_total=2700,
        cost=cost,
        payload={"component": "patricia", "model": "gpt-4o", "cost_brl": float(cost.cost_brl)},
    )
    assert len(captured) == 1
    e = captured[0]
    # Envelope canônico
    assert uuid.UUID(e.event_id)  # event_id é UUID válido
    assert e.schema_version == 1
    assert e.event_type == "LLM_CALL"
    assert e.client == "amc"
    assert e.agent == "patricia-amc"
    assert e.contact_id == "ghl_abc"
    assert e.conversation_id == "ghl_conv_1"
    assert isinstance(e.occurred_at, datetime) and e.occurred_at.tzinfo is timezone.utc
    # Custo duplo
    assert e.cost_usd > 0
    assert e.cost_brl > 0
    assert e.cost_brl == (e.cost_usd * Decimal("5.40")).quantize(Decimal("0.000001"))
    assert e.usd_brl_rate == Decimal("5.40")
    assert e.pricing_version == "2026-06-17"


@pytest.mark.asyncio
async def test_event_id_unique(captured) -> None:
    for _ in range(3):
        await ev.emit_event(event_type="CONVERSATION_STARTED", contact_id="c1", payload={})
    ids = {e.event_id for e in captured}
    assert len(ids) == 3  # todos distintos
