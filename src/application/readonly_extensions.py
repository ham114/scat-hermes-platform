"""Opt-in skill operation catalog; legacy five-tool profiles stay unchanged."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from application.readonly import (
    READ_ONLY_TOOLS,
    ReadOnlyApplication,
    ReadOnlySpec,
    business_owner_policy,
)
from scat.schemas.tool_inputs import (
    DEFAULT_BATCH,
    AirportStatsInput,
    EmptyInput,
    FlightReferenceInput,
    ListBatchesInput,
    PredictionErrorStatsInput,
    PredictionMetricSummariesInput,
    PredictionSnapshotInput,
    ScatModelDataQualityInput,
    ScatQualityCleanFlightsInput,
    ScatQualityProblemFlightsInput,
    WorstPredictionCasesInput,
)
from uav.schemas.tool_inputs import (
    UavChannelStatsInput,
    UavEmptyInput,
    UavFlightReferenceInput,
    UavNormalVsAnomalyInput,
    UavSamplesInput,
    UavSearchFlightsInput,
)


class BatchProblemFlights(ScatQualityProblemFlightsInput):
    batch: str = Field(default=DEFAULT_BATCH, min_length=1, max_length=200)


class BatchCleanFlights(ScatQualityCleanFlightsInput):
    batch: str = Field(default=DEFAULT_BATCH, min_length=1, max_length=200)


class SnapshotListInput(FlightReferenceInput):
    start: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=100)


class BoundedUavSamples(UavSamplesInput):
    limit: int = Field(default=20, ge=1, le=100)


class BoundedCfplSummaries(PredictionMetricSummariesInput):
    limit: int = Field(default=20, ge=1, le=50)
    offset: int = Field(default=0, ge=0, le=10000)
    horizon_s: Literal[60, 180, 300, 600, 900, 1200] | None = None


class BoundedCfplWorst(WorstPredictionCasesInput):
    limit: int = Field(default=10, ge=1, le=20)
    offset: int = Field(default=0, ge=0, le=10000)
    horizon_s: Literal[60, 180, 300, 600, 900, 1200] | None = None


ALL_READ_ONLY_TOOLS = {
    **READ_ONLY_TOOLS,
    "get_prediction_metric_summaries": ReadOnlySpec(
        BoundedCfplSummaries,
        "分页读取指定批次已物化的CFPL计划vs实飞汇总，六档冻结时域；不是GBAR指标，不计算或物化。",
    ),
    "get_worst_prediction_cases": ReadOnlySpec(
        BoundedCfplWorst,
        "分页读取指定批次已有CFPL误差最差样本，保留米、时域和阶段；不发起模型推理。",
    ),
    "get_scat_database_overview": ReadOnlySpec(
        EmptyInput, "读取数据库规模与批次元数据，不返回全量航迹。"
    ),
    "list_scat_batches": ReadOnlySpec(ListBatchesInput, "列出真实 SCAT 批次标识；按 limit 限制。"),
    "get_scat_flight_overview": ReadOnlySpec(
        FlightReferenceInput, "读取单航班摘要，用真实 external_id 定位。"
    ),
    "get_scat_airport_stats": ReadOnlySpec(AirportStatsInput, "读取指定批次机场分布与延误统计。"),
    "list_scat_quality_problem_flights": ReadOnlySpec(
        BatchProblemFlights, "指定批次的问题航班分页；说明 missing/outlier/spike 策略。", timeout=60
    ),
    "list_scat_quality_clean_flights": ReadOnlySpec(
        BatchCleanFlights, "指定批次质量合格航班分页；不重写原始数据。", timeout=60
    ),
    "inspect_scat_model_data_quality": ReadOnlySpec(
        ScatModelDataQualityInput, "检查单航班模型输入窗口的真实质量，不运行预测模型。"
    ),
    "get_scat_fpl_overview": ReadOnlySpec(FlightReferenceInput, "读取单航班飞行计划摘要。"),
    "get_scat_prediction_overview": ReadOnlySpec(
        FlightReferenceInput, "读取数据中已有空管预测摘要，不发起模型推理。"
    ),
    "list_scat_prediction_snapshots": ReadOnlySpec(
        SnapshotListInput, "分页发现已有预测快照时间，避免猜造 timestamp。"
    ),
    "get_scat_prediction_snapshot": ReadOnlySpec(
        PredictionSnapshotInput, "读取指定时刻之前最近的已有预测快照。"
    ),
    "get_scat_flight_phase_stats": ReadOnlySpec(FlightReferenceInput, "读取单航班飞行阶段统计。"),
    "get_scat_prediction_error_stats": ReadOnlySpec(
        PredictionErrorStatsInput, "以注册计算口径比较已有预测与实际航迹，不运行新模型。"
    ),
    "list_uav_models": ReadOnlySpec(UavEmptyInput, "列出 UAV 机型与架次规模。"),
    "search_uav_flights": ReadOnlySpec(
        UavSearchFlightsInput, "按机型、正常/异常类型及带时区时间检索 UAV 架次。"
    ),
    "get_uav_flight_overview": ReadOnlySpec(UavFlightReferenceInput, "读取单个 UAV 架次摘要。"),
    "list_uav_channels": ReadOnlySpec(
        UavEmptyInput, "列出 UAV 真实通道键、中文名与单位。", database_required=False
    ),
    "get_uav_samples": ReadOnlySpec(
        BoundedUavSamples, "分页读取 UAV 样本，默认20最多100；先按通道/组缩小范围。"
    ),
    "get_uav_channel_stats": ReadOnlySpec(
        UavChannelStatsInput, "最多六个通道的确定性跨架次统计。", timeout=60
    ),
    "get_uav_normal_vs_anomaly_stats": ReadOnlySpec(
        UavNormalVsAnomalyInput, "比较正常与异常样本通道统计；保留组别、机型和样本数。", timeout=60
    ),
    "get_uav_phase_stats": ReadOnlySpec(UavFlightReferenceInput, "读取单架次飞行阶段统计及单位。"),
}


class NoPersistentQualityCache:
    """Read-only profiles may reuse the service's memory cache, never Redis writes."""

    enabled = False


