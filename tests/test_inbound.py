from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from zoi_agent.config import settings
from zoi_agent.endpoints import inbound as inb
from zoi_agent.main import app


# --- Helpers unit ---------------------------------------------------------


def test_strip_received_on() -> None:
    assert inb.strip_received_on("Olá!\n\nReceived on 📱[Lucas]") == "Olá!"
    assert inb.strip_received_on("Tudo bem? \n  Received on [device]") == "Tudo bem?"
    assert inb.strip_received_on("texto simples") == "texto simples"
    assert inb.strip_received_on("") == ""
    assert inb.strip_received_on(None) == ""


def test_parse_tags_csv() -> None:
    assert inb.parse_tags_csv("a,b, c") == {"a", "b", "c"}
    assert inb.parse_tags_csv(["x", "y"]) == {"x", "y"}
    assert inb.parse_tags_csv("") == set()
    assert inb.parse_tags_csv(None) == set()


def test_classify_attachments() -> None:
    urls = [
        "https://x/audio.ogg",
        "https://x/foto.jpg",
        "https://x/doc.pdf",
        "https://x/audio2.opus",
        "https://x/img.PNG",
    ]
    c = inb.classify_attachments(urls)
    assert set(c["audio"]) == {"https://x/audio.ogg", "https://x/audio2.opus"}
    assert set(c["image"]) == {"https://x/foto.jpg", "https://x/img.PNG"}
    assert c["other"] == ["https://x/doc.pdf"]


def test_extract_latest_inbound_ordering() -> None:
    msgs = [
        {"direction": "outbound", "dateAdded": "2026-05-27T12:00:00Z", "body": "out"},
        {"direction": "inbound", "dateAdded": "2026-05-27T10:00:00Z", "body": "primeiro"},
        {"direction": "inbound", "dateAdded": "2026-05-27T14:00:00Z", "body": "ultimo"},
    ]
    latest = inb.extract_latest_inbound(msgs)
    assert latest and latest["body"] == "ultimo"


# --- Endpoint -------------------------------------------------------------


@pytest.fixture
def patch_all(monkeypatch):
    search_mock = AsyncMock()
    msgs_mock = AsyncMock()
    process_mock = AsyncMock()
    transcribe_mock = AsyncMock()

    monkeypatch.setattr(inb.ghl_conv, "search_conversations", search_mock)
    monkeypatch.setattr(inb.ghl_conv, "get_messages", msgs_mock)
    monkeypatch.setattr(inb, "process_turn", process_mock)
    monkeypatch.setattr(inb, "transcribe_url", transcribe_mock)

    return {
        "search": search_mock,
        "messages": msgs_mock,
        "process": process_mock,
        "transcribe": transcribe_mock,
    }


def _payload(**overrides) -> dict:
    base = {
        "contact_id": "c1",
        "tags": "agente-ia,outra",
    }
    base.update(overrides)
    return base


def _conv(direction: str = "inbound", conv_id: str = "conv-1") -> dict:
    return {"conversations": [{"id": conv_id, "lastMessageDirection": direction}]}


def _msgs(messages: list[dict]) -> dict:
    return {"messages": {"messages": messages}}


def test_secret_403(patch_all) -> None:
    with TestClient(app) as c:
        r = c.post("/webhook/inbound?secret=nope", json=_payload())
        assert r.status_code == 403


def test_c24_tag_ausente_ignora(patch_all, monkeypatch) -> None:
    async def fake_contact(cid):
        return {"contact": {"tags": ["outra"]}}

    from zoi_agent.ghl import contacts as gc

    monkeypatch.setattr(gc, "get_contact", fake_contact)
    payload = _payload(tags="outra")
    with TestClient(app) as c:
        r = c.post(f"/webhook/inbound?secret={settings.webhook_secret}", json=payload)
    assert r.status_code == 200
    assert r.json()["reason"] == "no agent tag"
    patch_all["process"].assert_not_awaited()


