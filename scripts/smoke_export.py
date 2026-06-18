"""Smoke fim-a-fim do GET /export/events (transporte do ZOI Performance Hub).

Valida o runbook DEPLOY_AMC.txt contra a URL pública APÓS o deploy:
  - 401 sem secret e com secret errado (endpoint nunca aberto)
  - 200 com HMAC-SHA256(ZOI_EXPORT_SECRET, str(since)) + shape {events,next_cursor,count}
  - 200 com secret cru (compat)
  - envelope canônico v1 em cada evento
  - LLM_CALL traz tokens crus + cost_brl + usd_brl_rate + pricing_version
  - avanço de cursor (since=next_cursor não estoura)

Uso:
  ZOI_EXPORT_SECRET=<hex32> .venv/bin/python scripts/smoke_export.py [BASE_URL]

  BASE_URL default = https://lucas-amc.appzoi.com.br
  Secret: env ZOI_EXPORT_SECRET (ou --secret <valor>).

Saída: relatório PASS/FAIL por checagem; exit code 0 só se tudo passou.
NÃO toca no webhook do GHL. Read-only.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import sys

import httpx

DEFAULT_BASE = "https://lucas-amc.appzoi.com.br"
TIMEOUT = 30

_ok = 0
_fail = 0


def _check(name: str, cond: bool, detail: str = "") -> None:
    global _ok, _fail
    mark = "PASS" if cond else "FAIL"
    if cond:
        _ok += 1
    else:
        _fail += 1
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))


def _sig(secret: str, since: int) -> str:
    return hmac.new(secret.encode(), str(since).encode(), hashlib.sha256).hexdigest()


def main(base_url: str, secret: str) -> int:
    base = base_url.rstrip("/")
    url = f"{base}/export/events"
    print(f"== smoke /export/events @ {base} ==")

    with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as cli:
        # 1) sem secret -> 401
        r = cli.get(url, params={"since": 0})
        _check("sem secret = 401", r.status_code == 401, f"got {r.status_code}")

        # 2) secret errado -> 401
        r = cli.get(url, params={"since": 0, "secret": "errado"})
        _check("secret errado = 401", r.status_code == 401, f"got {r.status_code}")

        # 3) HMAC(since=0) -> 200 + shape
        r = cli.get(url, params={"since": 0, "secret": _sig(secret, 0)})
        ok200 = r.status_code == 200
        _check("HMAC(secret, '0') = 200", ok200, f"got {r.status_code}")
        if not ok200:
            print("  abortando: sem 200 não dá pra validar envelope.")
            return _summary()
        body = r.json()
        _check(
            "shape {events,next_cursor,count}",
            all(k in body for k in ("events", "next_cursor", "count")),
            f"keys={list(body)}",
        )

        events = body.get("events") or []
        print(f"  -> {len(events)} evento(s), next_cursor={body.get('next_cursor')}")

        # 4) envelope canônico no 1º evento
        if events:
            e = events[0]
            req = ("event_id", "schema_version", "event_type", "client", "agent",
                   "contact_id", "occurred_at", "payload")
            _check("envelope: campos obrigatórios", all(k in e for k in req),
                   f"faltando={[k for k in req if k not in e]}")
            _check("schema_version == 1", e.get("schema_version") == 1, str(e.get("schema_version")))
            _check("client == 'amc'", e.get("client") == "amc", str(e.get("client")))
            _check("agent == 'patricia-amc'", e.get("agent") == "patricia-amc", str(e.get("agent")))

            # 5) custo no LLM_CALL (procura o 1º na página)
            llm = next((x for x in events if x.get("event_type") == "LLM_CALL"), None)
            if llm:
                p = llm.get("payload") or {}
                cost_ok = (
                    isinstance(p.get("cost_brl"), (int, float)) and p["cost_brl"] > 0
                    and "usd_brl_rate" in p and "pricing_version" in p
                    and "input_tokens" in p and "output_tokens" in p
                )
                _check("LLM_CALL: tokens crus + cost_brl>0 + rate + pricing_version",
                       cost_ok, f"payload_keys={list(p)}")
            else:
                print("  [skip] nenhum LLM_CALL nesta página (ok se ainda não houve turno)")
        else:
            print("  [skip] página vazia — sem eventos ainda (deploy novo). Envelope não verificável.")

        # 6) avanço de cursor
        nxt = body.get("next_cursor")
        if nxt is not None:
            r2 = cli.get(url, params={"since": nxt, "secret": _sig(secret, nxt)})
            _check("cursor avança (since=next_cursor = 200)", r2.status_code == 200, f"got {r2.status_code}")

        # 7) secret cru (compat)
        r3 = cli.get(url, params={"since": 0, "secret": secret})
        _check("secret cru aceito (compat) = 200", r3.status_code == 200, f"got {r3.status_code}")

    return _summary()


def _summary() -> int:
    print(f"\n== {_ok} PASS / {_fail} FAIL ==")
    return 0 if _fail == 0 else 1


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--secret"]
    base = args[0] if args and not args[0].startswith("-") else DEFAULT_BASE
    sec = os.getenv("ZOI_EXPORT_SECRET", "")
    if "--secret" in sys.argv:
        i = sys.argv.index("--secret")
        if i + 1 < len(sys.argv):
            sec = sys.argv[i + 1]
    if not sec:
        print("ERRO: defina ZOI_EXPORT_SECRET (env) ou --secret <valor>.")
        sys.exit(2)
    sys.exit(main(base, sec))
