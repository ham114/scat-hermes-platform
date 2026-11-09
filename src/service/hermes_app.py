"""Separate single-user FastAPI entrypoint for Hermes and deterministic business APIs."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request, Response
from pydantic import SecretStr

from application.context import ExecutionContext
from core import settings
from core.settings import AnalysisIdentityMode
from hermes_runtime.client import HermesHTTPRuntime
from hermes_runtime.registry import RunRegistry
from hermes_runtime.results import ToolResultStore
from service.analysis_api import router as analysis_router
from service.auth import verify_bearer
from service.errors import install_exception_handlers
from service.hermes_api import router as hermes_router
from service.materialization_api import router as materialization_router
from service.model_task_api import router as model_task_router
from service.request_context import RequestContextMiddleware
from service.request_limits import RequestLimitsMiddleware
from service.scat_api import router as scat_router
from service.scat_only import lifespan as business_lifespan
from service.uav_api import router as uav_router
from service.workflow_api import router as workflow_router

ROOT = Path(__file__).resolve().parents[2]


def project_path(value: str) -> Path:
    path = (ROOT / value).resolve()
    if path == ROOT or not path.is_relative_to(ROOT):
        raise ValueError("Hermes platform files must remain inside the project")
    return path


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.ANALYSIS_IDENTITY_MODE == AnalysisIdentityMode.SINGLE_USER:
        ExecutionContext(settings.ANALYSIS_SINGLE_USER_ID, "configuration-check")
    elif settings.ANALYSIS_IDENTITY_MODE == AnalysisIdentityMode.TRUSTED_PROXY:
        from hermes_runtime.portal_identity import PortalDirectory

        if not settings.PORTAL_IDENTITIES_FILE or not settings.PORTAL_SIGNING_KEY_FILE:
            raise RuntimeError("Hermes requires configured portal identity assertions")
        PortalDirectory(ROOT, settings.PORTAL_IDENTITIES_FILE, settings.PORTAL_SIGNING_KEY_FILE)
    else:
        raise RuntimeError("Hermes identity must be configured")
    key_file = project_path(settings.HERMES_API_KEY_FILE)
    state_file = project_path(settings.HERMES_STATE_FILE)
    try:
        key = SecretStr(key_file.read_text().strip())
    except OSError:
        raise RuntimeError("Hermes internal service key is not available") from None
    from hermes_runtime.autonomy_config import load_autonomy
    from hermes_runtime.honcho_memory import MemoryRuntime

    autonomy = load_autonomy(ROOT)
    memory = MemoryRuntime(ROOT, autonomy.memory) if autonomy and autonomy.memory.enabled else None
    runtime = HermesHTTPRuntime(
        settings.HERMES_BASE_URL,
        key,
        RunRegistry(
            state_file,
            workspace_root=ROOT,
            max_active_per_owner=settings.HERMES_MAX_ACTIVE_PER_USER,
            max_active_total=settings.HERMES_MAX_ACTIVE_TOTAL,
        ),
        results=ToolResultStore(
            project_path(settings.HERMES_RESULT_STATE_FILE), workspace_root=ROOT
        ),
        memory=memory,
    )
    from application.model_tasks import ModelTasks
    from application.task_store import TaskStore
    from scat_model_runtime.production import (
        configure_production_model_runtime,
        shutdown_production_model_runtime,
    )

    configure_production_model_runtime(settings)
    app.state.model_tasks = ModelTasks(
        TaskStore(project_path(settings.HERMES_TASK_STATE_FILE), workspace_root=ROOT)
    )
    if settings.HERMES_SKILL_JOBS_ENABLED or settings.HERMES_WORKFLOWS_ENABLED:
        import os

        from application.skill_job_factory import create_skill_tasks

        app.state.model_tasks = create_skill_tasks(
            ROOT,
            project_path(settings.HERMES_TASK_STATE_FILE),
            cpu_fixture=os.environ.get("SCAT_S4_CPU_FIXTURE") == "1",
            workflows_enabled=settings.HERMES_WORKFLOWS_ENABLED,
        )
    app.state.hermes_runtime = runtime
    app.state.memory_runtime = memory
    if memory:
        memory.start()
    try:
        async with business_lifespan(app):
            yield
    finally:
        shutdown_production_model_runtime()
        app.state.model_tasks = None
        app.state.hermes_runtime = None
        app.state.memory_runtime = None
        if memory:
            await memory.aclose()
        await runtime.aclose()


app = FastAPI(title="SCAT Hermes Platform API", version="0.2.0", lifespan=lifespan)
app.add_middleware(
    RequestLimitsMiddleware,
    max_body_bytes=settings.REQUEST_MAX_BODY_BYTES,
    max_concurrency=settings.REQUEST_MAX_CONCURRENCY,
)
app.add_middleware(RequestContextMiddleware, slow_request_ms=settings.OBSERVABILITY_SLOW_REQUEST_MS)
from service.ownership import TrustedIdentityMiddleware

app.add_middleware(TrustedIdentityMiddleware)
install_exception_handlers(app)
for business_router in [analysis_router, materialization_router, scat_router, uav_router]:
    app.include_router(business_router, dependencies=[Depends(verify_bearer)])
app.include_router(hermes_router)
app.include_router(model_task_router)
app.include_router(workflow_router)
from service.memory_api import router as memory_router

app.include_router(memory_router)


@app.get("/health/live")
async def live():
    return {"status": "ok", "profile": "scat-hermes"}


@app.get("/health/ready")
async def ready(request: Request, response: Response):
    from scat.infrastructure.database import flight_database

    try:
        await flight_database.ping()
        database_ok = True
    except Exception:  # noqa: BLE001 -- health exposes only availability.
        database_ok = False
    runtime = getattr(request.app.state, "hermes_runtime", None)
    hermes_ok = runtime is not None and await runtime.healthy()
    configured = getattr(request.app.state, "configuration_status", None) == "valid"
    ok = database_ok and hermes_ok and configured
    if not ok:
        response.status_code = 503
    return {
        "status": "ok" if ok else "not_ready",
        "profile": "scat-hermes",
        "checks": {"mongodb": database_ok, "hermes": hermes_ok, "configuration": configured},
    }