def test_c5_texto_dispatch(patch_all) -> None:
    patch_all["search"].return_value = _conv()
    patch_all["messages"].return_value = _msgs(
        [
            {"direction": "outbound", "dateAdded": "2026-05-27T10:00Z", "body": "oi"},
            {"direction": "inbound", "dateAdded": "2026-05-27T11:00Z",
             "body": "Quero ver opções\n\nReceived on 📱[Lucas]", "attachments": []},
        ]
    )
    with TestClient(app) as c:
        r = c.post(f"/webhook/inbound?secret={settings.webhook_secret}", json=_payload())
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "accepted"
    args = patch_all["process"].await_args.args
    assert args[0] == "c1"
    assert args[1] == "Quero ver opções"  # sufixo strippado


def test_c12_so_imagem_ignora(patch_all) -> None:
    patch_all["search"].return_value = _conv()
    patch_all["messages"].return_value = _msgs(
        [
            {"direction": "inbound", "dateAdded": "2026-05-27T11:00Z",
             "body": "Received on 📱[Lucas]",  # sufixo only -> strip = vazio
             "attachments": ["https://x/foto.jpg"]},
        ]
    )
    with TestClient(app) as c:
        r = c.post(f"/webhook/inbound?secret={settings.webhook_secret}", json=_payload())
    assert r.status_code == 200
    assert r.json()["reason"] == "attachment only"
    patch_all["process"].assert_not_awaited()


def test_c10_audio_transcrevido(patch_all) -> None:
    patch_all["search"].return_value = _conv()
    patch_all["messages"].return_value = _msgs(
        [
            {"direction": "inbound", "dateAdded": "2026-05-27T11:00Z",
             "body": "Received on 📱[Lucas]",
             "attachments": ["https://x/audio.ogg"]},
        ]
    )
    patch_all["transcribe"].return_value = "oi tudo bem quero um SUV"

    with TestClient(app) as c:
        r = c.post(f"/webhook/inbound?secret={settings.webhook_secret}", json=_payload())
    assert r.status_code == 200
    args = patch_all["process"].await_args.args
    assert args[1] == "oi tudo bem quero um SUV"


def test_c11_multi_audio_concat(patch_all) -> None:
    patch_all["search"].return_value = _conv()
    patch_all["messages"].return_value = _msgs(
        [
            {"direction": "inbound", "dateAdded": "2026-05-27T11:00Z",
             "body": "Received on 📱[Lucas]",
             "attachments": ["https://x/a.ogg", "https://x/b.opus"]},
        ]
    )
    patch_all["transcribe"].side_effect = ["primeiro", "segundo"]

    with TestClient(app) as c:
        r = c.post(f"/webhook/inbound?secret={settings.webhook_secret}", json=_payload())
    assert r.status_code == 200
    args = patch_all["process"].await_args.args
    assert "primeiro" in args[1] and "segundo" in args[1]


def test_last_outbound_ignora(patch_all) -> None:
    patch_all["search"].return_value = _conv(direction="outbound")
    with TestClient(app) as c:
        r = c.post(f"/webhook/inbound?secret={settings.webhook_secret}", json=_payload())
    assert r.status_code == 200
    assert r.json()["reason"] == "last message outbound"
    patch_all["process"].assert_not_awaited()


def test_no_conversation(patch_all) -> None:
    patch_all["search"].return_value = {"conversations": []}
    with TestClient(app) as c:
        r = c.post(f"/webhook/inbound?secret={settings.webhook_secret}", json=_payload())
    assert r.status_code == 200
    assert r.json()["reason"] == "no conversation"


def test_audio_e_texto_concat(patch_all) -> None:
    """Lead manda áudio E texto na mesma mensagem -> concatena."""
    patch_all["search"].return_value = _conv()
    patch_all["messages"].return_value = _msgs(
        [
            {"direction": "inbound", "dateAdded": "2026-05-27T11:00Z",
             "body": "olha esse\n\nReceived on 📱[Lucas]",
             "attachments": ["https://x/a.ogg"]},
        ]
    )
    patch_all["transcribe"].return_value = "voz aqui"
    with TestClient(app) as c:
        r = c.post(f"/webhook/inbound?secret={settings.webhook_secret}", json=_payload())
    assert r.status_code == 200
    text = patch_all["process"].await_args.args[1]
    assert "olha esse" in text and "voz aqui" in text
