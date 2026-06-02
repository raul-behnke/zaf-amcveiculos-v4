from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from zoi_agent.config import settings
from zoi_agent.tools import calendar as cal

SP = ZoneInfo("America/Sao_Paulo")


def _fake_freeslots(now: datetime) -> dict:
    """Gera response simulada com hoje + amanhã."""
    today = now.date()
    tomorrow = (now.replace(hour=23) - now.replace(hour=23)).days  # noop hack
    from datetime import timedelta

    d1 = today
    d2 = today + timedelta(days=1)
    return {
        d1.isoformat(): {
            "slots": [
                f"{d1.isoformat()}T08:00:00-03:00",
                f"{d1.isoformat()}T11:00:00-03:00",
                f"{d1.isoformat()}T14:00:00-03:00",
                f"{d1.isoformat()}T15:00:00-03:00",
                f"{d1.isoformat()}T19:00:00-03:00",
            ]
        },
        d2.isoformat(): {
            "slots": [
                f"{d2.isoformat()}T09:00:00-03:00",
                f"{d2.isoformat()}T13:00:00-03:00",
            ]
        },
        "traceId": "fake",
    }


@pytest.fixture
def mock_client(monkeypatch):
    fake_get = AsyncMock()
    fake_post = AsyncMock()
    fake = type("C", (), {"get": fake_get, "post": fake_post})()
    monkeypatch.setattr(cal, "get_client", lambda: fake)
    return fake


@pytest.mark.asyncio
async def test_propose_slots_interleave(mock_client, monkeypatch) -> None:
    # Fixa "agora" às 07:00 de um dia útil para todos os slots serem futuros
    fixed = datetime(2026, 6, 1, 7, 0, tzinfo=SP)  # segunda

    class FakeDT:
        @classmethod
        def now(cls, tz=None):
            return fixed

    monkeypatch.setattr(cal, "datetime", FakeDT)
    mock_client.get.return_value = _fake_freeslots(fixed)

    slots, _fb = await cal.propose_slots(limit=3)
    assert len(slots) == 3
    # Espera 1 do d1, 1 do d2, 1 do d1 (interleave)
    dias = {s.dt.date() for s in slots}
    assert len(dias) == 2


@pytest.mark.asyncio
async def test_propose_filtra_periodo_manha(mock_client, monkeypatch) -> None:
    fixed = datetime(2026, 6, 1, 7, 0, tzinfo=SP)

    class FakeDT:
        @classmethod
        def now(cls, tz=None):
            return fixed

    monkeypatch.setattr(cal, "datetime", FakeDT)
    mock_client.get.return_value = _fake_freeslots(fixed)

    slots, _fb = await cal.propose_slots(periodo="manha", limit=5)
    assert all(s.dt.hour < 12 for s in slots)
    assert len(slots) >= 1


@pytest.mark.asyncio
async def test_propose_filtra_periodo_noite(mock_client, monkeypatch) -> None:
    fixed = datetime(2026, 6, 1, 7, 0, tzinfo=SP)

    class FakeDT:
        @classmethod
        def now(cls, tz=None):
            return fixed

    monkeypatch.setattr(cal, "datetime", FakeDT)
    mock_client.get.return_value = _fake_freeslots(fixed)

    slots, _fb = await cal.propose_slots(periodo="noite", limit=5)
    assert all(s.dt.hour >= 18 for s in slots)


@pytest.mark.asyncio
async def test_propose_filtra_dia_amanha(mock_client, monkeypatch) -> None:
    fixed = datetime(2026, 6, 1, 7, 0, tzinfo=SP)

    class FakeDT:
        @classmethod
        def now(cls, tz=None):
            return fixed

    monkeypatch.setattr(cal, "datetime", FakeDT)
    mock_client.get.return_value = _fake_freeslots(fixed)

    slots, _fb = await cal.propose_slots(dia="amanhã", limit=5)
    target = (fixed.date()).replace().toordinal() + 1
    assert all(s.dt.date().toordinal() == target for s in slots)


@pytest.mark.asyncio
async def test_propose_pula_slots_passados(mock_client, monkeypatch) -> None:
    fixed = datetime(2026, 6, 1, 13, 0, tzinfo=SP)  # 13h

    class FakeDT:
        @classmethod
        def now(cls, tz=None):
            return fixed

    monkeypatch.setattr(cal, "datetime", FakeDT)
    mock_client.get.return_value = _fake_freeslots(fixed)

    slots, _fb = await cal.propose_slots(limit=10)
    for s in slots:
        assert s.dt > fixed


def test_slot_label_pt() -> None:
    s = cal.Slot(iso="2026-06-03T09:30:00-03:00")
    # 3/6 é quarta
    assert s.label_pt() == "quarta 03/06 às 09:30"


@pytest.mark.asyncio
async def test_book_appointment_payload(mock_client) -> None:
    mock_client.post.return_value = {"appointment": {"id": "apt-1"}}
    resp = await cal.book_appointment(
        contact_id="c1",
        slot_iso="2026-06-03T09:30:00-03:00",
        lead_name="Raul",
        modelo="Renault Duster",
        notes="qualificado, foco Duster",
    )
    assert resp["appointment"]["id"] == "apt-1"
    args = mock_client.post.await_args
    assert args.args[0] == "/calendars/events/appointments"
    payload = args.kwargs["json"]
    assert payload["calendarId"] == settings.ghl_calendar_id
    assert payload["locationId"] == settings.ghl_location_id
    assert payload["contactId"] == "c1"
    assert payload["appointmentStatus"] == "confirmed"
    assert payload["title"] == "Visita AMC — Raul — Renault Duster"
    assert payload["startTime"].startswith("2026-06-03T09:30:00")
    # endTime = start + 60min
    assert payload["endTime"].startswith("2026-06-03T10:30:00")
    assert payload["notes"] == "qualificado, foco Duster"
