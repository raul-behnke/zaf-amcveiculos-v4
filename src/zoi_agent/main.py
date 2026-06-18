from contextlib import asynccontextmanager

from fastapi import FastAPI, Response

from zoi_agent import db
from zoi_agent.config import settings
from zoi_agent.logging import configure_logging, get_logger
from zoi_agent.metrics import render_metrics

configure_logging()
log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("app_starting", location_id=settings.ghl_location_id)
    try:
        ok = await db.ping()
        log.info("db_ping", ok=ok)
        if ok:
            from zoi_agent.db.sessions import init_schema

            await init_schema()
            log.info("db_schema_ready")
    except Exception as e:
        log.error("db_ping_failed", error=str(e))
    yield
    await db.close()
    log.info("app_stopped")


app = FastAPI(title="ZOI Agent — AMC Veículos", version="0.1.0", lifespan=lifespan)

from zoi_agent.endpoints.abandon import router as abandon_router  # noqa: E402
from zoi_agent.endpoints.export import router as export_router  # noqa: E402
from zoi_agent.endpoints.greet import router as greet_router  # noqa: E402
from zoi_agent.endpoints.inbound import router as inbound_router  # noqa: E402

app.include_router(greet_router)
app.include_router(inbound_router)
app.include_router(abandon_router)
app.include_router(export_router)


@app.get("/health")
async def health() -> dict:
    try:
        db_ok = await db.ping()
    except Exception:
        db_ok = False
    return {"status": "ok" if db_ok else "degraded", "db": db_ok}


@app.get("/metrics")
async def metrics() -> Response:
    body, content_type = render_metrics()
    return Response(content=body, media_type=content_type)


def run() -> None:
    import uvicorn

    uvicorn.run(
        "zoi_agent.main:app",
        host=settings.app_host,
        port=settings.app_port,
        log_config=None,
    )


if __name__ == "__main__":
    run()
