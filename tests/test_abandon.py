from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from zoi_agent.agent.schemas import Collected, SessionState
from zoi_agent.config import settings
from zoi_agent.endpoints import abandon as ab
from zoi_agent.main import app


@pytest.fixture
def store(monkeypatch):
    store: dict[str, SessionState] = {}

    async def load(cid):
        return store.get(cid)

    async def save(cid, st):
        store[cid] = st

    monkeypatch.setattr(ab.session_repo, "load", load)
    monkeypatch.setattr(ab.session_repo, "save", save)
    monkeypatch.setattr(ab, "emit_event", AsyncMock())
    return store


def test_secret_403(store) -> None:
    with TestClient(app) as c:
        r = c.post("/sessions/c1/abandon?secret=nope")
        assert r.status_code == 403


def test_abandon_sessao_existente(store) -> None:
    store["c1"] = SessionState(stage="descoberta", collected=Collected(nome="Raul"))
    with TestClient(app) as c:
        r = c.post(f"/sessions/c1/abandon?secret={settings.webhook_secret}")
        assert r.status_code == 200
        assert r.json()["skipped"] is False
    saved = store["c1"]
    assert saved.terminal_reason == "abandonado"
    assert saved.stage == "fechado"
    # nome preservado, sem nota/workflow disparados
    assert saved.collected.nome == "Raul"


def test_abandon_sem_sessao_cria_terminal_stub(store) -> None:
    with TestClient(app) as c:
        r = c.post(f"/sessions/cX/abandon?secret={settings.webhook_secret}")
        assert r.status_code == 200
        assert r.json().get("created_terminal") is True
    assert store["cX"].terminal_reason == "abandonado"
    assert store["cX"].stage == "fechado"


def test_abandon_idempotente_terminal_existente(store) -> None:
    store["c1"] = SessionState(stage="fechado", terminal_reason="qualificado_agendado")
    with TestClient(app) as c:
        r = c.post(f"/sessions/c1/abandon?secret={settings.webhook_secret}")
        body = r.json()
    assert body["skipped"] is True
    assert body["reason"] == "qualificado_agendado"
    # NÃO sobrescreveu
    assert store["c1"].terminal_reason == "qualificado_agendado"
