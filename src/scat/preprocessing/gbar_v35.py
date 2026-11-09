"""Compatibility imports and Mongo-backed tools; algorithms live in gbar_core."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from gbar_core.preprocessing import (
    FEET_TO_METRES,
    FLIGHT_LEVEL_TO_METRES,
    FPM_TO_MPS,
    SCAT_GBAR_ENCODING,
    SCAT_GBAR_FEATURES,
    SCAT_GBAR_MAXIMUM_POSITION_JUMP_METRES,
    SCAT_GBAR_MAXIMUM_REPAIR_RUN,
    SCAT_GBAR_MAXIMUM_REPAIR_SPAN_SECONDS,
    SCAT_GBAR_MAXIMUM_SOURCE_GAP_SECONDS,
    SCAT_GBAR_QUALITY_POLICY_VERSION,
    SCAT_GBAR_SPIKE_THRESHOLDS,
    FeatureEncoding,
    ScatGbarDataError,
    ScatGbarFeatureQuality,
    ScatGbarPreparedWindow,
    ScatGbarQualityReport,
    ScatGbarV35DataProcessor,
)


class _CandidateRepository(Protocol):
    async def get_gbar_input_candidates(
        self,
        *,
        batch: str,
        external_id: str | int,
        end_time: str | None = None,
        limit: int = 500,
    ) -> dict[str, Any] | None: ...


class ScatGbarV35DataTool:
    """Shared deterministic tool for quality inspection and model preparation."""

    def __init__(
        self,
        repository: _CandidateRepository | None = None,
        processor: ScatGbarV35DataProcessor | None = None,
        collection_quality_service: Any | None = None,
    ) -> None:
        if repository is None:
            from scat.repositories.flight_repository import (  # type: ignore[import-untyped]
                ScatFlightRepository,
            )

            repository = ScatFlightRepository()
        self.repository = repository
        self.processor = processor or ScatGbarV35DataProcessor()
        self._collection_quality_service = collection_quality_service

    async def _get_points(
        self, *, batch: str, external_id: str | int, end_time: str | None, source_limit: int
    ) -> list[Mapping[str, Any]]:
        result = await self.repository.get_gbar_input_candidates(
            batch=batch, external_id=external_id, end_time=end_time, limit=source_limit
        )
        if result is None:
            raise ScatGbarDataError("flight_not_found", "没有找到指定的SCAT航班。")
        points = result.get("points")
        if not isinstance(points, list):
            raise ScatGbarDataError("invalid_repository_result", "SCAT轨迹查询结果无效。")
        return points

    async def prepare(
        self,
        *,
        batch: str,
        external_id: str | int,
        end_time: str | None = None,
        source_limit: int = 500,
    ) -> ScatGbarPreparedWindow:
        points = await self._get_points(
            batch=batch, external_id=external_id, end_time=end_time, source_limit=source_limit
        )
        return self.processor.prepare(points, batch=batch, external_id=external_id)

    async def inspect_quality(
        self,
        *,
        batch: str,
        external_id: str | int,
        end_time: str | None = None,
        source_limit: int = 500,
    ) -> dict[str, Any]:
        points = await self._get_points(
            batch=batch, external_id=external_id, end_time=end_time, source_limit=source_limit
        )
        try:
            prepared = self.processor.prepare(points, batch=batch, external_id=external_id)
        except ScatGbarDataError as exc:
            return {
                "model_ready": False,
                "error_code": exc.code,
                "message": str(exc),
                "quality": exc.quality.as_dict() if exc.quality else None,
            }
        return {
            "model_ready": True,
            "error_code": None,
            "message": "质量检查完成；异常替换在模型窗口划分前执行。",
            "selected_window": prepared.summary(),
            "quality": prepared.quality.as_dict(),
        }

    async def inspect_collection_quality(
        self,
        *,
        batch: str | None = None,
    ) -> dict[str, Any]:
        """全库或指定批次的 GBAR 六特征质量聚合（缺失/越界/相邻点跳变）。

        scat-flight-agent 的问答工具与 model-agent 的特征流水线共用本组件；
        聚合在 MongoDB 端一次完成，不逐航班调用单航班检查。
        """
        # 延迟导入避免模块级循环依赖（flight_service -> repository -> 本模块）。
        from scat.services.flight_service import ScatFlightService

        if self._collection_quality_service is None:
            self._collection_quality_service = ScatFlightService()
        return await self._collection_quality_service.get_model_data_quality_summary(batch=batch)


__all__ = [
    "FEET_TO_METRES",
    "FPM_TO_MPS",
    "FLIGHT_LEVEL_TO_METRES",
    "SCAT_GBAR_FEATURES",
    "SCAT_GBAR_QUALITY_POLICY_VERSION",
    "SCAT_GBAR_MAXIMUM_SOURCE_GAP_SECONDS",
    "SCAT_GBAR_MAXIMUM_REPAIR_RUN",
    "SCAT_GBAR_MAXIMUM_REPAIR_SPAN_SECONDS",
    "SCAT_GBAR_MAXIMUM_POSITION_JUMP_METRES",
    "SCAT_GBAR_SPIKE_THRESHOLDS",
    "SCAT_GBAR_ENCODING",
    "FeatureEncoding",
    "ScatGbarDataError",
    "ScatGbarFeatureQuality",
    "ScatGbarQualityReport",
    "ScatGbarPreparedWindow",
    "ScatGbarV35DataProcessor",
    "ScatGbarV35DataTool",
]
