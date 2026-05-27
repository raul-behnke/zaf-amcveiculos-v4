"""Schemas de StateUpdate e session_state conforme PLAN §6."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Stage = Literal["abertura", "descoberta", "apresentacao", "fechamento", "fechado"]
Sentiment = Literal["neutro", "positivo", "negativo", "irritado"]
Intent = Literal[
    "qualificar",
    "duvida",
    "opt_out",
    "pedido_humano",
    "agendamento",
    "apresentar",
]
IntentSecundario = Literal["duvida_operacional", "ver_outros_carros", "pedido_foto"] | None


class TrocaInfo(BaseModel):
    modelo: str | None = None
    ano: int | None = None
    km: int | None = None
    quitado: bool | None = None


class Collected(BaseModel):
    nome: str | None = None
    veiculo_interesse: str | None = None
    vehicle_focus_definido: bool = False
    intencao: Literal["compra_direta", "troca"] | None = None
    possui_troca: bool | None = None
    troca_completa: TrocaInfo | None = None
    motivo_compra_ou_troca: str | None = None
    forma_pagamento: Literal["a_vista", "financiado", "consorcio"] | None = None
    cidade: str | None = None
    interesse_agendamento: bool | None = None


class PreferenciaHorario(BaseModel):
    dia: str | None = None
    periodo: Literal["manha", "tarde", "noite"] | None = None


class StateUpdate(BaseModel):
    """Output estruturado do updater."""

    stage: Stage
    collected: Collected
    missing: list[str] = Field(
        description="Campos do funil ainda não preenchidos, em ordem PRIORITY"
    )
    next_action: str = Field(
        description="Próxima ação curta e operacional, ex: 'perguntar nome', 'apresentar matches'"
    )
    sentiment: Sentiment
    intent: Intent
    intent_secundario: IntentSecundario = None
    should_handoff: bool = False
    handoff_reason: str | None = None
    pode_handoff: bool = False
    terminal_reason: str | None = Field(
        default=None,
        description="Quando aplicável: qualificado_agendado, qualificado_sem_agenda, handoff_solicitado, handoff_erro",
    )
    preferencia_horario: PreferenciaHorario | None = None
    humano_solicitado_count_delta: int = Field(
        default=0,
        description="Incremento (0 ou 1) — usado pra atualizar contador na sessão",
    )
    ai_identity_asked_count_delta: int = Field(
        default=0, description="Incremento (0 ou 1)"
    )


class VeiculoOrigem(BaseModel):
    texto: str
    matches_external_ids: list[str] = Field(default_factory=list)


class SessionState(BaseModel):
    """Shape persistido em session_state JSONB."""

    stage: Stage = "abertura"
    greeted: bool = False
    veiculo_origem: VeiculoOrigem | None = None
    collected: Collected = Field(default_factory=Collected)
    vehicles_shown: list[str] = Field(default_factory=list)
    humano_solicitado_count: int = 0
    ai_identity_asked_count: int = 0
    last_sentiment: Sentiment = "neutro"
    last_intent: Intent = "qualificar"
    terminal_reason: str | None = None
    appointment: dict | None = None
    created_at: str | None = None
    updated_at: str | None = None


# ordem PRIORITY dos 10 campos (PLAN §4)
PRIORITY_FIELDS: tuple[str, ...] = (
    "nome",
    "veiculo_interesse",
    "vehicle_focus_definido",
    "intencao",
    "possui_troca",
    "troca_completa",
    "motivo_compra_ou_troca",
    "forma_pagamento",
    "cidade",
    "interesse_agendamento",
)


def compute_missing(c: Collected) -> list[str]:
    """Aplica ordem PRIORITY e exige troca_completa só se possui_troca=true."""
    miss: list[str] = []
    d = c.model_dump()
    for f in PRIORITY_FIELDS:
        if f == "vehicle_focus_definido":
            if not c.vehicle_focus_definido:
                miss.append(f)
            continue
        if f == "troca_completa":
            if c.possui_troca is True:
                t = c.troca_completa
                if not t or not all([t.modelo, t.ano, t.km, t.quitado is not None]):
                    miss.append(f)
            continue
        if d.get(f) in (None, ""):
            miss.append(f)
    return miss
