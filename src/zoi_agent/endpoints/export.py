"""GET /export/events — transporte PULL HTTP do ZOI Performance Hub (contrato §5).

MESMO contrato HTTP da frota (ver hub/agent_export_template.py):
  GET /export/events?since=<cursor>&secret=<hmac>
  -> {"events": [<envelope canônico v1>...], "next_cursor": <id|null>, "count": N}

Auth DEDICADA (separada do webhook): secret = HMAC-SHA256(ZOI_EXPORT_SECRET, str(since)),
aceitando também o secret cru p/ compat. 401 se ZOI_EXPORT_SECRET não configurado
(endpoint NUNCA fica aberto). NÃO toca no webhook do GHL.

Diferença vs template da frota: o template usa psycopg SÍNCRONO; aqui usamos o
engine async já existente do app (SQLAlchemy) — mesmo contrato HTTP, sem bloquear
o event loop e sem nova dependência (psycopg). EXPORT_TABLE default = agent_events.
"""
from __future__ import annotations

import hashlib
import hmac

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from zoi_agent.config import settings
from zoi_agent.db.engine import get_engine

router = APIRouter()

_MAX = 5000


def _auth_ok(provided: str | None, since: int) -> bool:
    expected = settings.zoi_export_secret
    if not expected or not provided:
        return False
    h = hmac.new(expected.encode(), str(since).encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(provided, h) or hmac.compare_digest(provided, expected)


@router.get("/export/events", response_model=None)
async def export_events(
    request: Request,
    since: int = Query(0, ge=0),
    limit: int = Query(1000, ge=1),
) -> JSONResponse | dict:
    if not _auth_ok(request.query_params.get("secret"), since):
        return JSONResponse(status_code=401, content={"action": "unauthorized"})

    lim = max(1, min(int(limit), _MAX))
    table = settings.export_table  # default agent_events
    sql = text(
        f"SELECT id, event_id, schema_version, event_type, client, agent, "
        f"contact_id, conversation_id, occurred_at, payload "
        f"FROM {table} WHERE id > :since ORDER BY id ASC LIMIT :lim"
    )

    events: list[dict] = []
    next_cursor: int | None = None
    engine = get_engine()
    async with engine.connect() as conn:
        result = await conn.execute(sql, {"since": int(since), "lim": lim})
        for r in result.mappings():
            next_cursor = r["id"]
            occurred = r["occurred_at"]
            events.append(
                {
                    "event_id": r["event_id"],
                    "schema_version": r["schema_version"] or 1,
                    "event_type": r["event_type"],
                    "client": r["client"],
                    "agent": r["agent"],
                    "contact_id": r["contact_id"],
                    "conversation_id": r["conversation_id"],
                    "occurred_at": occurred.isoformat()
                    if hasattr(occurred, "isoformat")
                    else occurred,
                    "payload": r["payload"] or {},
                }
            )

    return {"events": events, "next_cursor": next_cursor, "count": len(events)}
