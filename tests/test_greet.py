from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from zoi_agent.agent.schemas import SessionState
from zoi_agent.config import settings
from zoi_agent.endpoints import greet as greet_mod
from zoi_agent.main import app


@pytest.fixture
def patch_all(monkeypatch):
    # state store em memória
    store: dict[str, SessionState] = {}

    async def fake_load_or_new(cid):
        return store.get(cid) or SessionState()

    async def fake_save(cid, st):
        store[cid] = st

    monkeypatch.setattr(greet_mod.session_repo, "load_or_new", fake_load_or_new)
    monkeypatch.setattr(greet_mod.session_repo, "save", fake_save)
    monkeypatch.setattr(greet_mod, "emit_event", AsyncMock())

    get_contact_mock = AsyncMock()
    send_mock = AsyncMock()
    update_field_mock = AsyncMock()

    monkeypatch.setattr(greet_mod.ghl_contacts, "get_contact", get_contact_mock)
    monkeypatch.setattr(greet_mod.ghl_contacts, "update_custom_field", update_field_mock)
    monkeypatch.setattr(greet_mod.ghl_conv, "send_message", send_mock)

    return {
        "store": store,
        "get_contact": get_contact_mock,
        "send": send_mock,
        "update_field": update_field_mock,
    }


def _contact(*, veiculo: str | None = None, saud_sim: bool = False) -> dict:
    fields = []
    if veiculo is not None:
        fields.append({"id": settings.ghl_field_veiculo_interesse, "value": veiculo})
    fields.append(
        {"id": settings.ghl_field_saudacao_prevendas, "value": "SIM" if saud_sim else ""}
    )
    return {"contact": {"id": "c1", "customFields": fields, "tags": ["agente-ia"]}}


def test_secret_invalid_403(patch_all) -> None:
    with TestClient(app) as client:
        r = client.post("/sessions/c1/greet?secret=wrong")
        assert r.status_code == 403


def test_greet_sem_veiculo(patch_all) -> None:
    patch_all["get_contact"].return_value = _contact()
    with TestClient(app) as client:
        r = client.post(f"/sessions/c1/greet?secret={settings.webhook_secret}")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["skipped"] is False
        assert body["com_veiculo"] is False
    sent_kwargs = patch_all["send"].await_args.kwargs
    assert "carro específico" in sent_kwargs["message"]
    assert sent_kwargs["message_type"] == "SMS"
    assert patch_all["store"]["c1"].greeted is True
    assert patch_all["store"]["c1"].veiculo_origem is None


def test_greet_com_veiculo(patch_all) -> None:
    patch_all["get_contact"].return_value = _contact(veiculo="Renault Duster")
    with TestClient(app) as client:
        r = client.post(f"/sessions/c1/greet?secret={settings.webhook_secret}")
        assert r.status_code == 200
        body = r.json()
        assert body["com_veiculo"] is True
        assert body["veiculo"] == "Renault Duster"
    sent = patch_all["send"].await_args.kwargs["message"]
    assert "Renault Duster" in sent
    st = patch_all["store"]["c1"]
    assert st.greeted is True
    assert st.veiculo_origem and st.veiculo_origem.texto == "Renault Duster"
    # marca SAUDAÇÃO=SIM
    args = patch_all["update_field"].await_args.args
    assert args[1] == settings.ghl_field_saudacao_prevendas
    assert args[2] == "SIM"


def test_idempotent_via_state(patch_all) -> None:
    patch_all["store"]["c1"] = SessionState(greeted=True)
    patch_all["get_contact"].return_value = _contact()
    with TestClient(app) as client:
        r = client.post(f"/sessions/c1/greet?secret={settings.webhook_secret}")
        assert r.status_code == 200
        assert r.json()["skipped"] is True
    patch_all["send"].assert_not_awaited()


def test_idempotent_via_custom_field(patch_all) -> None:
    patch_all["get_contact"].return_value = _contact(saud_sim=True)
    with TestClient(app) as client:
        r = client.post(f"/sessions/c1/greet?secret={settings.webhook_secret}")
        assert r.status_code == 200
        assert r.json()["skipped"] is True
    patch_all["send"].assert_not_awaited()


def test_send_failure_502(patch_all) -> None:
    patch_all["get_contact"].return_value = _contact()
    patch_all["send"].side_effect = RuntimeError("boom")
    with TestClient(app) as client:
        r = client.post(f"/sessions/c1/greet?secret={settings.webhook_secret}")
        assert r.status_code == 502
    # state não foi gravado
    assert "c1" not in patch_all["store"]
