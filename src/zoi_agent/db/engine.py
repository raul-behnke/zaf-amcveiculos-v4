from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from zoi_agent.config import settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(settings.database_url, pool_pre_ping=True, echo=False)
    return _engine


def get_session_factory() -> async_sessionmaker:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _session_factory


async def ping() -> bool:
    from sqlalchemy import text

    engine = get_engine()
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT 1"))
        return result.scalar() == 1


async def close() -> None:
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None