def build_extended_business_application() -> ReadOnlyApplication:
    from application.knowledge import KnowledgeSearchService
    from core.settings import settings
    from scat.infrastructure.database import flight_database
    from scat.preprocessing.gbar_v35 import ScatGbarV35DataTool
    from scat.services.flight_service import ScatFlightService
    from uav.services.uav_service import UavFlightService

    flights = ScatFlightService(quality_snapshot_cache=NoPersistentQualityCache())
    quality = ScatGbarV35DataTool(collection_quality_service=flights)
    uav = UavFlightService()
    knowledge = KnowledgeSearchService(
        enabled=settings.SCAT_RAG_BASELINE_ENABLED,
        base_url=settings.SCAT_RAG_BASELINE_URL,
        timeout=settings.SCAT_RAG_BASELINE_TIMEOUT_SECONDS,
    )

    async def search(**kwargs):
        kwargs["start_time"] = kwargs.pop("takeoff_start")
        kwargs["end_time"] = kwargs.pop("takeoff_end")
        return await flights.search_flights(**kwargs)

    async def search_uav(**kwargs):
        kwargs["start_time"] = kwargs.pop("takeoff_start")
        kwargs["end_time"] = kwargs.pop("takeoff_end")
        return await uav.list_flights(**kwargs)

    async def channels():
        return uav.list_channels()

    async def model_quality(**kwargs):
        if kwargs.get("end_time") is not None:
            kwargs["end_time"] = kwargs["end_time"].isoformat()
        return {
            "status": "ok",
            "query": dict(kwargs),
            "data": await quality.inspect_quality(**kwargs),
        }

    async def cfpl(operation, **kwargs):
        from scat.services.materialization_service import MaterializationService, _expired

        query = dict(kwargs)
        limit = kwargs.pop("limit")
        if operation == "get_worst_prediction_cases":
            kwargs["metric"] = {
                "horizontal": "horizontal_error_m",
                "vertical": "vertical_error_m",
                "along": "along_track_error_m",
                "cross": "cross_track_error_m",
            }[kwargs["metric"]]
            data = await MaterializationService().list_worst_prediction_samples(
                **kwargs, limit=limit + 1
            )
            key = "samples"
        else:
            data = await MaterializationService().list_metric_summaries(**kwargs, limit=limit + 1)
            key = "summaries"
        rows = data.get(key, [])
        if any(_expired(row, None) for row in rows):
            return {
                "status": "stale_materialization",
                "query": query,
                "data": None,
                "message": "已有CFPL物化结果过期，需要明确准备刷新任务；不使用过期数值。",
            }
        more = len(rows) > limit
        data[key] = rows[:limit]
        data["pagination"] = {
            "offset": query["offset"],
            "limit": limit,
            "returned": len(data[key]),
            "has_more": more,
            "next_offset": query["offset"] + len(data[key]) if more else None,
        }
        data["record_count" if key == "summaries" else "sample_count"] = len(data[key])
        data.update(
            metric_protocol="scat-prediction-metrics-v2",
            comparison="CFPL计划vs实飞，非任务模型",
            coverage_note="读取本批次已有物化指标；覆盖以样本数和航班数为准，不表示所有原始航班均有可比预测。",
        )
        return {"status": "ok", "operation": operation, "query": query, "data": data}

    async def cfpl_summaries(**kwargs):
        return await cfpl("get_prediction_metric_summaries", **kwargs)

    async def cfpl_worst(**kwargs):
        return await cfpl("get_worst_prediction_cases", **kwargs)

    bindings = {
        "get_prediction_metric_summaries": cfpl_summaries,
        "get_worst_prediction_cases": cfpl_worst,
        "get_scat_database_overview": flights.get_database_overview,
        "list_scat_batches": flights.list_batches,
        "search_scat_flights": search,
        "get_scat_batch_overview": flights.get_batch_overview,
        "get_scat_flight_overview": flights.get_flight_overview,
        "get_scat_track_points": flights.get_track_points,
        "get_scat_airport_stats": flights.get_airport_stats,
        "inspect_scat_collection_data_quality": quality.inspect_collection_quality,
        "list_scat_quality_problem_flights": flights.list_model_data_quality_problem_flights,
        "list_scat_quality_clean_flights": flights.list_model_data_quality_clean_flights,
        "inspect_scat_model_data_quality": model_quality,
        "get_scat_fpl_overview": flights.get_fpl_overview,
        "get_scat_prediction_overview": flights.get_prediction_overview,
        "list_scat_prediction_snapshots": flights.list_prediction_snapshots,
        "get_scat_prediction_snapshot": flights.get_prediction_snapshot,
        "get_scat_flight_phase_stats": flights.get_flight_phase_stats,
        "get_scat_prediction_error_stats": flights.get_prediction_error_stats,
        "search_scat_knowledge": knowledge.search,
        "list_uav_models": uav.list_models,
        "search_uav_flights": search_uav,
        "get_uav_flight_overview": uav.get_flight,
        "list_uav_channels": channels,
        "get_uav_samples": uav.get_samples,
        "get_uav_channel_stats": uav.get_channel_stats,
        "get_uav_normal_vs_anomaly_stats": uav.get_normal_vs_anomaly_stats,
        "get_uav_phase_stats": uav.get_flight_phase_stats,
    }
    if set(bindings) != set(ALL_READ_ONLY_TOOLS):
        raise RuntimeError("Extended readonly binding coverage mismatch")
    return ReadOnlyApplication(
        bindings,
        specs=ALL_READ_ONLY_TOOLS,
        connect=flight_database.connect,
        close=flight_database.close,
        **business_owner_policy(settings),
    )
