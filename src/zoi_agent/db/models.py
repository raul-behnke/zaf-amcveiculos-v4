from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Date, DateTime, Index, Integer, Numeric, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Session(Base):
    __tablename__ = "sessions"

    contact_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    state: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    terminal_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class AgentEvent(Base):
    """Tabela append-only de telemetria — fonte de verdade do ZOI Performance Hub.

    Envelope CANÔNICO v1 (CONTRATO_EVENTOS_CANONICO.md §2): event_id, schema_version,
    event_type, client, agent, contact_id, conversation_id, occurred_at, payload.
    NÃO sofre UPDATE/DELETE. Pull SQL incremental por cursor (id / occurred_at).
    Hub deduplica por `event_id`. Colunas de token/custo são denormalização do
    payload p/ agregação SQL eficiente (custo financeiro vive também no payload).
    """

    __tablename__ = "agent_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # --- Envelope canônico ---
    event_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    client: Mapped[str] = mapped_column(String(32), nullable=False)
    agent: Mapped[str] = mapped_column(String(32), nullable=False)
    contact_id: Mapped[str] = mapped_column(String(64), nullable=False)
    conversation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # --- Denormalização financeira (espelha payload p/ SQL) ---
    component: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model: Mapped[str | None] = mapped_column(String(40), nullable=True)
    tokens_input: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_output: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reasoning_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    cost_brl: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    usd_brl_rate: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    pricing_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # --- Insert time (≠ occurred_at) ---
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_agent_events_agent_occurred", "agent", "occurred_at"),
        Index("ix_agent_events_contact_occurred", "contact_id", "occurred_at"),
        Index("ix_agent_events_type_occurred", "event_type", "occurred_at"),
        Index("ix_agent_events_conv", "conversation_id"),
        Index("ix_agent_events_event_id", "event_id", unique=True),
    )


class Pricing(Base):
    """Preços por modelo+kind — forma CANÔNICA da frota (contrato §4).

    price_usd = USD por 1M tokens (kind input/output/reasoning) OU por minuto
    (kind=audio_minute). usd_brl_rate e pricing_version versionados junto.
    """

    __tablename__ = "pricing"

    model: Mapped[str] = mapped_column(String(40), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), primary_key=True)  # input|output|reasoning|audio_minute
    effective_from: Mapped[date] = mapped_column(
        Date, primary_key=True, default=date(2024, 1, 1)
    )
    price_usd: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    usd_brl_rate: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    pricing_version: Mapped[str] = mapped_column(String(32), nullable=False)
