"""Smoke do updater: 6 cenários de conversa -> StateUpdate esperado."""
from __future__ import annotations

import asyncio
import json
import sys

from zoi_agent.agent.schemas import (
    Collected,
    SessionState,
    VeiculoOrigem,
)
from zoi_agent.agent.updater import merge_into_state, run_updater


def msg(direction: str, body: str, mtype: str = "SMS") -> dict:
    return {"direction": direction, "body": body, "messageType": mtype, "dateAdded": "2026-05-27T10:00:00Z"}


SCENARIOS: list[tuple[str, SessionState, list[dict], str]] = [
    (
        "C5 — extrai nome",
        SessionState(
            stage="abertura",
            greeted=True,
            veiculo_origem=VeiculoOrigem(texto="Renault Duster"),
        ),
        [
            msg("outbound", "Olá! Bem-vindo à AMC. Está procurando algum carro específico?"),
        ],
        "oi, me chamo Raul",
    ),
    (
        "C9 — dúvida operacional (financiamento)",
        SessionState(stage="descoberta", greeted=True, collected=Collected(nome="Raul")),
        [
            msg("outbound", "Show, Raul! Tem algum veículo em mente?"),
        ],
        "vocês financiam?",
    ),
    (
        "C13 — quer ver outros (regressão pra apresentacao)",
        SessionState(
            stage="fechamento",
            greeted=True,
            collected=Collected(
                nome="Raul",
                veiculo_interesse="Duster",
                veiculo_interesse_confirmado=True,
                intencao="compra_direta",
                possui_troca=False,
                motivo_compra_ou_troca="trabalho",
                forma_pagamento="financiado",
                cidade="Joinville",
                interesse_agendamento=True,
            ),
        ),
        [msg("outbound", "Posso te propor uns horários pra visita?")],
        "espera, quero ver outro carro antes",
    ),
    (
        "C14 — pedido humano 1ª vez (não fazer handoff ainda)",
        SessionState(stage="descoberta", greeted=True, collected=Collected(nome="Raul")),
        [msg("outbound", "Manda ver, em que posso ajudar?")],
        "posso falar com um vendedor?",
    ),
    (
        "C15 — pedido humano 2ª vez (handoff)",
        SessionState(
            stage="descoberta",
            greeted=True,
            humano_solicitado_count=1,
            collected=Collected(nome="Raul"),
        ),
        [
            msg("outbound", "Posso te adiantar bastante coisa antes de chamar o consultor, beleza?"),
        ],
        "não, quero falar com vendedor agora",
    ),
    (
        "C16 — opt-out explícito (handoff imediato)",
        SessionState(stage="descoberta", greeted=True),
        [msg("outbound", "Tudo bem, me conta o que está procurando?")],
        "para de me mandar mensagem, chega",
    ),
]


async def run() -> int:
    failures = 0
    for label, state, history, last in SCENARIOS:
        print(f"\n=== {label} ===")
        print(f"last: {last!r}")
        try:
            upd = await run_updater(history=history, state=state, last_message=last)
        except Exception as e:
            print(f"  FAIL: {e}")
            failures += 1
            continue
        new_state = merge_into_state(state, upd)
        print(json.dumps(
            {
                "stage": upd.stage,
                "intent": upd.intent,
                "intent_sec": upd.intent_secundario,
                "sentiment": upd.sentiment,
                "should_handoff": upd.should_handoff,
                "terminal_reason": upd.terminal_reason,
                "humano_delta": upd.humano_solicitado_count_delta,
                "humano_count_after": new_state.humano_solicitado_count,
                "missing_head": upd.missing[:3],
                "collected_nome": new_state.collected.nome,
                "next_action": upd.next_action,
            },
            indent=2, ensure_ascii=False,
        ))

    print(f"\n{'='*40}\nfailures: {failures}")
    return failures


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
