"""M5 评估与分析工具域：只读、口径敏感（与模型评估页 /evaluation 同源）。

本模块承载「SCAT 原始预测评估」工具：

- 单航班：RDP 分段与六类飞行阶段标注（物化优先、实时回退，点数有界）；
- 批次级：预测指标汇总（六档时域 × overall/phase/aircraft_type）与最差案例 Top-K。

口径与评估页一致（`scat-prediction-metrics-v2`）：对比对象是空管下发的飞行计划
航路（CFPL）与实飞轨迹，不是任何模型预测；与任务模型评估是两套独立体系。

按《Agent 拓扑与子代理接入规范》（计划 §5.3）：本域当前保持“工具”形态（确定性只读）；
当出现多步对比/解释需求（M6 自研模型对比）时，再评估是否升级为内部子代理。

工具只注册给 `scat-flight-agent`（见 `agents.scat_agent.aviation_tools`）。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Literal

from langchain_core.tools import BaseTool, tool

from agents.scat_tools import _execute_service_operation
from scat.materialization.tasks import phase_points_from_rows
from scat.preprocessing.phases import interval_phase_seconds, label_trajectory
from scat.repositories.flight_repository import ScatFlightRepository
from scat.schemas.tool_inputs import (
    DEFAULT_BATCH,
    FlightReferenceInput,
    PredictionMetricSummariesInput,
    WorstPredictionCasesInput,
)
from scat.services.materialization_service import MaterializationService

# 实时回退时单航班点数的上限（与 ScatFlightRepository 的投影上限一致）。
MAX_LIVE_POINTS = 20000


@tool(args_schema=FlightReferenceInput)
async def get_scat_flight_phase_segments(
    batch: str = DEFAULT_BATCH,
    external_id: str = "",
) -> dict[str, Any]:
    """查询一条 SCAT 航班的 RDP 分段与六类飞行阶段标注（论文口径）。

    阶段口径：先对整条轨迹做三维 ENU RDP 分段（容差 0.1 km），每段取 I062/200.vert 众数，
    再用该点的高度落六类：takeoff_initial_climb / climb / high_cruise / midlow_level /
    descent / approach。返回每段起止时间、点数、时长、段平均高度层、段垂直状态与段主导阶段，
    以及整条航班的六类秒数（按点区间归属，总和等于轨迹跨度）与主导阶段。

    数据来源：优先读取已物化的段级标注（scat_flight_phase_segments），缺失时按同一份实现
    实时计算（单航班、点数有界）。

    适用问题：
    - 这趟航班被分成几段、每段是什么阶段；
    - 巡航/下降/进场各持续多久；
    - 哪一段是转弯段、段平均高度多少。
    """
    normalized_external_id = str(external_id).strip()
    if not normalized_external_id:
        return {
            "status": "invalid_request",
            "operation": "get_scat_flight_phase_segments",
            "message": "必须提供 external_id。",
            "data": None,
        }

    async def operation() -> dict[str, Any]:
        service = MaterializationService()
        document = await service.get_flight_phase_segments(
            batch=batch,
            external_id=normalized_external_id,
        )
        if document is not None:
            return {
                "status": "ok",
                "query": {"batch": batch, "external_id": normalized_external_id},
                "data": {
                    "batch": batch,
                    "external_id": normalized_external_id,
                    "source": "materialized",
                    "point_count": int(document.get("point_count") or 0),
                    "segment_count": int(document.get("segment_count") or 0),
                    "dominant_phase": document.get("dominant_phase"),
                    "phase_seconds": document.get("phase_seconds") or {},
                    "segments": document.get("segments") or [],
                },
            }

        points = phase_points_from_rows(
            await ScatFlightRepository().get_trajectory_points_for_phases(
                batch=batch,
                external_id=normalized_external_id,
                limit=MAX_LIVE_POINTS,
            )
        )
        if not points:
            return {
                "status": "not_found",
                "query": {"batch": batch, "external_id": normalized_external_id},
                "message": f"批次 {batch} 中未找到航班 {normalized_external_id} 的有效轨迹点。",
                "data": None,
            }
        labels, segments = label_trajectory(points)
        return {
            "status": "ok",
            "query": {"batch": batch, "external_id": normalized_external_id},
            "data": {
                "batch": batch,
                "external_id": normalized_external_id,
                "source": "live",
                "point_count": len(points),
                "segment_count": len(segments),
                "dominant_phase": _dominant(labels),
                "phase_seconds": interval_phase_seconds(labels),
                "segments": [
                    {
                        "index": segment.index,
                        "start_time": segment.start_time,
                        "end_time": segment.end_time,
                        "point_count": segment.point_count,
                        "duration_s": segment.duration_s,
                        "mean_flight_level": segment.mean_flight_level,
                        "vertical_mode": segment.vertical_mode,
                        "phase": segment.phase,
                    }
                    for segment in segments
                ],
            },
        }

    return await _execute_service_operation(
        operation_name="get_scat_flight_phase_segments",
        operation=operation,
    )


def _dominant(labels: Iterable[Any]) -> str | None:
    from scat.preprocessing.phases import modal_phase

    return modal_phase(label.phase for label in labels)


@tool(args_schema=WorstPredictionCasesInput)
async def get_worst_prediction_cases(
    batch: str = DEFAULT_BATCH,
    metric: Literal["horizontal", "vertical", "along", "cross"] = "horizontal",
    horizon_s: float | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """查询某批次预测误差最大的航班样本（M5-07 最差案例检索）。

    误差口径：SCAT 原始预测（空管飞行计划航路 CFPL vs 实飞轨迹），与任务模型
    评估无关；数值体系与 get_prediction_metric_summaries 相同。

    按样本级误差倒序取 Top-K（每条 = 一个航班在一个快照、一个预测时域上的对齐结果），
    返回航班号、快照时刻、时域、水平/垂直/沿航向/横向误差，并附该航班的**主导阶段**与**机型**。

    - `metric`：排序用的误差维度（horizontal 默认 / vertical / along / cross）；
    - `horizon_s`：只查指定时域（60/180/300/600/900/1200 秒），缺省为全部时域；
    - `limit`：返回条数（≤100）。

    适用问题：
    - 这批预测里最差的是哪些航班/时刻；
    - 哪些飞行阶段的误差最大；
    - 最差案例的具体误差有多大（用于事后复盘）。
    """
    field = {
        "horizontal": "horizontal_error_m",
        "vertical": "vertical_error_m",
        "along": "along_track_error_m",
        "cross": "cross_track_error_m",
    }[metric]

    async def operation() -> dict[str, Any]:
        service = MaterializationService()
        data = await service.list_worst_prediction_samples(
            batch=batch,
            metric=field,
            horizon_s=horizon_s,
            limit=limit,
        )
        return {
            "status": "ok",
            "operation": "get_worst_prediction_cases",
            "query": {"batch": batch, "metric": field, "horizon_s": horizon_s, "limit": limit},
            "data": data,
        }

    return await _execute_service_operation(
        operation_name="get_worst_prediction_cases",
        operation=operation,
    )


@tool(args_schema=PredictionMetricSummariesInput)
async def get_prediction_metric_summaries(
    batch: str = DEFAULT_BATCH,
    group_by: Literal["overall", "phase", "aircraft_type"] = "overall",
    horizon_s: float | None = None,
    limit: int = 500,
) -> dict[str, Any]:
    """查询 **SCAT 原始预测** 的批次级评估汇总（M5 冻结口径，与模型评估页同源）。

    对比对象是空管系统下发的**飞行计划航路（CFPL）与实飞轨迹**——**不是任何模型
    预测**；指标版本 `scat-prediction-metrics-v2`（冻结于 `docs/PREDICTION_EVALUATION.md`），
    与评估页（/evaluation）展示同一份物化汇总、可按样本复核。

    注意口径区分：本工具与“任务模型（GBAR 等）的预测评估”是两套独立体系（数据、
    工具、数值均不同），不得混用或直接对比；回答时须说明这是“计划 vs 实飞”口径。

    返回指定分组维度下每个预测时域的汇总行：样本量、覆盖航班、水平/垂直 MAE 与 RMSE、
    沿航向/横向 MAE，以及 overall 分组的中位数/P90/P95 分位数。

    - `group_by`：overall（整批次，默认）/ phase（按航段主导阶段）/
      aircraft_type（按机型）；分组汇总一律带样本量；
    - `horizon_s`：只看某一档时域（秒）；缺省返回六档（60/180/300/600/900/1200 秒）；
    - `limit`：返回条数（≤500；一次取全部分组建议用默认值）。

    适用问题：
    - SCAT 原始预测在六档时域的整体误差水平（MAE/中位数/P95）与样本量；
    - 哪个飞行阶段或机型误差更大（分组对比，均带样本量）；
    - 评估页 KPI 的可解释、可复核数字。
    """

    async def operation() -> dict[str, Any]:
        service = MaterializationService()
        data = await service.list_metric_summaries(
            batch=batch,
            group_by=group_by,
            horizon_s=horizon_s,
            limit=limit,
        )
        return {
            "status": "ok",
            "operation": "get_prediction_metric_summaries",
            "query": {
                "batch": batch,
                "group_by": group_by,
                "horizon_s": horizon_s,
                "limit": limit,
            },
            "data": data,
        }

    return await _execute_service_operation(
        operation_name="get_prediction_metric_summaries",
        operation=operation,
    )


eval_tools: list[BaseTool] = [
    get_scat_flight_phase_segments,
    get_worst_prediction_cases,
    get_prediction_metric_summaries,
]

__all__ = [
    "MAX_LIVE_POINTS",
    "eval_tools",
    "get_prediction_metric_summaries",
    "get_scat_flight_phase_segments",
    "get_worst_prediction_cases",
]
