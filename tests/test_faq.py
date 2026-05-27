from __future__ import annotations

import pytest

from zoi_agent.tools import faq as faq_mod


@pytest.mark.asyncio
async def test_faq_cache_hits_only_once(monkeypatch) -> None:
    calls = {"n": 0}

    async def fake_fetch() -> str:
        calls["n"] += 1
        return "financiamento: sim\nseguro: nao"

    # Reset cache
    fresh = faq_mod.TTLCache(ttl_seconds=60, loader=fake_fetch)
    monkeypatch.setattr(faq_mod, "_faq_cache", fresh)

    a = await faq_mod.get_faq_raw()
    b = await faq_mod.get_faq_raw()
    assert a == b
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_faq_parses_yaml(monkeypatch) -> None:
    async def fake_fetch() -> str:
        return "financiamento: sim\nlocalizacao: Joinville"

    fresh = faq_mod.TTLCache(ttl_seconds=60, loader=fake_fetch)
    monkeypatch.setattr(faq_mod, "_faq_cache", fresh)
    parsed = await faq_mod.get_faq_parsed()
    assert parsed["financiamento"] == "sim"
    assert parsed["localizacao"] == "Joinville"
