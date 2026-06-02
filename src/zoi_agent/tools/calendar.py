"""Calendário GHL — propose_slots + book_appointment.

PLAN §11:
- GET /calendars/{id}/free-slots janela hoje + amanhã + depois (3 dias)
- Filtra por preferência (dia + periodo: manha/tarde/noite)
- Retorna até 3 slots, espalhando entre dias
- POST /calendars/events/appointments: confirmed, 60min, title "Visita AMC — {nome} — {modelo}"
- assignedUserId omitido (default do calendar), address omitido
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from dateutil import parser as dtparser

from zoi_agent.config import settings
from zoi_agent.ghl.client import get_client
from zoi_agent.logging import get_logger

log = get_logger(__name__)

Period = Literal["manha", "tarde", "noite"]


# --- Data classes ---------------------------------------------------------


@dataclass
class Slot:
    iso: str  # "2026-05-28T09:30:00-03:00"

    @property
    def dt(self) -> datetime:
        return dtparser.isoparse(self.iso)

    def label_pt(self) -> str:
        tz = ZoneInfo(settings.app_timezone)
        d = self.dt.astimezone(tz)
        weekday = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"][d.weekday()]
        return f"{weekday} {d.day:02d}/{d.month:02d} às {d.hour:02d}:{d.minute:02d}"


# --- propose_slots --------------------------------------------------------


def _period_of(d: datetime) -> Period:
    h = d.hour
    if h < 12:
        return "manha"
    if h < 18:
        return "tarde"
    return "noite"


def _matches_day_hint(d: datetime, hint: str | None, now: datetime) -> bool:
    if not hint:
        return True
    h = hint.strip().lower()
    if h in ("hoje", "today"):
        return d.date() == now.date()
    if h in ("amanhã", "amanha", "tomorrow"):
        return d.date() == (now + timedelta(days=1)).date()
    if h in ("depois de amanhã", "depois de amanha"):
        return d.date() == (now + timedelta(days=2)).date()
    # tenta parse dd/mm
    try:
        if "/" in h:
            parts = h.split("/")
            day = int(parts[0])
            month = int(parts[1]) if len(parts) > 1 else now.month
            return d.day == day and d.month == month
    except Exception:
        pass
    # nome de dia da semana
    weekdays = {
        "segunda": 0, "terca": 1, "terça": 1, "quarta": 2, "quinta": 3,
        "sexta": 4, "sabado": 5, "sábado": 5, "domingo": 6,
    }
    if h in weekdays:
        return d.weekday() == weekdays[h]
    return True


def _filter_and_pick(
    by_day: dict[str, dict],
    *,
    dia: str | None,
    periodo: Period | None,
    limit: int,
    now: datetime,
) -> list[Slot]:
    """Filtra + interleava entre dias pra não retornar 3 slots do mesmo dia."""
    days_sorted = sorted(k for k in by_day.keys() if "T" not in k and "-" in k)
    candidates_by_day: dict[str, list[Slot]] = {}
    for dkey in days_sorted:
        block = by_day.get(dkey) or {}
        slots_iso = block.get("slots") or []
        kept: list[Slot] = []
        for s in slots_iso:
            try:
                d = dtparser.isoparse(s)
            except Exception:
                continue
            if d <= now:
                continue
            if not _matches_day_hint(d, dia, now):
                continue
            if periodo and _period_of(d) != periodo:
                continue
            kept.append(Slot(iso=s))
        if kept:
            candidates_by_day[dkey] = kept

    # Round-robin pelos dias até atingir o limit
    picked: list[Slot] = []
    while len(picked) < limit and candidates_by_day:
        empty: list[str] = []
        for dkey in list(candidates_by_day.keys()):
            if not candidates_by_day[dkey]:
                empty.append(dkey)
                continue
            picked.append(candidates_by_day[dkey].pop(0))
            if len(picked) >= limit:
                break
        for k in empty:
            candidates_by_day.pop(k, None)
    return picked


async def propose_slots(
    *,
    dia: str | None = None,
    periodo: Period | None = None,
    limit: int = 3,
    janela_dias: int = 7,
) -> tuple[list[Slot], bool]:
    """Retorna (slots, fallback). `fallback=True` quando a preferência
    do lead (dia/periodo) não tinha disponibilidade e tivemos que ignorar
    o filtro — responder usa isso pra explicar ao lead que não tem o dia
    pedido mas tem opções na semana.

    Janela de 7 dias (default) cobre semana toda incluindo sábado/domingo,
    desde que o calendário do GHL tenha disponibilidade configurada lá.
    """
    tz = ZoneInfo(settings.app_timezone)
    now = datetime.now(tz)
    end = now + timedelta(days=janela_dias)
    client = get_client()
    raw = await client.get(
        f"/calendars/{settings.ghl_calendar_id}/free-slots",
        params={
            "startDate": int(now.timestamp() * 1000),
            "endDate": int(end.timestamp() * 1000),
        },
        operation="calendar.free_slots",
    )
    slots = _filter_and_pick(raw, dia=dia, periodo=periodo, limit=limit, now=now)
    fallback = False
    # Se filtro por preferência veio vazio, tenta sem filtro pra não deixar
    # o lead sem opção alguma.
    if not slots and (dia or periodo):
        log.info("calendar_slots_fallback_no_pref_match", dia=dia, periodo=periodo)
        slots = _filter_and_pick(raw, dia=None, periodo=None, limit=limit, now=now)
        fallback = True
    log.info(
        "calendar_slots_proposed",
        dia=dia, periodo=periodo, returned=len(slots),
        fallback=fallback,
        slots=[s.iso for s in slots],
    )
    return slots, fallback


# --- book_appointment -----------------------------------------------------


async def book_appointment(
    *,
    contact_id: str,
    slot_iso: str,
    lead_name: str | None,
    modelo: str | None,
    notes: str | None = None,
) -> dict:
    tz = ZoneInfo(settings.app_timezone)
    start = dtparser.isoparse(slot_iso).astimezone(tz)
    end = start + timedelta(minutes=settings.ghl_appointment_duration_min)
    title = f"Visita AMC — {lead_name or 'Lead'} — {modelo or 'sem modelo'}"
    payload = {
        "calendarId": settings.ghl_calendar_id,
        "locationId": settings.ghl_location_id,
        "contactId": contact_id,
        "startTime": start.isoformat(),
        "endTime": end.isoformat(),
        "title": title,
        "appointmentStatus": "confirmed",
    }
    if notes:
        payload["notes"] = notes

    client = get_client()
    log.info("calendar_book_attempt", contact_id=contact_id, slot=slot_iso, title=title)
    resp = await client.post(
        "/calendars/events/appointments",
        json=payload,
        operation="calendar.book",
    )
    log.info(
        "calendar_booked",
        contact_id=contact_id,
        appointment_id=(resp.get("appointment") or {}).get("id") or resp.get("id"),
    )
    return resp
