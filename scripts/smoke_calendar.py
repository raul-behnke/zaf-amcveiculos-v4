"""Smoke C19/C20 ao vivo.

C19/parte 1: propose_slots ao vivo no calendário real.
C20: book_appointment NÃO é executado por padrão (cria evento real). Para testar:
     .venv/bin/python scripts/smoke_calendar.py --book  (use com cuidado)
"""
from __future__ import annotations

import asyncio
import sys

from zoi_agent.ghl.client import close_client
from zoi_agent.tools.calendar import book_appointment, propose_slots


async def main(do_book: bool) -> int:
    print("=== propose_slots (sem filtro) ===")
    slots = await propose_slots(limit=3)
    for s in slots:
        print(f"  - {s.iso}  | {s.label_pt()}")
    print()
    print("=== propose_slots periodo=manha amanhã ===")
    slots_m = await propose_slots(dia="amanhã", periodo="manha", limit=3)
    for s in slots_m:
        print(f"  - {s.iso}  | {s.label_pt()}")
    print()
    print("=== propose_slots periodo=tarde ===")
    slots_t = await propose_slots(periodo="tarde", limit=3)
    for s in slots_t:
        print(f"  - {s.iso}  | {s.label_pt()}")

    if do_book:
        if not slots:
            print("\nsem slots livres — pulando book")
            await close_client()
            return 0
        target = slots[0].iso
        print(f"\n=== book_appointment LIVE em {target} ===")
        resp = await book_appointment(
            contact_id="d9ILOnEyNkYhkIALa3wq",
            slot_iso=target,
            lead_name="Raul (smoke)",
            modelo="Renault Duster",
            notes="[ZOI smoke] qualificado_agendado",
        )
        print(f"resp: {resp}")

    await close_client()
    return 0


if __name__ == "__main__":
    do_book = "--book" in sys.argv
    sys.exit(asyncio.run(main(do_book)))
