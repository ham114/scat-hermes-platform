"""M6-09 read endpoint: self-developed predictor experiments for the evaluation page.

数据来源：`scat_model_experiments`（实验台账，M6-06/07）；口径 `m6-eval-v1`——
自研预测器基于实飞观测外推，与 CFPL 基准（scat-prediction-metrics-v2）以及任务模型
（GBAR）评估是相互独立的体系，页面并列展示时不得混算。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from scat.experiments.repository import ExperimentRepository
from scat.infrastructure.database import flight_database
from service.scat_api import BATCH_PATTERN, COMMON_ERROR_RESPONSES

router = APIRouter(
    prefix="/api/v1/scat/experiments",
    tags=["SCAT Experiments"],
    responses=COMMON_ERROR_RESPONSES,
)


def get_experiment_repository() -> ExperimentRepository:
    """FastAPI dependency hook that integration tests can override."""
    return ExperimentRepository(flight_database.db)


ExperimentReader = Annotated[ExperimentRepository, Depends(get_experiment_repository)]


@router.get(
    "/model-experiments",
    summary="List self-developed model experiments with six-horizon metrics (M6-09 evaluation page)",
)
async def list_model_experiments(
    repository: ExperimentReader,
    batch: Annotated[
        str | None,
        Query(min_length=1, max_length=200, pattern=BATCH_PATTERN),
    ] = None,
    predictor_id: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> Any:
    experiments = await repository.list_with_metrics(
        batch=batch,
        predictor_id=predictor_id,
        limit=limit,
    )
    return {
        "batch": batch,
        "predictor_id": predictor_id,
        "count": len(experiments),
        "experiments": experiments,
    }
