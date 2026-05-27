"""Smoke responder ao vivo: 5 cenários representativos."""
from __future__ import annotations

import asyncio
import sys

from zoi_agent.agent.responder import run_responder
from zoi_agent.agent.schemas import (
    Collected,
    SessionState,
    StateUpdate,
    VeiculoOrigem,
)


def msg(direction: str, body: str) -> dict:
    return {"direction": direction, "body": body, "messageType": "SMS"}


async def scenario_c5_extrai_nome() -> None:
    state = SessionState(
        stage="abertura", greeted=True,
        veiculo_origem=VeiculoOrigem(texto="Renault Duster"),
    )
    update = StateUpdate(
        stage="descoberta",
        collected=Collected(nome="Raul"),
        missing=["veiculo_interesse", "veiculo_interesse_confirmado", "intencao"],
        next_action="confirmar interesse no Duster e perguntar mais detalhes",
        sentiment="neutro",
        intent="qualificar",
    )
    history = [msg("outbound", "Olá! Bem-vindo à AMC. Está procurando algum carro específico?")]
    bubbles = await run_responder(
        state=state, update=update, history=history,
        last_message="oi, me chamo Raul",
    )
    print("\n=== C5: extrai nome ===")
    for b in bubbles:
        print(f"  > {b}")


async def scenario_c6_apresentacao() -> None:
    state = SessionState(
        stage="apresentacao", greeted=True,
        collected=Collected(nome="Raul"),
    )
    update = StateUpdate(
        stage="apresentacao",
        collected=state.collected,
        missing=["veiculo_interesse_confirmado", "intencao"],
        next_action="apresentar matches e perguntar qual interessou",
        sentiment="neutro",
        intent="apresentar",
        intent_secundario="ver_outros_carros",
    )
    history = [msg("inbound", "tem SUV automático até 80 mil?")]
    tool_outputs = {
        "search_results": {
            "exatos": [
                {"titulo": "Ford EcoSport Freestyle 1.6 Aut.", "ano": 2017, "preco": 62900, "quilometragem": 78000, "cambio": "Automático", "external_id": "1506455"},
                {"titulo": "Jeep Renegade Longitude 1.8 Aut.", "ano": 2016, "preco": 63900, "quilometragem": 90000, "cambio": "Automático", "external_id": "1478803"},
            ],
            "parecidos": [],
        }
    }
    bubbles = await run_responder(
        state=state, update=update, history=history,
        last_message="tem SUV automático até 80 mil?",
        tool_outputs=tool_outputs,
    )
    print("\n=== C6: apresentação ===")
    for b in bubbles:
        print(f"  > {b}")


async def scenario_c9_faq() -> None:
    state = SessionState(stage="descoberta", greeted=True, collected=Collected(nome="Raul"))
    update = StateUpdate(
        stage="descoberta",
        collected=state.collected,
        missing=["veiculo_interesse", "intencao"],
        next_action="responder sobre financiamento e perguntar veículo de interesse",
        sentiment="neutro",
        intent="duvida",
        intent_secundario="duvida_operacional",
    )
    history = [msg("outbound", "Show, Raul! Tem algum veículo em mente?")]
    tool_outputs = {
        "faq_yaml": """\
faq:
  blocos:
    - titulo: Formas de Pagamento
      itens:
        - pergunta: Aceitam financiamento?
          resposta: Sim, aceitamos.
        - pergunta: Qual a entrada mínima?
          resposta: A entrada varia de acordo com o CPF, podendo financiar até 100%.
""",
    }
    bubbles = await run_responder(
        state=state, update=update, history=history,
        last_message="vocês financiam?",
        tool_outputs=tool_outputs,
    )
    print("\n=== C9: FAQ financiamento ===")
    for b in bubbles:
        print(f"  > {b}")


async def scenario_c14_humano_1a() -> None:
    state = SessionState(
        stage="descoberta", greeted=True,
        collected=Collected(nome="Raul"),
        humano_solicitado_count=0,
    )
    update = StateUpdate(
        stage="descoberta",
        collected=state.collected,
        missing=["veiculo_interesse", "intencao"],
        next_action="insistir 1x e seguir qualificando",
        sentiment="neutro",
        intent="pedido_humano",
        humano_solicitado_count_delta=1,
    )
    history = [msg("outbound", "Manda ver, em que posso ajudar?")]
    bubbles = await run_responder(
        state=state, update=update, history=history,
        last_message="posso falar com um vendedor?",
    )
    print("\n=== C14: humano 1ª vez (insistir) ===")
    for b in bubbles:
        print(f"  > {b}")


async def scenario_c17_identidade() -> None:
    state = SessionState(
        stage="descoberta", greeted=True,
        collected=Collected(nome="Raul"),
        ai_identity_asked_count=0,
    )
    update = StateUpdate(
        stage="descoberta",
        collected=state.collected,
        missing=["veiculo_interesse"],
        next_action="evadir e seguir qualificando",
        sentiment="neutro",
        intent="duvida",
        ai_identity_asked_count_delta=1,
    )
    history = [msg("outbound", "Show, Raul! Tem algum veículo em mente?")]
    bubbles = await run_responder(
        state=state, update=update, history=history,
        last_message="você é um robô?",
    )
    print("\n=== C17: identidade 1ª vez (evasivo) ===")
    for b in bubbles:
        print(f"  > {b}")


async def main() -> int:
    await scenario_c5_extrai_nome()
    await scenario_c6_apresentacao()
    await scenario_c9_faq()
    await scenario_c14_humano_1a()
    await scenario_c17_identidade()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
