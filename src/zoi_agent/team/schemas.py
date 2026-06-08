"""Schemas do Team Agno — InventoryDecision (EstoqueExpert) e BubbleSequence (Patricia).

Separado de agent/schemas.py (que contém o StateUpdate do Updater, intacto).
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


# --- EstoqueExpert output ---------------------------------------------------


class VeiculoSelecionado(BaseModel):
    """Um veículo escolhido pelo EstoqueExpert, com motivo individual."""

    external_id: str = Field(description="external_id do veículo no inventário")
    motivo_individual: str = Field(
        description=(
            "Por que esse veículo encaixa NESTE turno, dado o contexto da "
            "conversa (1 frase curta)."
        )
    )


InventoryAction = Literal[
    "mostrar_card_unico",        # 1 veículo, template de card cheio
    "mostrar_card_lista",         # 2-3 veículos, template de lista
    "comentar_em_texto",          # cita veículo(s) em prosa, SEM card determinístico
    "perguntar_refinamento",      # falta info pra recomendar bem; pergunta antes de mostrar
    "nao_mostrar",                # turno não pede estoque (lead em outro assunto)
]


class InventoryDecision(BaseModel):
    """Output estruturado do EstoqueExpert.

    Patricia (leader) recebe essa decisão e tece bolhas finais. Orchestrator
    renderiza cards via templates.py quando action começa com mostrar_card_*.
    """

    action: InventoryAction = Field(
        description=(
            "Decisão de formato:\n"
            "- mostrar_card_unico: 1 veículo, card cheio (template).\n"
            "- mostrar_card_lista: 2-3 veículos, lista (template).\n"
            "- comentar_em_texto: já mostrou antes, ou lead pediu spec específica — "
            "responde em prosa sem repetir card.\n"
            "- perguntar_refinamento: pedido amplo + muitos candidatos — pergunta antes.\n"
            "- nao_mostrar: turno NÃO pede estoque (lead em FAQ, agenda, qualificação)."
        ),
    )
    veiculos_selecionados: list[VeiculoSelecionado] = Field(
        default_factory=list,
        description=(
            "Veículos escolhidos. SEMPRE preenchido em mostrar_card_* e "
            "comentar_em_texto. Vazio em perguntar_refinamento e nao_mostrar."
        ),
    )
    pergunta_refinamento: str | None = Field(
        default=None,
        description=(
            "Pergunta CURTA de refinamento (pt-BR) quando action=perguntar_refinamento. "
            "Patricia veste persona em cima. Ex: 'Câmbio automático ou manual?'"
        ),
    )
    hint_narrativo: str | None = Field(
        default=None,
        description=(
            "Ângulo de venda / tom pra Patricia incorporar. "
            "Ex: 'lead falou em fretes — posicione por robustez e economia'."
        ),
    )
    texto_sugerido_apresentacao: str | None = Field(
        default=None,
        description=(
            "Rascunho de bolha narrativa pra Patricia vestir persona. Opcional. "
            "Patricia pode reescrever, mas mantém o sentido."
        ),
    )
    motivo_geral: str = Field(
        description=(
            "Raciocínio interno do EstoqueExpert (pra logs/debug). NÃO vai pro lead."
        ),
    )


# --- Patricia output --------------------------------------------------------


class BubbleSequence(BaseModel):
    """Output estruturado da Patricia (Team leader).

    Orchestrator monta bolhas finais:
        [abertura?, cards_renderizados_pelo_template?, fechamento]
    """

    abertura: str | None = Field(
        default=None,
        description=(
            "Bolha de abertura/acknowledgment ANTES de cards. None se não precisa. "
            "Ex: 'Boa, frete pede veículo robusto. Olha o que separei pra você:'"
        ),
    )
    fechamento: str = Field(
        description=(
            "Bolha final OBRIGATÓRIA. Geralmente é a pergunta de funil "
            "(NextQuestion.canonical_text vestida de persona) OU pergunta de foco "
            "OU pergunta de refinamento (quando InventoryDecision.action=perguntar_refinamento)."
        ),
    )
