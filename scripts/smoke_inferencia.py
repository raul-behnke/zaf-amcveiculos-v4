"""C26/C27/C28 — inferência contextual do updater ao vivo.

C26: lead pergunta "aceitam troca?" -> updater seta intencao=troca + possui_troca=true
C27: lead diz "moro em Floripa" no meio de outra conversa -> cidade=Floripa
C28: lead diz hipotético "se eu trocasse, vocês avaliam?" -> NÃO infere intencao
"""
from __future__ import annotations

import asyncio
import json
import sys

from zoi_agent.agent.schemas import Collected, SessionState
from zoi_agent.agent.updater import merge_into_state, run_updater


def msg(d: str, b: str) -> dict:
    return {"direction": d, "body": b, "messageType": "SMS"}


SCENARIOS: list[tuple[str, SessionState, list[dict], str, dict]] = [
    (
        "C26 — aceitam troca? -> infere intencao + possui_troca",
        SessionState(stage="descoberta", greeted=True,
                     collected=Collected(nome="Raul", veiculo_interesse="Montana 2019", veiculo_interesse_confirmado=True)),
        [msg("outbound", "Show, qual a sua intenção com a Montana?")],
        "Vocês aceitam troca?",
        {"intencao": "troca", "possui_troca": True},
    ),
    (
        "C27 — moro em Floripa no meio de outra conversa",
        SessionState(stage="descoberta", greeted=True,
                     collected=Collected(nome="Raul", veiculo_interesse="Montana 2019", veiculo_interesse_confirmado=True, intencao="compra_direta")),
        [msg("outbound", "Beleza! O que te motiva a comprar agora?")],
        "Tô precisando de um carro mais confortável, moro em Floripa e ando muito",
        {"cidade": "Floripa"},
    ),
    (
        "C28 — hipotético: 'se eu trocasse, vocês avaliam?' -> NÃO infere",
        SessionState(stage="descoberta", greeted=True,
                     collected=Collected(nome="Raul", veiculo_interesse="Montana 2019", veiculo_interesse_confirmado=True)),
        [msg("outbound", "Show, qual a sua intenção com a Montana?")],
        "Se eu trocasse, vocês avaliam meu carro?",
        # esperado: intencao=null e possui_troca=null (não inequívoco)
        {"intencao": None, "possui_troca": None},
    ),
]


async def main() -> int:
    fails = 0
    for label, state, history, last_msg, expect in SCENARIOS:
        print(f"\n=== {label} ===")
        print(f"  lead: {last_msg!r}")
        update = await run_updater(history=history, state=state, last_message=last_msg)
        merged = merge_into_state(state, update)
        c = merged.collected.model_dump()
        print(f"  next_action: {update.next_action!r}")
        print(f"  collected: { {k: v for k, v in c.items() if v not in (None, False, '', [])} }")
        for key, want in expect.items():
            got = c.get(key)
            ok = (got == want) if want is not None else (got in (None, "", False))
            mark = "✅" if ok else "❌"
            print(f"  {mark} {key}: esperado={want!r} obtido={got!r}")
            if not ok:
                fails += 1
    print(f"\nfails: {fails}")
    return fails


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
