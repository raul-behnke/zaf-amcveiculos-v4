from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from zoi_agent.agent.schemas import (
    Collected,
    SessionState,
    StateUpdate,
    VeiculoOrigem,
)
from zoi_agent.tools import origem as om
from zoi_agent.tools.inventory import SearchResult, VehicleSummary


def _mk_result() -> SearchResult:
    return SearchResult(
        exatos=[
            VehicleSummary(
                titulo="Chevrolet Montana LT 1.4 Flex",
                marca="Chevrolet", modelo="Montana",
                ano=2019, preco=58900, quilometragem=95000,
                cambio="Manual", cor="Branco", opcionais=[],
                imagem=None, external_id="m-1",
            ),
            VehicleSummary(
                titulo="Chevrolet Montana LS 1.4",
                marca="Chevrolet", modelo="Montana",
                ano=2017, preco=46900, quilometragem=120000,
                cambio="Manual", cor="Prata", opcionais=[],
                imagem=None, external_id="m-2",
            ),
        ],
        parecidos=[],
        total=2,
    )


@pytest.mark.asyncio
async def test_origem_search_quando_nao_apresentada(monkeypatch) -> None:
    async def fake_search(query: str):
        return _mk_result()

    monkeypatch.setattr(om, "search_inventory", fake_search)
    state = SessionState(
        veiculo_origem=VeiculoOrigem(texto="Chevrolet Montana"),
    )
    payload = await om.buscar_veiculo_interesse_origem(state)
    assert payload is not None
    assert payload["texto_origem"] == "Chevrolet Montana"
    assert len(payload["matches"]["exatos"]) == 2


@pytest.mark.asyncio
async def test_origem_skip_quando_apresentada(monkeypatch) -> None:
    """vehicles_shown não-vazio sinaliza que lead já viu catálogo nosso —
    não re-apresentar origem proativamente."""
    called = {"n": 0}

    async def fake_search(query: str):
        called["n"] += 1
        return _mk_result()

    monkeypatch.setattr(om, "search_inventory", fake_search)
    state = SessionState(
        veiculo_origem=VeiculoOrigem(texto="Chevrolet Montana"),
        vehicles_shown=["alguma-coisa-vista-antes"],
    )
    payload = await om.buscar_veiculo_interesse_origem(state)
    assert payload is None
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_origem_skip_sem_origem() -> None:
    state = SessionState(veiculo_origem=None)
    assert await om.buscar_veiculo_interesse_origem(state) is None


def test_collect_external_ids() -> None:
    payload = {
        "matches": {
            "exatos": [{"external_id": "a"}, {"external_id": "b"}],
            "parecidos": [{"vehicle": {"external_id": "c"}, "motivo": "x"}],
        }
    }
    ids = om.collect_external_ids(payload)
    assert ids == ["a", "b", "c"]
