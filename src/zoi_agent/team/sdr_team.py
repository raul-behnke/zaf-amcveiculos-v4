"""SDR Team — Patricia (Team leader) + EstoqueExpert (member), mode=coordinate.

Em Agno Teams Coordinate, o LEADER vive no próprio `Team` (model, instructions,
tools, output_schema). Patricia É o Team. EstoqueExpert é o member especialista.

Patricia delega ao EstoqueExpert quando precisa raciocinar sobre veículos.
Member retorna InventoryDecision; Team (Patricia) tece BubbleSequence final.

Uso (orchestrator):
    team = await build_sdr_team()
    result = await team.arun(input_payload_json)
    bubble_seq: BubbleSequence = result.content
"""
from __future__ import annotations

from agno.models.openai import OpenAIChat
from agno.team import Team, TeamMode

from zoi_agent.config import settings
from zoi_agent.team.inventory_expert import build_inventory_expert
from zoi_agent.team.patricia import PATRICIA_INSTRUCTIONS, consultar_faq
from zoi_agent.team.schemas import BubbleSequence


async def build_sdr_team() -> Team:
    """Constrói o SDR Team (Patricia leader + EstoqueExpert member).

    Constrói uma instância nova por turno: o EstoqueExpert carrega o snapshot
    atual do inventário no system prompt (cache TTL 5min cobre múltiplas
    construções sem custo GHL extra).
    """
    inventory_expert = await build_inventory_expert()

    return Team(
        name="SDR_AMC_Team",
        model=OpenAIChat(id=settings.openai_model_patricia),
        mode=TeamMode.coordinate,
        members=[inventory_expert],
        description=(
            "SDR conversacional da AMC Veículos (Joinville/SC). Patricia "
            "conduz qualificação de leads via WhatsApp; delega ao "
            "EstoqueExpert para tudo relacionado a veículos do pátio."
        ),
        instructions=PATRICIA_INSTRUCTIONS,
        tools=[consultar_faq],
        output_schema=BubbleSequence,
        respond_directly=False,
        markdown=False,
        telemetry=False,
    )
