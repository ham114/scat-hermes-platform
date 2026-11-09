"""将 UAV 查询服务封装为 Agent 可调用的白名单工具。"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from langchain_core.tools import BaseTool, tool
from pymongo.errors import PyMongoError

from agents.analysis_results import (
    attach_analysis_result,
    build_uav_channel_stats_analysis,
    build_uav_flight_overview_analysis,
    build_uav_flight_search_analysis,
    build_uav_flight_phase_stats_analysis,
    build_uav_model_analysis,
    build_uav_normal_vs_anomaly_analysis,
    build_uav_samples_analysis,
)
from scat.infrastructure.database import flight_database
from uav.schemas.tool_inputs import (
    UavChannelStatsInput,
    UavEmptyInput,
    UavFlightReferenceInput,
    UavNormalVsAnomalyInput,
    UavSamplesInput,
    UavSearchFlightsInput,
)
from uav.services.uav_service import UavFlightService

logger = logging.getLogger(__name__)
uav_flight_service = UavFlightService()


async def _execute_uav_operation(
    *,
    operation_name: str,
    operation: Callable[[], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    """建立共享 MongoDB 连接并统一隐藏内部错误细节。"""
    try:
        await flight_database.connect()
        return await operation()
    except ValueError as exc:
        return {
            "status": "invalid_request",
            "operation": operation_name,
            "message": str(exc),
            "data": None,
        }
    except PyMongoError:
        logger.exception("MongoDB error while executing UAV operation: %s", operation_name)
        return {
            "status": "database_error",
            "operation": operation_name,
            "message": "UAV 数据库查询失败。请稍后重试或检查数据库服务状态。",
            "data": None,
        }
    except Exception:
        logger.exception("Unexpected error while executing UAV operation: %s", operation_name)
        return {
            "status": "internal_error",
            "operation": operation_name,
            "message": "执行 UAV 业务操作时发生未预期错误。",
            "data": None,
        }


@tool(args_schema=UavEmptyInput)
async def list_uav_models() -> dict[str, Any]:
    """列出民用固定翼 UAV 数据中的机型、架次数量和起飞时间范围。

    当用户询问 UAV 数据规模、有哪些机型、每种机型有多少架次时使用。
    返回全部机型的轻量聚合结果，不返回传感器样本。
    """
    result = await _execute_uav_operation(
        operation_name="list_uav_models",
        operation=uav_flight_service.list_models,
    )
    return attach_analysis_result(result, build_uav_model_analysis(result))


@tool(args_schema=UavSearchFlightsInput)
async def search_uav_flights(
    model: str | None = None,
    tail_number: str | None = None,
    mission: str | None = None,
    flight_kind: str | None = None,
    label: str | None = None,
    takeoff_start: datetime | None = None,
    takeoff_end: datetime | None = None,
    sort_by: str = "takeoff_at",
    sort_order: int = 1,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    """按机型、机尾号、任务、样本类型、标签和起飞时间搜索 UAV 架次。

    所有条件采用 AND 关系。结果仅包含架次摘要，最多返回 100 条。
    """
    result = await _execute_uav_operation(
        operation_name="search_uav_flights",
        operation=lambda: uav_flight_service.list_flights(
            model=model,
            tail_number=tail_number,
            mission=mission,
            flight_kind=flight_kind,
            label=label,
            start_time=takeoff_start,
            end_time=takeoff_end,
            sort_by=sort_by,
            sort_order=sort_order,
            limit=limit,
            offset=offset,
        ),
    )
    return attach_analysis_result(result, build_uav_flight_search_analysis(result))


@tool(args_schema=UavFlightReferenceInput)
async def get_uav_flight_overview(external_id: str) -> dict[str, Any]:
    """查询单个 UAV 架次的身份信息、数据质量和 34 通道统计摘要。

    返回高度、地速、位置边界以及各通道的最小值、最大值、平均值和缺失数，
    不返回完整 samples 数组。统计分析应优先使用本工具。
    """
    result = await _execute_uav_operation(
        operation_name="get_uav_flight_overview",
        operation=lambda: uav_flight_service.get_flight(external_id),
    )
    return attach_analysis_result(result, build_uav_flight_overview_analysis(result))


@tool(args_schema=UavEmptyInput)
async def list_uav_channels() -> dict[str, Any]:
    """列出 UAV 数据集 34 个传感器通道的键名、中文名、单位和通道组。"""

    async def operation() -> dict[str, Any]:
        return uav_flight_service.list_channels()

    return await _execute_uav_operation(
        operation_name="list_uav_channels",
        operation=operation,
    )


@tool(args_schema=UavSamplesInput)
async def get_uav_samples(
    external_id: str,
    start: int = 0,
    limit: int = 100,
    every_nth: int = 1,
    fields: set[str] | None = None,
    channels: set[str] | None = None,
) -> dict[str, Any]:
    """分页、降采样读取单个 UAV 架次的传感器样本。

    仅在用户明确要求查看时序值、绘图数据或模型输入时使用。普通统计查询应先
    使用 get_uav_flight_overview，避免把大量传感器点放进模型上下文。fields
    选择通道组，channels 可进一步选择精确通道键。

    用户要求二维飞行轨迹图时，使用 fields=["position"]，常用 channels=[
    "cmp_comb_lon", "cmp_comb_lat", "cmp_comb_alt"]（组合高度用于着色）；
    返回结果会自动渲染二维轨迹图（经纬度连线）。
    """
    result = await _execute_uav_operation(
        operation_name="get_uav_samples",
        operation=lambda: uav_flight_service.get_samples(
            external_id,
            start=start,
            limit=limit,
            every_nth=every_nth,
            fields=fields,
            channels=channels,
        ),
    )
    return attach_analysis_result(result, build_uav_samples_analysis(result))


@tool(args_schema=UavChannelStatsInput)
async def get_uav_channel_stats(
    channels: set[str],
    model: str | None = None,
    flight_kind: str | None = None,
) -> dict[str, Any]:
    """跨架次聚合指定 UAV 传感器通道的统计。

    适用问题：
    - 某个通道（如组合高度、俯仰角、地速）在所有架次/某机型架次上的
      最小值、最大值、加权平均值和缺失数；
    - 正常架次与异常架次分开的通道总体水平。

    加权均值 = Σ(架次均值×该架次样本数)/Σ样本数；一次最多统计 6 个通道，
    通道键以 list_uav_channels 返回为准。不含标准差。
    """
    result = await _execute_uav_operation(
        operation_name="get_uav_channel_stats",
        operation=lambda: uav_flight_service.get_channel_stats(
            channels=channels,
            model=model,
            flight_kind=flight_kind,
        ),
    )
    return attach_analysis_result(result, build_uav_channel_stats_analysis(result))


@tool(args_schema=UavNormalVsAnomalyInput)
async def get_uav_normal_vs_anomaly_stats(
    channels: set[str],
    model: str | None = None,
) -> dict[str, Any]:
    """对比正常与异常样本架次在指定通道上的统计差异。

    适用问题：
    - 姿态异常/高度异常/轨迹偏离样本与正常架次在哪些通道上差异明显；
    - 异常样本的某通道均值相对正常架次高/低多少。

    返回每个通道的正常组与异常组的架次数、加权均值、极值和差值；
    一次最多对比 6 个通道。异常样本机型为 sample，指定 model 时注意
    正常组与异常组可能来自不同机型。
    """
    result = await _execute_uav_operation(
        operation_name="get_uav_normal_vs_anomaly_stats",
        operation=lambda: uav_flight_service.get_normal_vs_anomaly_stats(
            channels=channels,
            model=model,
        ),
    )
    return attach_analysis_result(
        result,
        build_uav_normal_vs_anomaly_analysis(result),
    )


@tool(args_schema=UavFlightReferenceInput)
async def get_uav_phase_stats(external_id: str) -> dict[str, Any]:
    """查询单个 UAV 架次的爬升/平飞/下降阶段统计。

    阶段划分基于组合天向速度 cmp_comb_vu：> +1.0 m/s 爬升，
    < -1.0 m/s 下降，其余平飞。每个阶段返回：
    - 样本数与占比、开始/结束相对时间与持续时长；
    - 高度的最小/平均/最大值；
    - 平均/最大地速与平均天向速度。

    适用问题：
    - 这个架次爬升/平飞/下降各持续多久；
    - 爬升阶段的高度变化和速度范围。
    """
    result = await _execute_uav_operation(
        operation_name="get_uav_phase_stats",
        operation=lambda: uav_flight_service.get_flight_phase_stats(
            external_id,
        ),
    )
    return attach_analysis_result(
        result,
        build_uav_flight_phase_stats_analysis(result),
    )


uav_tools: list[BaseTool] = [
    list_uav_models,
    search_uav_flights,
    get_uav_flight_overview,
    list_uav_channels,
    get_uav_samples,
    get_uav_channel_stats,
    get_uav_normal_vs_anomaly_stats,
    get_uav_phase_stats,
]

__all__ = [
    "get_uav_channel_stats",
    "get_uav_flight_overview",
    "get_uav_normal_vs_anomaly_stats",
    "get_uav_phase_stats",
    "get_uav_samples",
    "list_uav_channels",
    "list_uav_models",
    "search_uav_flights",
    "uav_tools",
]
