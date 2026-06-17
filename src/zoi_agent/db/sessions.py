"""Repo de session_state (JSONB)."""
from __future__ import annotations

from zoi_agent.agent.schemas import SessionState
from zoi_agent.db.engine import get_session_factory
from zoi_agent.db.models import Session as SessionRow


async def init_schema() -> None:
    """Cria tabela se não existir. Chame no startup."""
    from zoi_agent.db.engine import get_engine
    from zoi_agent.db.models import Base

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Seed preços OpenAI (idempotente) — habilita cálculo de custo dos eventos.
    from zoi_agent.db.events import seed_pricing

    await seed_pricing()


async def load(contact_id: str) -> SessionState | None:
    factory = get_session_factory()
    async with factory() as s:
        row = await s.get(SessionRow, contact_id)
        if row is None:
            return None
        return SessionState(**row.state)


async def load_or_new(contact_id: str) -> SessionState:
    existing = await load(contact_id)
    return existing or SessionState()


async def save(contact_id: str, state: SessionState) -> None:
    factory = get_session_factory()
    async with factory() as s:
        async with s.begin():
            row = await s.get(SessionRow, contact_id)
            if row is None:
                row = SessionRow(
                    contact_id=contact_id,
                    state=state.model_dump(mode="json"),
                    terminal_reason=state.terminal_reason,
                )
                s.add(row)
            else:
                row.state = state.model_dump(mode="json")
                row.terminal_reason = state.terminal_reason


async def delete(contact_id: str) -> None:
    factory = get_session_factory()
    async with factory() as s:
        async with s.begin():
            row = await s.get(SessionRow, contact_id)
            if row is not None:
                await s.delete(row)
