"""Servidor minimal pra dumpar payload do GHL durante mapeamento (Sprint 9).

Uso:
    .venv/bin/python scripts/inspect_webhook.py
    # ou: PORT=9000 .venv/bin/python scripts/inspect_webhook.py

Em outra aba, ngrok aponta pro mesmo PORT (default 9001):
    ngrok http 9001

Configura workflow GHL "Inbound" pra POST:
    https://<NGROK_URL>/inspect?secret=<WEBHOOK_SECRET>

Cada POST grava:
    payloads/<timestamp>.json  (headers + json/body + query)
e ecoa um resumo no console.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request

OUT_DIR = Path(__file__).resolve().parent.parent / "payloads"
OUT_DIR.mkdir(exist_ok=True)

app = FastAPI(title="GHL webhook inspector")


@app.post("/inspect")
async def inspect(request: Request) -> dict:
    ts = time.strftime("%Y%m%d-%H%M%S")
    seq = int(time.time() * 1000) % 10_000_000

    headers = dict(request.headers)
    query = dict(request.query_params)
    raw = await request.body()
    try:
        body = json.loads(raw)
        body_kind = "json"
    except Exception:
        body = raw.decode(errors="replace")
        body_kind = "text"

    record = {
        "ts": ts,
        "method": request.method,
        "url": str(request.url),
        "headers": headers,
        "query": query,
        "body_kind": body_kind,
        "body": body,
    }
    out_path = OUT_DIR / f"{ts}-{seq}.json"
    out_path.write_text(json.dumps(record, indent=2, ensure_ascii=False))
    print(f"\n=== {ts} -> {out_path.name} ===")
    print(f"query: {query}")
    if body_kind == "json" and isinstance(body, dict):
        print(f"top-level keys: {list(body.keys())[:15]}")
        # destaca campos típicos
        for k in ("type", "messageType", "direction", "contactId", "body", "message", "attachments", "locationId", "conversationId"):
            if k in body:
                v = body[k]
                if isinstance(v, str) and len(v) > 120:
                    v = v[:120] + "..."
                print(f"  {k}: {v!r}")
    else:
        print(f"body (text, first 300): {str(body)[:300]!r}")
    return {"ok": True, "saved": out_path.name}


@app.get("/inspect/health")
async def health() -> dict:
    return {"status": "ok"}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "9001"))
    print(f"Inspector escutando em 0.0.0.0:{port}  (payloads -> {OUT_DIR})")
    sys.stdout.flush()
    uvicorn.run(app, host="0.0.0.0", port=port, log_config=None)
