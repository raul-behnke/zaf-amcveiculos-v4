"""C31-C33: photos.pick_target_vehicle priority + rendered_ids tracking."""
from __future__ import annotations

import pytest

from zoi_agent.agent.schemas import Collected, SessionState, VeiculoOrigem
from zoi_agent.agent.templates import build_vehicle_blocks_with_ids
from zoi_agent.tools import photos as photos_mod


MONTANA = {
    "external_id": "1632332",
    "titulo": "Chevrolet Montana LS 1.4",
    "marca": "Chevrolet",
    "modelo": "Montana",
    "ano": 2018,
    "preco": 49900,
    "quilometragem": 140000,
    "cambio": "Mecânico",
    "imagens": ["a.jpg", "b.jpg", "c.jpg"],
}
ECOSPORT = {
    "external_id": "1632540",
    "titulo": "Ford EcoSport Freestyle 1.6",
    "marca": "Ford",
    "modelo": "EcoSport",
    "ano": 2017,
    "preco": 62900,
    "quilometragem": 130000,
    "cambio": "Automático",
    "imagens": ["x.jpg", "y.jpg"],
}
INVENTORY = [MONTANA, ECOSPORT]


@pytest.fixture(autouse=True)
def stub_inventory(monkeypatch):
    async def _load():
        return INVENTORY
    monkeypatch.setattr(photos_mod, "load_inventory", _load)


def test_c33_rendered_ids_card_unico() -> None:
    """1 exato -> rendered_ids tem só esse, nunca candidatos extras."""
    blocks, ids = build_vehicle_blocks_with_ids(
        exatos=[MONTANA], parecidos=[ECOSPORT]
    )
    assert len(blocks) == 1
    assert ids == ["1632332"]
    assert "1632540" not in ids  # ECOSPORT (parecido) NÃO entra


def test_c33_rendered_ids_lista() -> None:
    """2+ exatos -> lista renderiza top-3, ids batem."""
    _, ids = build_vehicle_blocks_with_ids(exatos=[MONTANA, ECOSPORT])
    assert ids == ["1632332", "1632540"]


def test_c33_rendered_ids_parecidos_quando_zero_exatos() -> None:
    _, ids = build_vehicle_blocks_with_ids(exatos=[], parecidos=[ECOSPORT])
    assert ids == ["1632540"]


def test_c33_rendered_ids_vazio() -> None:
    _, ids = build_vehicle_blocks_with_ids(exatos=[], parecidos=[])
    assert ids == []


@pytest.mark.asyncio
async def test_c32_last_card_id_tem_prioridade_sobre_vehicles_shown() -> None:
    """Bug original: vehicles_shown[-1]=EcoSport pegou foto errada quando card era Montana."""
    state = SessionState(
        last_card_external_id="1632332",  # Montana foi o card
        vehicles_shown=["1632332", "1632540"],  # ambos passaram em algum momento
    )
    v = await photos_mod.pick_target_vehicle(last_message="tem fotos?", state=state)
    assert v is not None
    assert v["external_id"] == "1632332"  # Montana, não EcoSport


@pytest.mark.asyncio
async def test_keyword_match_overrides_last_card() -> None:
    """Lead cita modelo explicitamente -> usa esse, ignora last_card."""
    state = SessionState(last_card_external_id="1632332")  # Montana
    v = await photos_mod.pick_target_vehicle(
        last_message="me mostra o EcoSport", state=state
    )
    assert v["external_id"] == "1632540"


@pytest.mark.asyncio
async def test_origem_fallback_quando_sem_card_nem_keyword() -> None:
    state = SessionState(
        veiculo_origem=VeiculoOrigem(texto="Chevrolet Montana"),
    )
    v = await photos_mod.pick_target_vehicle(last_message="tem fotos?", state=state)
    assert v["external_id"] == "1632332"


@pytest.mark.asyncio
async def test_focus_fallback() -> None:
    state = SessionState(
        collected=Collected(
            veiculo_interesse_confirmado=True, veiculo_interesse="EcoSport"
        ),
    )
    v = await photos_mod.pick_target_vehicle(last_message="manda foto", state=state)
    assert v["external_id"] == "1632540"
