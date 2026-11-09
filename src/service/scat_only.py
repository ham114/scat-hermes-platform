"""Lightweight FastAPI entrypoint for SCAT data access without an LLM."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Response, status

from core import settings
from core.observability import configure_structured_logging
from core.redis_request_limiter import redis_request_limiter
from core.redis_runtime import redis_runtime
from scat.infrastructure.database import flight_database
from service.analysis_api import router as analysis_router
from service.auth import verify_bearer
from service.errors import install_exception_handlers
from service.materialization_api import router as materialization_router
from service.request_context import RequestContextMiddleware
from service.request_limits import RequestLimitsMiddleware
from service.runtime_validation import validate_scat_configuration
from service.scat_api import router as scat_router
from service.uav_api import router as uav_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    configure_structured_logging(
        level=settings.LOG_LEVEL.to_logging_level(),
        json_logs=settings.OBSERVABILITY_JSON_LOGS,
        replace_existing_formatters=True,
    )
    app.state.configuration_status = "checking"
    try:
        validate_scat_configuration(settings)
        app.state.configuration_status = "valid"
        try:
            await flight_database.connect()
            logger.info("SCAT MongoDB connected")
        except Exception:
            logger.exception("SCAT MongoDB was unavailable during startup")
        yield
    finally:
        await flight_database.close()
        await redis_runtime.close()


app = FastAPI(
    title="SCAT Deterministic Data API",
    version="1.0.0",
    lifespan=lifespan,
)
app.add_middleware(
    RequestLimitsMiddleware,
    max_body_bytes=settings.REQUEST_MAX_BODY_BYTES,
    max_concurrency=settings.REQUEST_MAX_CONCURRENCY,
    global_limiter=redis_request_limiter,
)
app.add_middleware(
    RequestContextMiddleware,
    slow_request_ms=settings.OBSERVABILITY_SLOW_REQUEST_MS,
    error_rate_min_requests=settings.OBSERVABILITY_ERROR_RATE_MIN_REQUESTS,
    error_rate_threshold=settings.OBSERVABILITY_ERROR_RATE_THRESHOLD,
    error_rate_window_seconds=settings.OBSERVABILITY_ERROR_RATE_WINDOW_SECONDS,
    alert_cooldown_seconds=settings.OBSERVABILITY_ALERT_COOLDOWN_SECONDS,
)
install_exception_handlers(app)
app.include_router(analysis_router, dependencies=[Depends(verify_bearer)])
app.include_router(materialization_router, dependencies=[Depends(verify_bearer)])
app.include_router(scat_router, dependencies=[Depends(verify_bearer)])
app.include_router(uav_router, dependencies=[Depends(verify_bearer)])


async def _database_status() -> str:
    try:
        await flight_database.ping()
    except Exception:
        logger.debug("SCAT MongoDB health check failed", exc_info=True)
        return "disconnected"
    return "connected"


@app.get("/health/live", tags=["Health"])
async def liveness_check() -> dict[str, str]:
    return {"status": "ok", "profile": "scat-only"}


@app.get("/health/ready", tags=["Health"])
async def readiness_check(response: Response) -> dict[str, Any]:
    database_status = await _database_status()
    configuration_status = getattr(app.state, "configuration_status", "unknown")
    ready = database_status == "connected" and configuration_status == "valid"
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "status": "ok" if ready else "not_ready",
        "profile": "scat-only",
        "checks": {
            "scat_mongodb": database_status,
            "configuration": configuration_status,
        },
    }
