"""C25 — anti-repetição ao vivo (5 turnos seguidos).

Roda o responder real (gpt-4o) 5 vezes simulando diferentes momentos da conversa.
Após cada turno, alimenta o "history_recent" com as bolhas anteriores e checa que
nenhuma bolha nova bate >70% de similaridade (difflib.SequenceMatcher) com qualquer
bolha já enviada nos turnos anteriores.

Critério de aprovação: para cada par (nova bolha, bolha anterior do lucas),
ratio < 0.7. Tolerância 0.7 por causa de pequenas similaridades naturais (saudação
inicial vs frases curtas).
"""
from __future__ import annotations

import asyncio
import sys
from difflib import SequenceMatcher

from zoi_agent.agent.responder import run_responder
from zoi_agent.agent.schemas import (
    Collected,
    SessionState,
    StateUpdate,
    VeiculoOrigem,
)


def msg(direction: str, body: str) -> dict:
    return {"direction": direction, "body": body, "messageType": "SMS"}


SCENARIOS: list[tuple[str, SessionState, StateUpdate, str, dict]] = [
    (
        "turno 1: lead confirma interesse",
        SessionState(
            stage="abertura", greeted=True,
            veiculo_origem=VeiculoOrigem(texto="Chevrolet Montana"),
            origem_apresentada=False,
        ),
        StateUpdate(
            stage="abertura",
            collected=Collected(),
            missing=["veiculo_interesse"],
            next_action="apresentar matches da origem",
            sentiment="positivo",
            intent="apresentar",
        ),
        "Olá, pode sim",
        {
            "origem_matches": {
                "texto_origem": "Chevrolet Montana",
                "matches": {
                    "exatos": [
                        {"titulo": "Chevrolet Montana LT 1.4", "ano": 2019, "preco": 58900, "quilometragem": 95000, "cambio": "Manual", "external_id": "m-1"},
                        {"titulo": "Chevrolet Montana LS 1.4", "ano": 2017, "preco": 46900, "quilometragem": 120000, "cambio": "Manual", "external_id": "m-2"},
                    ],
                    "parecidos": [],
                },
            }
        },
    ),
    (
        "turno 2: lead engaja no 2019",
        SessionState(
            stage="descoberta", greeted=True,
            veiculo_origem=VeiculoOrigem(texto="Chevrolet Montana"),
            origem_apresentada=True,
            vehicles_shown=["m-1", "m-2"],
            collected=Collected(vehicle_focus_definido=True, veiculo_interesse="Montana 2019"),
        ),
        StateUpdate(
            stage="descoberta",
            collected=Collected(vehicle_focus_definido=True, veiculo_interesse="Montana 2019"),
            missing=["nome", "intencao"],
            next_action="perguntar nome",
            sentiment="positivo",
            intent="qualificar",
        ),
        "gostei do 2019",
        {},
    ),
    (
        "turno 3: lead diz nome",
        SessionState(
            stage="descoberta", greeted=True,
            collected=Collected(nome="Raul", vehicle_focus_definido=True, veiculo_interesse="Montana 2019"),
            origem_apresentada=True,
        ),
        StateUpdate(
            stage="descoberta",
            collected=Collected(nome="Raul", vehicle_focus_definido=True, veiculo_interesse="Montana 2019"),
            missing=["intencao"],
            next_action="perguntar intenção",
            sentiment="positivo",
            intent="qualificar",
        ),
        "Me chamo Raul",
        {},
    ),
    (
        "turno 4: lead diz troca",
        SessionState(
            stage="descoberta", greeted=True,
            collected=Collected(nome="Raul", vehicle_focus_definido=True, veiculo_interesse="Montana 2019", intencao="troca"),
            origem_apresentada=True,
        ),
        StateUpdate(
            stage="descoberta",
            collected=Collected(nome="Raul", vehicle_focus_definido=True, veiculo_interesse="Montana 2019", intencao="troca", possui_troca=True),
            missing=["troca_completa"],
            next_action="puxar detalhes da troca",
            sentiment="neutro",
            intent="qualificar",
        ),
        "Quero trocar meu Gol 2014",
        {},
    ),
    (
        "turno 5: lead complementa troca",
        SessionState(
            stage="descoberta", greeted=True,
            collected=Collected(nome="Raul", vehicle_focus_definido=True, veiculo_interesse="Montana 2019", intencao="troca", possui_troca=True),
            origem_apresentada=True,
        ),
        StateUpdate(
            stage="descoberta",
            collected=Collected(nome="Raul", vehicle_focus_definido=True, veiculo_interesse="Montana 2019", intencao="troca", possui_troca=True),
            missing=["motivo_compra_ou_troca"],
            next_action="puxar motivo",
            sentiment="neutro",
            intent="qualificar",
        ),
        "Tá com 80mil km e quitado",
        {},
    ),
]


def normalize(s: str) -> str:
    return " ".join(s.lower().split())


async def main() -> int:
    print(f"\n=== C25 anti-repetição (5 turnos, threshold 0.70) ===\n")
    history: list[dict] = [msg("outbound", "Olá! 👋 Bem-vindo à AMC Veículos. Vi que você demonstrou interesse no Chevrolet Montana 🚗. Posso te passar mais informações sobre ele?")]
    all_lucas_bubbles: list[str] = []
    fail_count = 0

    for label, state, update, last_msg, tools in SCENARIOS:
        history.append(msg("inbound", last_msg))
        bubbles = await run_responder(
            state=state, update=update, history=history,
            last_message=last_msg, tool_outputs=tools,
        )
        print(f"\n--- {label} ---")
        print(f"  lead: {last_msg!r}")
        for b in bubbles:
            print(f"  > {b}")
            history.append(msg("outbound", b))

        # Validação anti-repetição
        for new_b in bubbles:
            n = normalize(new_b)
            for old_b in all_lucas_bubbles:
                ratio = SequenceMatcher(None, n, normalize(old_b)).ratio()
                if ratio >= 0.70:
                    print(f"  ❌ similaridade {ratio:.2f}")
                    print(f"     atual:  {new_b!r}")
                    print(f"     prévia: {old_b!r}")
                    fail_count += 1

        # Validações de abertura com nome
        if state.collected.nome:
            for b in bubbles:
                first_word = b.lstrip().split(",")[0].split()[0] if b.strip() else ""
                if state.collected.nome.lower() in normalize(b).split(maxsplit=2)[:2]:
                    # nome aparece nas 2 primeiras palavras -> provável abertura proibida
                    # toleramos se não for a abertura (ex: "fechado, Raul")
                    if normalize(b).startswith(state.collected.nome.lower()) or normalize(b).startswith(f"opa, {state.collected.nome.lower()}") or normalize(b).startswith(f"show, {state.collected.nome.lower()}") or normalize(b).startswith(f"beleza, {state.collected.nome.lower()}"):
                        print(f"  ❌ abertura com nome detectada: {b!r}")
                        fail_count += 1

        all_lucas_bubbles.extend(bubbles)

    print(f"\n=== RESULTADO ===\n  falhas: {fail_count}")
    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
