from __future__ import annotations

from zoi_agent.agent.templates import (
    build_vehicle_blocks,
    render_vehicle_card,
    render_vehicle_list,
)


V_MONTANA = {
    "external_id": "m1",
    "titulo": "Chevrolet Montana LS 1.4",
    "marca": "Chevrolet",
    "modelo": "Montana",
    "ano": 2018,
    "preco": 49900,
    "quilometragem": 140000,
    "cambio": "Manual",
    "combustivel": "Flex",
    "opcionais": ["Ar-condicionado", "Direção hidráulica", "Rodas liga leve", "ABS", "Air Bag"],
}

V_ECOSPORT = {
    "external_id": "e1",
    "titulo": "Ford EcoSport Titanium 2.0",
    "marca": "Ford",
    "modelo": "EcoSport",
    "ano": 2017,
    "preco": 62900,
    "quilometragem": 130000,
    "cambio": "Automático",
    "combustivel": "Gasolina",
}

V_SAVEIRO = {
    "external_id": "s1",
    "titulo": "VW Saveiro Trendline 1.6",
    "marca": "VW",
    "modelo": "Saveiro",
    "ano": 2019,
    "preco": 58900,
    "quilometragem": 95000,
    "cambio": "Manual",
    "combustivel": "Flex",
}


def test_card_montana_full() -> None:
    card = render_vehicle_card(V_MONTANA)
    assert "🚗 *Chevrolet Montana LS 1.4*" in card
    assert "📅 2018" in card
    assert "🛣️ 140.000 km" in card
    assert "⚙️ Manual" in card
    assert "⛽ Flex" in card
    assert "💰 *R$ 49.900*" in card
    # Apenas 3 destaques
    assert "Destaques: Ar-condicionado, Direção hidráulica, Rodas liga leve" in card
    assert "ABS" not in card and "Air Bag" not in card


def test_card_sem_opcionais() -> None:
    v = dict(V_ECOSPORT)
    v.pop("opcionais", None)
    card = render_vehicle_card(v)
    assert "Destaques" not in card
    assert "💰 *R$ 62.900*" in card


def test_card_sem_titulo_usa_marca_modelo() -> None:
    v = {"marca": "Fiat", "modelo": "Uno", "ano": 2015, "preco": 25000}
    card = render_vehicle_card(v)
    assert "🚗 *Fiat Uno*" in card


def test_list_compacta_3() -> None:
    out = render_vehicle_list([V_MONTANA, V_ECOSPORT, V_SAVEIRO])
    lines = out.split("\n")
    assert lines[0] == "Achei essas opções pra você:"
    assert "1️⃣ *Chevrolet Montana 2018* — R$ 49.900 (140k km, manual)" in out
    assert "2️⃣ *Ford EcoSport 2017* — R$ 62.900 (130k km, automático)" in out
    assert "3️⃣ *VW Saveiro 2019* — R$ 58.900 (95k km, manual)" in out


def test_list_corta_em_3() -> None:
    extra = dict(V_MONTANA)
    extra["modelo"] = "Onix"
    out = render_vehicle_list([V_MONTANA, V_ECOSPORT, V_SAVEIRO, extra])
    assert "4️⃣" not in out
    assert "Onix" not in out


def test_build_blocks_1_exato_card() -> None:
    blocks = build_vehicle_blocks(exatos=[V_MONTANA])
    assert len(blocks) == 1
    assert "🚗" in blocks[0]
    assert "Destaques" in blocks[0]


def test_build_blocks_2_exatos_lista() -> None:
    blocks = build_vehicle_blocks(exatos=[V_MONTANA, V_ECOSPORT])
    assert len(blocks) == 1
    assert "1️⃣" in blocks[0]
    assert "2️⃣" in blocks[0]


def test_build_blocks_zero_exatos_com_parecidos() -> None:
    blocks = build_vehicle_blocks(exatos=[], parecidos=[V_MONTANA, V_ECOSPORT])
    assert len(blocks) == 2
    assert "Não achei exatamente" in blocks[0]
    assert "1️⃣" in blocks[1]


def test_build_blocks_vazio() -> None:
    assert build_vehicle_blocks(exatos=[], parecidos=[]) == []
