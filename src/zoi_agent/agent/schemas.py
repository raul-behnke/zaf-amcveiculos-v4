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

# Tópicos múltiplos por turno — substituição estrutural ao intent_secundario.
# Lead pode tocar em vários assuntos numa mensagem só ("Quais horários? Qual
# endereço?" -> ["agendamento","duvida_operacional"]) e o orchestrator dispara
# uma ferramenta por tópico no mesmo turno.
Topic = Literal[
    "duvida_operacional",
    "agendamento",
    "ver_outros_carros",
    "pedido_foto",
]


class TrocaInfo(BaseModel):
    modelo: str | None = None
    ano: int | None = None
    km: int | None = None
    quitado: bool | None = None


class Collected(BaseModel):
    nome: str | None = None
    veiculo_interesse: str | None = None
    veiculo_interesse_confirmado: bool = False
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
    hora: str | None = None  # "HH:MM" quando lead dá horário explícito (ex: "10:00")


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
    topics: list[Topic] = Field(
        default_factory=list,
        description=(
            "Lista de TODOS os tópicos identificados na mensagem do lead nesta "
            "rodada — pode conter múltiplos. Ex: 'Quais horários posso passar? "
            "Qual o endereço?' -> ['agendamento','duvida_operacional']. NUNCA "
            "deixe vazio se houver qualquer dos tópicos canônicos."
        ),
    )
    should_handoff: bool = False
    handoff_reason: str | None = None
    pode_handoff: bool = False
    terminal_reason: str | None = Field(
        default=None,
        description="Quando aplicável: qualificado_agendado, qualificado_sem_agenda, handoff_solicitado, handoff_erro",
    )
    preferencia_horario: PreferenciaHorario | None = None
    chosen_slot_iso: str | None = Field(
        default=None,
        description="ISO8601 com offset. Preenchido SOMENTE quando o lead aceitou explicitamente um dos slots propostos no turno anterior.",
    )
    humano_solicitado_count_delta: int = Field(
        default=0,
        description="Incremento (0 ou 1) — usado pra atualizar contador na sessão",
    )
    ai_identity_asked_count_delta: int = Field(
        default=0, description="Incremento (0 ou 1)"
    )
    photo_target_external_id: str | None = Field(
        default=None,
        description=(
            "Quando intent_secundario=pedido_foto OU 'pedido_foto' está em topics: "
            "external_id do veículo cujas fotos devem ser enviadas, ESCOLHIDO "
            "estritamente da lista candidates fornecida no input. "
            "DEIXE NULL se: (1) intent não é pedido_foto, (2) lead não nomeou "
            "veículo e não há contexto claro, (3) dúvida real sobre qual alvo. "
            "PROIBIDO inventar ID que não esteja em candidates."
        ),
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
    origem_apresentada: bool = False
    last_asked_fields: list[str] = Field(default_factory=list)
    last_card_external_id: str | None = None
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
    "veiculo_interesse_confirmado",
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
        if f == "veiculo_interesse_confirmado":
            if not c.veiculo_interesse_confirmado:
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
