"""FastAPI application factory. Run with: uvicorn reviewer.main:create_app --factory"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool

from reviewer.api.health import router as health_router
from reviewer.api.runs import router as runs_router
from reviewer.api.webhooks import router as webhooks_router
from reviewer.config import get_settings
from reviewer.logging import configure_logging
from reviewer.services.sweep import sweep_stuck_runs


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await run_in_threadpool(sweep_stuck_runs)  # never raises, even if the DB is down
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(title="AI Code Review Agent", version="0.1.0", lifespan=lifespan)
    app.include_router(health_router)
    app.include_router(webhooks_router)
    app.include_router(runs_router)
    return app
