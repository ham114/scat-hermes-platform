"""M6 自研预测工具域：单航班即时轨迹预测（确定性、只读、秒级）。

- 口径：自研预测器（`scat.prediction`，M6-01/04/05）基于**实飞观测**外推到目标时刻；
  与「SCAT 原始预测评估」（CFPL 计划航路 vs 实飞，M5 冻结口径）以及任务模型
  （GBAR，`model_tools`）评估是**不同体系**，回答时必须注明来源与预测器版本，
  数字不得混列或直接对比；
- 拓扑（计划 §5.3 / D-20）：本能力是确定性单步、秒级、结果可直接进对话 →
  保持“工具”形态，直接调用服务层，不经过任何子代理；批量实验/评估（分钟级、
  需实验台账）由 `experiment-agent` 承担（M6 后续接入）。
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import BaseTool, tool

from agents.scat_tools import _execute_service_operation
from scat.prediction.alignment import HORIZONS_S
from scat.prediction.registry import default_registry
from scat.schemas.tool_inputs import DEFAULT_BATCH, EmptyInput, PredictFlightTrajectoryInput
from scat.services.prediction_service import DEFAULT_PREDICTOR_ID, PredictionService


@tool(args_schema=EmptyInput)
async def list_prediction_models() -> dict[str, Any]:
    """列出可用于 SCAT 即时预测、批量实验和统一对比的自研预测器。"""

    items = [
        {
            "predictor_id": predictor_id,
            "predictor_version": predictor_version,
            "dataset": "scat",
            "capabilities": [
                "single_flight_prediction",
                "batch_experiment",
                "unified_model_comparison",
            ],
            "default_horizons_s": list(HORIZONS_S),
        }
        for predictor_id, predictor_version in default_registry.list_predictors()
    ]
    return {
        "status": "ok",
        "operation": "list_prediction_models",
        "data": {"count": len(items), "items": items},
    }


@tool(args_schema=PredictFlightTrajectoryInput)
async def predict_scat_flight_trajectory(
    batch: str = DEFAULT_BATCH,
    external_id: str = "",
    predictor_id: str = DEFAULT_PREDICTOR_ID,
    as_of: str | None = None,
    horizons_s: list[float] | None = None,
    compare_actual: bool = True,
) -> dict[str, Any]:
    """用自研预测器（M6，如 Kalman）基于实飞观测外推单航班未来轨迹。

    口径（重要）：这是**自研模型的即时外推预测**——输入为实飞轨迹（到 `as_of`，
    缺省=整条轨迹末点），输出为目标时刻的预测点；与「SCAT 原始预测评估」
    （CFPL 计划航路 vs 实飞）和「任务模型（GBAR）评估」是不同体系，数字不得
    混列或直接对比。回答时须注明预测器 id 与版本。

    - `predictor_id`：kalman-cv（默认，Kalman 常速度）/ constant-velocity-heading
      （匀速-恒航向）；
    - `as_of`：观测截止时刻（ISO 8601）。给定中途时刻=只看到该时刻为止的实飞数据，
      可给出与实飞真值的水平误差对照；缺省=从轨迹末点向后盲预测（无实飞对照）；
    - `horizons_s`：目标时域（秒，最多 6 档）；缺省 60/180/300/600/900/1200；
    - `compare_actual`：True（默认）时，目标时刻在实飞轨迹覆盖范围内即给出实际
      位置与水平误差（米）；超出范围则给出排除原因。

    适用问题：
    - 用 Kalman/匀速模型预测某航班未来 N 分钟的位置（给经纬度）；
    - 如果在中途某时刻"掐断"数据，模型能预测到哪、误差多大；
    - 自研基线预测与实飞轨迹的即时对照。
    """
    normalized_external_id = str(external_id).strip()
    if not normalized_external_id:
        return {
            "status": "invalid_request",
            "operation": "predict_scat_flight_trajectory",
            "message": "必须提供 external_id。",
            "data": None,
        }

    query = {
        "batch": batch,
        "external_id": normalized_external_id,
        "predictor_id": predictor_id,
        "as_of": as_of,
        "horizons_s": horizons_s,
        "compare_actual": compare_actual,
    }

    async def operation() -> dict[str, Any]:
        service = PredictionService()
        data = await service.predict_flight(
            batch=batch,
            external_id=normalized_external_id,
            predictor_id=predictor_id,
            as_of=as_of,
            horizons_s=horizons_s,
            compare_actual=compare_actual,
        )
        if data is None:
            return {
                "status": "not_found",
                "operation": "predict_scat_flight_trajectory",
                "query": query,
                "message": f"批次 {batch} 中未找到航班 {normalized_external_id} 的有效轨迹。",
                "data": None,
            }
        return {
            "status": "ok",
            "operation": "predict_scat_flight_trajectory",
            "query": query,
            "data": data,
        }

    return await _execute_service_operation(
        operation_name="predict_scat_flight_trajectory",
        operation=operation,
    )


prediction_tools: list[BaseTool] = [
    list_prediction_models,
    predict_scat_flight_trajectory,
]

__all__ = [
    "predict_scat_flight_trajectory",
    "list_prediction_models",
    "prediction_tools",
]
