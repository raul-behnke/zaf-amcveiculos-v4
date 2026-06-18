"""GET /export/events — auth HMAC dedicada + envelope canônico."""
from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from zoi_agent.config import settings
from zoi_agent.endpoints import export as ex
from zoi_agent.main import app

client = TestClient(app)

_ROWS = [
    {
        "id": 1, "event_id": "uuid-1", "schema_version": 1, "event_type": "LLM_CALL",
        "client": "amc", "agent": "patricia-amc", "contact_id": "c1",
        "conversation_id": "conv1", "occurred_at": datetime(2026, 6, 17, 18, 42, tzinfo=timezone.utc),
        "payload": {"component": "patricia", "cost_brl": 0.0648},
    },
    {
        "id": 2, "event_id": "uuid-2", "schema_version": 1, "event_type": "CONVERSATION_STARTED",
        "client": "amc", "agent": "patricia-amc", "contact_id": "c2",
        "conversation_id": None, "occurred_at": datetime(2026, 6, 17, 18, 43, tzinfo=timezone.utc),
        "payload": {},
    },
]


class _FakeResult:
    def __init__(self, rows): self._rows = rows
    def mappings(self): return iter(self._rows)


class _FakeConn:
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def execute(self, sql, params):
        rows = [r for r in _ROWS if r["id"] > params["since"]][: params["lim"]]
        return _FakeResult(rows)


class _FakeEngine:
    def connect(self): return _FakeConn()


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(ex, "get_engine", lambda: _FakeEngine())


def _sig(secret: str, since: int) -> str:
    return hmac.new(secret.encode(), str(since).encode(), hashlib.sha256).hexdigest()


def test_no_secret_configured_401(monkeypatch) -> None:
    monkeypatch.setattr(settings, "zoi_export_secret", "")
    r = client.get("/export/events?since=0&secret=qualquer")
    assert r.status_code == 401


def test_wrong_secret_401(monkeypatch) -> None:
    monkeypatch.setattr(settings, "zoi_export_secret", "topsecret")
    r = client.get("/export/events?since=0&secret=errado")
    assert r.status_code == 401


def test_hmac_since_200_envelope(monkeypatch, db) -> None:
    monkeypatch.setattr(settings, "zoi_export_secret", "topsecret")
    r = client.get(f"/export/events?since=0&secret={_sig('topsecret', 0)}")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    assert body["next_cursor"] == 2
    e = body["events"][0]
    # envelope canônico
    assert e["event_id"] == "uuid-1"
    assert e["schema_version"] == 1
    assert e["client"] == "amc"
    assert e["agent"] == "patricia-amc"
    assert e["occurred_at"].startswith("2026-06-17T18:42")
    assert e["payload"]["cost_brl"] == 0.0648


def test_raw_secret_compat_and_cursor(monkeypatch, db) -> None:
    monkeypatch.setattr(settings, "zoi_export_secret", "topsecret")
    # secret cru aceito (compat); cursor since=1 -> só id=2
    r = client.get("/export/events?since=1&secret=topsecret")
    assert r.status_code == 200
    body = r.json()
    assert [e["event_id"] for e in body["events"]] == ["uuid-2"]
    assert body["next_cursor"] == 2
