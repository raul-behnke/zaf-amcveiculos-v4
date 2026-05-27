from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from zoi_agent import orchestrator as orch
from zoi_agent.agent.schemas import Collected, SessionState, StateUpdate
from zoi_agent.tools import photos as photos_mod


# --- pick_target_vehicle --------------------------------------------------


@pytest.fixture
def fake_inv(monkeypatch) -> list[dict]:
    inv = [
        {"external_id": "1", "marca": "Renault", "modelo": "Logan", "titulo": "Renault Logan",
         "imagens": ["http://x/1a", "http://x/1b", "http://x/1c"]},
        {"external_id": "2", "marca": "Renault", "modelo": "Duster", "titulo": "Renault Duster",
         "imagens": ["http://x/2a"]},
        {"external_id": "3", "marca": "Jeep", "modelo": "Compass", "titulo": "Jeep Compass",
         "imagens": ["http://x/3a", "http://x/3b"]},
    ]

    async def fake_load():
        return inv

    monkeypatch.setattr(photos_mod, "load_inventory", fake_load)
    return inv


@pytest.mark.asyncio
async def test_pick_por_keyword(fake_inv) -> None:
    v = await photos_mod.pick_target_vehicle(
        last_message="manda foto do Logan", state=SessionState()
    )
    assert v and v["external_id"] == "1"


@pytest.mark.asyncio
async def test_pick_por_marca(fake_inv) -> None:
    v = await photos_mod.pick_target_vehicle(
        last_message="quero o renault", state=SessionState()
    )
    assert v and v["marca"] == "Renault"


@pytest.mark.asyncio
async def test_pick_por_vehicles_shown(fake_inv) -> None:
    state = SessionState(vehicles_shown=["3"])
    v = await photos_mod.pick_target_vehicle(
        last_message="me manda as fotos", state=state
    )
    assert v and v["external_id"] == "3"


@pytest.mark.asyncio
async def test_pick_por_focus(fake_inv) -> None:
    state = SessionState(
        collected=Collected(veiculo_interesse="Duster", veiculo_interesse_confirmado=True)
    )
    v = await photos_mod.pick_target_vehicle(
        last_message="me manda foto", state=state
    )
    assert v and v["external_id"] == "2"


@pytest.mark.asyncio
async def test_pick_none(fake_inv) -> None:
    v = await photos_mod.pick_target_vehicle(
        last_message="oi tudo bem", state=SessionState()
    )
    assert v is None


# --- build_photo_payload --------------------------------------------------


@pytest.mark.asyncio
async def test_payload_c7_multi_imagem(fake_inv) -> None:
    p = await photos_mod.build_photo_payload(
        last_message="manda foto do Logan", state=SessionState()
    )
    assert p["available"] is True
    assert p["single_image_only"] is False
    assert p["will_send_count"] == 3
    assert len(p["images"]) == 3
    assert p["vehicle"]["external_id"] == "1"


@pytest.mark.asyncio
async def test_payload_c8_uma_imagem(fake_inv) -> None:
    p = await photos_mod.build_photo_payload(
        last_message="manda foto do Duster", state=SessionState()
    )
    assert p["available"] is True
    assert p["single_image_only"] is True
    assert p["will_send_count"] == 0
    assert p["images"] == []


@pytest.mark.asyncio
async def test_payload_sem_alvo(fake_inv) -> None:
    p = await photos_mod.build_photo_payload(
        last_message="foto", state=SessionState()
    )
    assert p["available"] is False
    assert p["images"] == []
    assert p["will_send_count"] == 0


# --- Orchestrator: envio paralelo + ordem -------------------------------


@pytest.fixture
def orch_patches(monkeypatch):
    sent_text: list[str] = []
    sent_photos: list[str] = []
    send_log: list[tuple[str, str]] = []  # (kind, value)

    async def fake_send(*, contact_id, conversation_id, text=None, attachments=None):
        if attachments:
            for a in attachments:
                send_log.append(("photo", a))
                sent_photos.append(a)
        elif text is not None:
            send_log.append(("text", text))
            sent_text.append(text)

    # Override _send_bubble e _send_photo pra registrar via send_log
    async def fake_bubble(*, contact_id, conversation_id, text):
        send_log.append(("text", text))
        sent_text.append(text)

    async def fake_photo(*, contact_id, conversation_id, url):
        send_log.append(("photo", url))
        sent_photos.append(url)

    monkeypatch.setattr(orch, "_send_bubble", fake_bubble)
    monkeypatch.setattr(orch, "_send_photo", fake_photo)
    monkeypatch.setattr(orch.settings, "responder_sleep_min", 0.0)
    monkeypatch.setattr(orch.settings, "responder_sleep_max", 0.0)
    return {"text": sent_text, "photos": sent_photos, "log": send_log}


@pytest.mark.asyncio
async def test_send_photos_then_bubbles(orch_patches) -> None:
    await orch._send_bubbles(
        contact_id="c1",
        conversation_id="conv",
        bubbles=["bolha1", "bolha2"],
        photos=["http://x/a.jpg", "http://x/b.jpg"],
    )
    log = orch_patches["log"]
    # Primeiro vêm as 2 fotos (ordem pode variar), depois as bolhas em ordem
    first_two = log[:2]
    assert all(k == "photo" for k, _ in first_two)
    assert set(v for _, v in first_two) == {"http://x/a.jpg", "http://x/b.jpg"}
    assert log[2:] == [("text", "bolha1"), ("text", "bolha2")]


@pytest.mark.asyncio
async def test_send_photo_falha_nao_aborta_bolhas(orch_patches, monkeypatch) -> None:
    async def flaky_photo(*, contact_id, conversation_id, url):
        if "fail" in url:
            raise RuntimeError("boom")
        orch_patches["log"].append(("photo", url))
        orch_patches["photos"].append(url)

    monkeypatch.setattr(orch, "_send_photo", flaky_photo)
    await orch._send_bubbles(
        contact_id="c1",
        conversation_id="conv",
        bubbles=["b1"],
        photos=["http://x/ok.jpg", "http://x/fail.jpg"],
    )
    assert ("text", "b1") in orch_patches["log"]
    assert "http://x/ok.jpg" in orch_patches["photos"]
