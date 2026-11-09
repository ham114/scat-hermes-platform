"""SCAT Agent 工具定义。

本模块将 ScatFlightService 封装为 LangChain Tool。

调用关系：

    LangGraph Agent
        -> SCAT Tool
        -> ScatFlightService
        -> ScatFlightRepository
        -> MongoDB

工具层负责：
1. 向大语言模型公开明确、受控的业务能力；
2. 使用 Pydantic Schema 校验工具参数；
3. 确保 SCAT MongoDB 连接已建立；
4. 将业务异常转换为结构化工具结果；
5. 禁止模型直接生成或执行任意 MongoDB 查询。

工具层不负责：
1. 编写 MongoDB 查询；
2. 计算业务统计；
3. 组织最终自然语言回答；
4. 管理 Agent 对话状态。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, Literal

from langchain_core.tools import BaseTool, tool
from pymongo.errors import PyMongoError

from agents.analysis_results import (
    attach_analysis_result,
    build_scat_airport_stats_analysis,
    build_scat_batch_analysis,
    build_scat_flight_overview_analysis,
    build_scat_flight_phase_stats_analysis,
    build_scat_flight_search_analysis,
    build_scat_prediction_error_analysis,
    build_scat_quality_clean_flights_analysis,
    build_scat_quality_problem_flights_analysis,
    build_scat_track_analysis,
)
from scat.infrastructure.database import flight_database
from scat.preprocessing.gbar_v35 import ScatGbarV35DataTool
from scat.schemas.tool_inputs import (
    DEFAULT_BATCH,
    AirportStatsInput,
    BatchOverviewInput,
    EmptyInput,
    FlightReferenceInput,
    ListBatchesInput,
    PredictionErrorStatsInput,
    PredictionSnapshotInput,
    ScatCollectionDataQualityInput,
    ScatModelDataQualityInput,
    ScatQualityCleanFlightsInput,
    ScatQualityProblemFlightsInput,
    SearchFlightsInput,
    TrackPointsInput,
)
from scat.services.flight_service import ScatFlightService

logger = logging.getLogger(__name__)


# Service 本身不保存单次请求状态，因此可以作为模块级单例复用。
flight_service = ScatFlightService()
gbar_data_tool = ScatGbarV35DataTool(collection_quality_service=flight_service)


async def _execute_service_operation(
    *,
    operation_name: str,
    operation: Callable[
        [],
        Awaitable[dict[str, Any]],
    ],
) -> dict[str, Any]:
    """统一执行一个 SCAT Service 操作。

    每次工具调用前都会调用 connect()。当前 FlightDatabase.connect()
    是幂等操作：已经存在客户端时不会重复创建客户端。

    当前阶段不在每个工具结束后关闭 MongoDB 客户端，因为：
    1. Agent 进程会连续处理多轮请求；
    2. PyMongo 客户端内部维护连接池；
    3. 每次查询后关闭会造成不必要的重复连接开销。

    后续在 FastAPI 生命周期中统一添加数据库关闭逻辑。
    """
    try:
        await flight_database.connect()
        return await operation()

    except ValueError as exc:
        # 参数或业务规则错误，可以安全地返回给模型，
        # 让模型修正参数后重新调用工具。
        return {
            "status": "invalid_request",
            "operation": operation_name,
            "message": str(exc),
            "data": None,
        }

    except PyMongoError:
        # 数据库内部错误写入服务端日志。
        # 不向模型返回连接字符串、认证信息等内部细节。
        logger.exception(
            "MongoDB error while executing SCAT operation: %s",
            operation_name,
        )

        return {
            "status": "database_error",
            "operation": operation_name,
            "message": ("SCAT 数据库查询失败。请稍后重试或检查数据库服务状态。"),
            "data": None,
        }

    except Exception:
        # 捕获未预期错误，防止整个 Agent 图因为单个工具异常中断。
        logger.exception(
            "Unexpected error while executing SCAT operation: %s",
            operation_name,
        )

        return {
            "status": "internal_error",
            "operation": operation_name,
            "message": ("执行 SCAT 业务操作时发生未预期错误。"),
            "data": None,
        }


@tool(args_schema=EmptyInput)
async def get_scat_database_overview() -> dict[str, Any]:
    """查询 SCAT 航班数据库总体概况。

    适用问题：
    - 数据库中一共有多少条航班；
    - 当前有哪些数据批次；
    - 每个批次覆盖什么时间范围；
    - 每个批次包含多少条航班。

    本工具只返回数据库和批次级摘要，不返回单条航班轨迹。
    """
    return await _execute_service_operation(
        operation_name="get_scat_database_overview",
        operation=flight_service.get_database_overview,
    )


@tool(args_schema=ListBatchesInput)
async def list_scat_batches(
    limit: int = 100,
) -> dict[str, Any]:
    """列出 SCAT 数据库中的可用批次。

    当用户询问“有哪些周的数据”“当前有哪些批次”或者需要先确定
    batch 参数时使用本工具。

    返回每个批次的：
    - 批次名称；
    - 航班数量；
    - 最早和最晚计划起飞时间；
    - 最早和最晚上传时间。
    """
    return await _execute_service_operation(
        operation_name="list_scat_batches",
        operation=lambda: flight_service.list_batches(
            limit=limit,
        ),
    )


@tool(args_schema=ScatCollectionDataQualityInput)
async def inspect_scat_collection_data_quality(
    batch: str | None = None,
) -> dict[str, Any]:
    """全库或按批次统计SCAT六个GBAR输入特征的数据质量。

    本工具在MongoDB端执行一次聚合，返回航班总数、轨迹点总数、包含
    缺失/物理范围越界/相邻点跳变值的航班数、问题航班并集/交集，以及
    六特征的问题值数量、无上述问题的轨迹总数和若干干净轨迹样例。
    不要逐航班调用单航班巡检来计算全库总数。

    “异常”指GBAR v35物理范围越界或相邻点孤立跳变（三点规则，
    位置跳变按球面距离 4 km、标量按预处理转换后的阈值判定）。
    跳变只计数不修复，修复发生在单航班预处理层。
    """
    return await _execute_service_operation(
        operation_name="inspect_scat_collection_data_quality",
        operation=lambda: gbar_data_tool.inspect_collection_quality(batch=batch),
    )


@tool(args_schema=ScatQualityProblemFlightsInput)
async def list_scat_quality_problem_flights(
    batch: str | None = None,
    limit: int = 20,
    offset: int = 0,
    problem_type: Literal["any", "missing", "outlier", "spike"] = "any",
    sort_by: str = "outlier_point_count",
) -> dict[str, Any]:
    """分页列出包含缺失、越界或跳变点的问题航班。

    与全库统计工具使用同一套判定口径，返回原始文件名、航班号、批次、
    呼号、轨迹点数和缺失/越界/跳变点数，默认按越界点数降序，每页
    默认 20 条。problem_type 可限定只返回缺失、越界或跳变轨迹。

    适用问题：
    - 哪些航班包含异常点/缺失点；
    - 问题最多的前 N 个航班是哪些。

    获得航班号后可用 inspect_scat_model_data_quality 查看该航班细节。
    """
    result = await _execute_service_operation(
        operation_name="list_scat_quality_problem_flights",
        operation=lambda: flight_service.list_model_data_quality_problem_flights(
            batch=batch,
            limit=limit,
            offset=offset,
            problem_type=problem_type,
            sort_by=sort_by,
        ),
    )
    return attach_analysis_result(
        result,
        build_scat_quality_problem_flights_analysis(result),
    )


@tool(args_schema=ScatQualityCleanFlightsInput)
async def list_scat_quality_clean_flights(
    batch: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    """分页列出当前质量口径下无缺失、越界或跳变的SCAT轨迹。

    返回原始文件名（数据库存在时）、batch、external_id、呼号和轨迹点数。
    batch + external_id 是权威定位标识；“无问题”仅指当前GBAR v35全库
    质量口径命中数为零，不自动等同于模型窗口一定可用。

    适用问题：
    - 给出几条没有异常点和缺失值的轨迹；
    - 列出可作为后续单航班检查候选的干净轨迹文件名或编号。
    """
    result = await _execute_service_operation(
        operation_name="list_scat_quality_clean_flights",
        operation=lambda: flight_service.list_model_data_quality_clean_flights(
            batch=batch,
            limit=limit,
            offset=offset,
        ),
    )
    return attach_analysis_result(
        result,
        build_scat_quality_clean_flights_analysis(result),
    )


@tool(args_schema=BatchOverviewInput)
async def get_scat_batch_overview(
    batch: str = DEFAULT_BATCH,
    distribution_limit: int = 10,
) -> dict[str, Any]:
    """查询一个 SCAT 数据批次的综合统计。

    适用问题：
    - 某批次有多少航班和轨迹点；
    - 主要机型有哪些；
    - 最常见航线有哪些；
    - 多少航班包含 SCAT 空管预测轨迹；
    - 轨迹点数量的平均值、最小值和最大值。

    这是批次级统计工具，不用于查询具体航班。
    """
    result = await _execute_service_operation(
        operation_name="get_scat_batch_overview",
        operation=lambda: flight_service.get_batch_overview(
            batch=batch,
            distribution_limit=distribution_limit,
        ),
    )
    return attach_analysis_result(result, build_scat_batch_analysis(result))


@tool(args_schema=SearchFlightsInput)
async def search_scat_flights(
    batch: str | None = DEFAULT_BATCH,
    callsign: str | None = None,
    callsigns: tuple[str, ...] = (),
    exclude_callsigns: tuple[str, ...] = (),
    callsign_prefix: str | None = None,
    aircraft_type: str | None = None,
    aircraft_types: tuple[str, ...] = (),
    exclude_aircraft_types: tuple[str, ...] = (),
    adep: str | None = None,
    adeps: tuple[str, ...] = (),
    exclude_adeps: tuple[str, ...] = (),
    ades: str | None = None,
    adess: tuple[str, ...] = (),
    exclude_adess: tuple[str, ...] = (),
    external_id: str | None = None,
    external_ids: tuple[str, ...] = (),
    exclude_external_ids: tuple[str, ...] = (),
    candidate_key: str | None = None,
    takeoff_start: datetime | None = None,
    takeoff_end: datetime | None = None,
    min_track_points: int | None = None,
    max_track_points: int | None = None,
    has_fpl: bool | None = None,
    has_scat_prediction: bool | None = None,
    sort_by: Literal["iobt", "callsign", "external_id"] = "iobt",
    sort_order: Literal["asc", "desc"] = "asc",
    limit: int = 20,
    skip: int = 0,
) -> dict[str, Any]:
    """按照结构化条件搜索 SCAT 航班。

    可以组合以下条件：
    - 数据批次；
    - 航班呼号；
    - ICAO 机型代码；
    - 起飞机场 ICAO 代码；
    - 目的机场 ICAO 代码；
    - candidate_key。
    - 计划起飞时间范围。
    - 单值或多值包含/排除条件；
    - 呼号前缀；
    - source.external_id 包含/排除；
    - 轨迹点数范围；
    - 是否含 FPL、是否含 SCAT 原始预测快照。

    所有提供的条件采用 AND 关系。

    示例：
    - 查询呼号 IBK8WB；
    - 查询所有 B738 航班；
    - 查询从 EGKK 飞往 ESSA 的航班；
    - 查询从 EGKK 飞往 ESSA 的 B738 航班。

    返回航班摘要，不返回完整 plots、FPL 或预测轨迹大数组。
    """
    result = await _execute_service_operation(
        operation_name="search_scat_flights",
        operation=lambda: flight_service.search_flights(
            batch=batch,
            callsign=callsign,
            callsigns=callsigns,
            exclude_callsigns=exclude_callsigns,
            callsign_prefix=callsign_prefix,
            aircraft_type=aircraft_type,
            aircraft_types=aircraft_types,
            exclude_aircraft_types=exclude_aircraft_types,
            adep=adep,
            adeps=adeps,
            exclude_adeps=exclude_adeps,
            ades=ades,
            adess=adess,
            exclude_adess=exclude_adess,
            external_id=external_id,
            external_ids=external_ids,
            exclude_external_ids=exclude_external_ids,
            candidate_key=candidate_key,
            start_time=takeoff_start,
            end_time=takeoff_end,
            min_track_points=min_track_points,
            max_track_points=max_track_points,
            has_fpl=has_fpl,
            has_scat_prediction=has_scat_prediction,
            sort_by=sort_by,
            sort_order=sort_order,
            limit=limit,
            skip=skip,
        ),
    )
    return attach_analysis_result(result, build_scat_flight_search_analysis(result))


@tool(args_schema=FlightReferenceInput)
async def get_scat_flight_overview(
    batch: str = DEFAULT_BATCH,
    external_id: str = "",
) -> dict[str, Any]:
    """查询一条 SCAT 航班的综合业务概况。

    使用 batch 和 source.external_id 唯一定位来源航班。

    返回内容包括：
    - 呼号、机型、起降机场；
    - 计划起飞时间；
    - 管制中心移交记录；
    - 轨迹持续时间、点数和范围；
    - 高度、速度和垂直状态统计；
    - 轨迹数据质量；
    - FPL 最新状态；
    - SCAT 空管预测轨迹摘要。

    当用户询问具体航班的总体情况时，应优先使用本工具。
    """
    result = await _execute_service_operation(
        operation_name="get_scat_flight_overview",
        operation=lambda: flight_service.get_flight_overview(
            batch=batch,
            external_id=external_id,
        ),
    )
    return attach_analysis_result(
        result,
        build_scat_flight_overview_analysis(result),
    )


@tool(args_schema=TrackPointsInput)
async def get_scat_track_points(
    batch: str = DEFAULT_BATCH,
    external_id: str = "",
    start: int = 0,
    limit: int = 100,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    every_nth: int = 1,
    fields: set[str] | None = None,
) -> dict[str, Any]:
    """分页读取一条 SCAT 航班的标准化轨迹点。

    每个返回点已经从原始 ASTERIX 嵌套结构转换为：
    - 时间；
    - 纬度和经度；
    - 飞行高度层、高度英尺和高度米；
    - vx、vy和地速；
    - 航迹角；
    - 爬升下降率；
    - 垂直状态；
    - 可选 Mode-S 信息；
    - 数据质量标记。

    仅在用户明确要求查看具体轨迹点、轨迹片段或模型输入值时使用。

    普通航班分析应优先调用 get_scat_flight_overview，避免将大量轨迹点
    放入模型上下文。通常 limit 应控制在 20 以内。

    用户要求二维航迹图时，fields 至少包含 position（可再加 motion 以高度/
    速度着色）；返回结果会自动渲染二维轨迹图（经纬度连线）。
    """
    result = await _execute_service_operation(
        operation_name="get_scat_track_points",
        operation=lambda: flight_service.get_track_points(
            batch=batch,
            external_id=external_id,
            start=start,
            limit=limit,
            start_time=start_time,
            end_time=end_time,
            every_nth=every_nth,
            fields=fields,
        ),
    )
    return attach_analysis_result(result, build_scat_track_analysis(result))


@tool(args_schema=ScatModelDataQualityInput)
async def inspect_scat_model_data_quality(
    batch: str = DEFAULT_BATCH,
    external_id: str = "",
    end_time: datetime | None = None,
    source_limit: int = 500,
) -> dict[str, Any]:
    """检查指定SCAT航班作为GBAR v35模型输入时的数据质量。

    返回六个特征各自的缺失、非有限值、越界值、跳变值、插值、有限
    外推和未修复数量，并说明指定截止时刻的最新窗口是否可以执行模型。

    本工具与 model-agent 共用同一个确定性预处理器。异常值会先替换为
    缺失标记，在完整连续轨迹段上完成插值/有限外推，之后才划分36点
    模型窗口；不会因为孤立异常而偷偷改用更早的窗口。
    """
    result = await _execute_service_operation(
        operation_name="inspect_scat_model_data_quality",
        operation=lambda: gbar_data_tool.inspect_quality(
            batch=batch,
            external_id=external_id,
            end_time=end_time.isoformat() if end_time is not None else None,
            source_limit=source_limit,
        ),
    )
    if "status" not in result:
        return {
            "status": "ok",
            "operation": "inspect_scat_model_data_quality",
            "query": {
                "batch": batch,
                "external_id": external_id,
                "end_time": end_time.isoformat() if end_time is not None else None,
                "source_limit": source_limit,
            },
            "data": result,
        }
    return result


@tool(args_schema=FlightReferenceInput)
async def get_scat_fpl_overview(
    batch: str = DEFAULT_BATCH,
    external_id: str = "",
) -> dict[str, Any]:
    """查询一条 SCAT 航班的飞行计划摘要。

    返回：
    - 飞行计划各类事件数量；
    - 最新起飞跑道和 SID；
    - 最新到达跑道和 STAR；
    - 最新许可高度、速度和航向；
    - 等待状态；
    - 进近许可；
    - 最新 ICAO 航路；
    - 请求飞行高度层和真空速。

    当问题集中在飞行计划、许可、SID、STAR、跑道或航路时使用。
    """
    return await _execute_service_operation(
        operation_name="get_scat_fpl_overview",
        operation=lambda: flight_service.get_fpl_overview(
            batch=batch,
            external_id=external_id,
        ),
    )


@tool(args_schema=FlightReferenceInput)
async def get_scat_prediction_overview(
    batch: str = DEFAULT_BATCH,
    external_id: str = "",
) -> dict[str, Any]:
    """查询一条航班的 SCAT 空管预测轨迹摘要。

    返回：
    - 是否存在预测数据；
    - 预测快照数量；
    - 第一和最后快照时间；
    - 每份快照包含的航路点数量统计。

    注意：这是原始 SCAT 数据中的空管系统预测轨迹，
    不是 GBAR 神经网络模型的预测结果。
    """
    return await _execute_service_operation(
        operation_name="get_scat_prediction_overview",
        operation=lambda: flight_service.get_prediction_overview(
            batch=batch,
            external_id=external_id,
        ),
    )


@tool(args_schema=PredictionSnapshotInput)
async def get_scat_prediction_snapshot(
    batch: str = DEFAULT_BATCH,
    external_id: str = "",
    timestamp: str = "",
) -> dict[str, Any]:
    """查询指定时刻之前最近的 SCAT 空管预测轨迹快照。

    返回该快照中的航路点，包括：
    - 航路点名称和类型；
    - 纬度和经度；
    - 预计过点时间；
    - 实际或预测飞行高度层；
    - 请求飞行高度层；
    - 该点是否已经实际飞越。

    适用于：
    - 查看某一时刻系统认为飞机未来会经过哪些点；
    - 对比实际轨迹与空管预测航路；
    - 分析预测轨迹随时间的更新。

    注意：如果目标时间晚于该航班全部快照时间，将返回最后一个快照。
    """
    return await _execute_service_operation(
        operation_name="get_scat_prediction_snapshot",
        operation=lambda: flight_service.get_prediction_snapshot(
            batch=batch,
            external_id=external_id,
            timestamp=timestamp,
        ),
    )


@tool(args_schema=AirportStatsInput)
async def get_scat_airport_stats(
    batch: str = DEFAULT_BATCH,
    limit: int = 20,
) -> dict[str, Any]:
    """查询一个 SCAT 批次内各起飞机场的航班量与起飞延误统计。

    适用问题：
    - 某个机场有多少航班起飞；
    - 各机场平均/最大起飞延误（ATD 相对计划起飞时间 IOBT）；
    - 多少航班延误超过 15 分钟。

    返回按航班量降序的机场列表，包含平均/最大/最小延误分钟数和延误率。
    无实际起飞时间（ATD）的航班不参与延误均值计算。
    """
    result = await _execute_service_operation(
        operation_name="get_scat_airport_stats",
        operation=lambda: flight_service.get_airport_stats(
            batch=batch,
            limit=limit,
        ),
    )
    return attach_analysis_result(
        result,
        build_scat_airport_stats_analysis(result),
    )


@tool(args_schema=FlightReferenceInput)
async def get_scat_flight_phase_stats(
    batch: str = DEFAULT_BATCH,
    external_id: str = "",
) -> dict[str, Any]:
    """查询一条 SCAT 航班的爬升/巡航/下降阶段统计。

    按轨迹点垂直状态（I062/200.vert）分组：0=平飞、1=爬升、2=下降，
    其余为不确定。每个阶段返回：
    - 轨迹点数与占比；
    - 开始/结束时间与持续时长；
    - 飞行高度层的最小/平均/最大值；
    - 平均/最大地速；
    - 平均/最大绝对爬升下降率。

    适用问题：
    - 这趟航班爬升/巡航/下降各用了多久；
    - 爬升阶段的高度变化和速度范围。
    """
    result = await _execute_service_operation(
        operation_name="get_scat_flight_phase_stats",
        operation=lambda: flight_service.get_flight_phase_stats(
            batch=batch,
            external_id=external_id,
        ),
    )
    return attach_analysis_result(
        result,
        build_scat_flight_phase_stats_analysis(result),
    )


@tool(args_schema=PredictionErrorStatsInput)
async def get_scat_prediction_error_stats(
    batch: str = DEFAULT_BATCH,
    external_id: str = "",
    timestamp: str | None = None,
    match_window_seconds: float = 300.0,
) -> dict[str, Any]:
    """计算 SCAT 空管预测轨迹与实际轨迹之间的误差统计。

    使用最新一份预测快照（或用 timestamp 指定某时刻之前最近的一份），
    对每个预测航路点，在实际轨迹中找时间最接近其预计过点时间（ETO）的
    轨迹点，计算：
    - 时间误差（实际时间 - ETO，秒）；
    - 水平误差（预测位置与实际位置的球面距离，米）；
    - 垂直误差（实际飞行高度层与预测高度层的差值，米）。

    返回均值、中位数、最大值、RMSE 及每个航路点的误差明细（最多 100 行）。
    这是对原始空管预测的确定性误差统计，不是 GBAR 模型评估；口径为“最近点
    匹配”（单份快照 + 时间窗口），与批次级 v2 口径（scat-prediction-metrics-v2，
    见 get_prediction_metric_summaries）数值不同，引用时必须注明口径。
    """
    result = await _execute_service_operation(
        operation_name="get_scat_prediction_error_stats",
        operation=lambda: flight_service.get_prediction_error_stats(
            batch=batch,
            external_id=external_id,
            timestamp=timestamp,
            match_window_seconds=match_window_seconds,
        ),
    )
    return attach_analysis_result(
        result,
        build_scat_prediction_error_analysis(result),
    )


# 后续创建 scat_agent.py 时，直接导入这个列表并绑定到模型。
scat_tools: list[BaseTool] = [
    get_scat_database_overview,
    list_scat_batches,
    inspect_scat_collection_data_quality,
    list_scat_quality_problem_flights,
    list_scat_quality_clean_flights,
    get_scat_batch_overview,
    search_scat_flights,
    get_scat_flight_overview,
    get_scat_track_points,
    inspect_scat_model_data_quality,
    get_scat_fpl_overview,
    get_scat_prediction_overview,
    get_scat_prediction_snapshot,
    get_scat_airport_stats,
    get_scat_flight_phase_stats,
    get_scat_prediction_error_stats,
]


__all__ = [
    "get_scat_airport_stats",
    "get_scat_batch_overview",
    "get_scat_database_overview",
    "get_scat_flight_overview",
    "get_scat_flight_phase_stats",
    "get_scat_fpl_overview",
    "get_scat_prediction_error_stats",
    "get_scat_prediction_overview",
    "get_scat_prediction_snapshot",
    "get_scat_track_points",
    "inspect_scat_collection_data_quality",
    "inspect_scat_model_data_quality",
    "list_scat_batches",
    "list_scat_quality_clean_flights",
    "list_scat_quality_problem_flights",
    "scat_tools",
    "search_scat_flights",
]
