"""M4-05/M4-06 航班特征物化任务测试：口径、增量、定向重算与失败隔离。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from scat.materialization.aggregates import FLIGHT_FEATURE_BASIS_PIPELINE
from scat.materialization.repository import MaterializationRepository
from scat.materialization.tasks import (
    FLIGHT_FEATURES_ALGORITHM_VERSION,
    FLIGHT_LEVEL_TO_METRES,
    iter_batch_external_ids,
    materialize_batch_flight_features,
    materialize_flight_features,
)

BATCH = "scat20161015_20161021"
EXTERNAL_ID = "100065"

BASIS: dict[str, Any] = {
    "aircraft_type": "B738",
    "point_count": 288,
    "duration_s": 3600.0,
    "distance_m": 720_000.0,
}

PHASES = [
    {
        "_id": 1,
        "point_count": 60,
        "start_time": datetime(2016, 10, 16, 16, 0, tzinfo=UTC),
        "end_time": datetime(2016, 10, 16, 16, 10, tzinfo=UTC),
        "min_flight_level": 30.0,
        "max_flight_level": 3000.0,
        "average_ground_speed_mps": 140.0,
        "maximum_ground_speed_mps": 160.0,
    },
    {
        "_id": 0,
        "point_count": 180,
        "start_time": datetime(2016, 10, 16, 16, 10, tzinfo=UTC),
        "end_time": datetime(2016, 10, 16, 16, 40, tzinfo=UTC),
        "min_flight_level": 3000.0,
        "max_flight_level": 3100.0,
        "average_ground_speed_mps": 220.0,
        "maximum_ground_speed_mps": 240.0,
    },
    {
        "_id": 2,
        "point_count": 48,
        "start_time": datetime(2016, 10, 16, 16, 40, tzinfo=UTC),
        "end_time": datetime(2016, 10, 16, 17, 0, tzinfo=UTC),
        "min_flight_level": 20.0,
        "max_flight_level": 3000.0,
        "average_ground_speed_mps": 130.0,
        "maximum_ground_speed_mps": 150.0,
    },
]


def _track_point(
    minute: int,
    lat: float,
    lon: float,
    flight_level: float,
    vertical_mode: int,
) -> dict[str, Any]:
    return {
        "timestamp": f"2016-10-16T16:{minute:02d}:00",
        "lat": lat,
        "lon": lon,
        "measured_flight_level": flight_level,
        "vertical_mode": vertical_mode,
    }


def _build_track() -> list[dict[str, Any]]:
    """M4-11 阶段秒数输入：低空爬升 → 高空平飞 → 下降，三段方向/高度各不相同。"""
    track: list[dict[str, Any]] = []
    minute = 0
    for index in range(5):  # 段一：低空爬升（vert=1，FL 20→60）
        track.append(
            _track_point(minute + index, 59.00 + index * 0.01, 18.00, 20.0 + index * 10, 1)
        )
    minute += 5
    for index in range(11):  # 段二：高空平飞（vert=0，FL 350）
        track.append(_track_point(minute + index, 59.04, 18.01 + index * 0.01, 350.0, 0))
    minute += 11
    for index in range(5):  # 段三：下降（vert=2，FL 90→10）
        track.append(
            _track_point(
                minute + index, 59.04 - index * 0.01, 18.12 + index * 0.01, 90.0 - index * 20, 2
            )
        )
    return track


TRACK: list[dict[str, Any]] = _build_track()
TRACK_SECONDS = 20 * 60.0  # 首尾点时间跨度（20 分钟）


def _cursor(rows: list[dict[str, Any]]) -> Mock:
    cursor = Mock()
    cursor.to_list = AsyncMock(return_value=rows)
    return cursor


def _find_cursor(pages: list[list[dict[str, Any]]]) -> Mock:
    cursor = Mock()
    cursor.sort.return_value = cursor
    cursor.limit.return_value = cursor
    cursor.to_list = AsyncMock(side_effect=pages)
    return cursor


def _external_id_of(pipeline: list[dict[str, Any]]) -> str:
    return str(pipeline[0]["$match"]["source.external_id"])


def _flight_repository(
    *,
    basis_by_id: dict[str, dict[str, Any]],
    pages: list[list[dict[str, Any]]] | None = None,
    phases: list[dict[str, Any]] | None = None,
    points: list[dict[str, Any]] | None = None,
) -> Mock:
    collection = Mock()
    collection.find = Mock(return_value=_find_cursor(pages or []))
    collection.aggregate = AsyncMock(
        side_effect=lambda pipeline: _cursor(
            [basis_by_id[_external_id_of(pipeline)]]
            if _external_id_of(pipeline) in basis_by_id
            else []
        )
    )
    repository = Mock()
    repository.collection = collection
    repository.get_trajectory_phase_stats = AsyncMock(
        return_value=PHASES if phases is None else phases
    )
    repository.get_trajectory_points_for_phases = AsyncMock(
        return_value=TRACK if points is None else points
    )
    repository.count_all = AsyncMock(return_value=len(pages[0]) if pages else 0)
    return repository


def _materialization_repository(
    *,
    upserts: list[tuple[dict[str, Any], dict[str, Any]]] | None = None,
    existing: set[str] | None = None,
) -> MaterializationRepository:
    repository = MaterializationRepository(Mock())
    collection = Mock()
    collection.replace_one = AsyncMock(
        side_effect=lambda f, d, **kw: upserts.append((f, d)) if upserts is not None else None
    )
    repository.collection = Mock(return_value=collection)  # type: ignore[method-assign]
    repository.existing_values = AsyncMock(  # type: ignore[method-assign]
        return_value=set(existing or ())
    )
    return repository


def _page(*external_ids: str) -> list[dict[str, Any]]:
    return [
        {"_id": index + 1, "source": {"external_id": external_id}}
        for index, external_id in enumerate(external_ids)
    ]


@pytest.mark.asyncio
async def test_materialize_flight_features_combines_basis_and_phases():
    upsert_calls: list[tuple[dict, dict]] = []
    record = await materialize_flight_features(
        batch=BATCH,
        external_id=EXTERNAL_ID,
        flight_repository=_flight_repository(basis_by_id={EXTERNAL_ID: BASIS}),
        materialization_repository=_materialization_repository(upserts=upsert_calls),
        generated_at=datetime(2026, 9, 17, 12, 0, tzinfo=UTC),
    )

    assert record.batch == BATCH
    assert record.external_id == EXTERNAL_ID
    assert record.aircraft_type == "B738"
    assert record.point_count == 288
    assert record.duration_s == 3600.0
    assert record.distance_km == 720.0
    # 阶段秒数由 RDP 分段 + 按点区间归属给出（不再是 vert 分组区间求和）
    assert record.climb_seconds == pytest.approx(240.0)
    assert record.cruise_seconds == pytest.approx(660.0)
    assert record.descent_seconds == pytest.approx(300.0)
    assert record.climb_seconds + record.cruise_seconds + record.descent_seconds == pytest.approx(
        TRACK_SECONDS
    )
    # 加权平均地速：(140*60 + 220*180 + 130*48) / 288
    assert record.average_ground_speed_mps == pytest.approx((140 * 60 + 220 * 180 + 130 * 48) / 288)
    assert record.maximum_ground_speed_mps == 240.0
    # 阶段行的 flight level 是 100 英尺单位，入库换算为米
    assert record.minimum_altitude_m == pytest.approx(20.0 * FLIGHT_LEVEL_TO_METRES)
    assert record.maximum_altitude_m == pytest.approx(3100.0 * FLIGHT_LEVEL_TO_METRES)

    filter_doc, document = upsert_calls[0]
    assert filter_doc == {
        "batch": BATCH,
        "external_id": EXTERNAL_ID,
        "source_version": "scat-import-2",
        "algorithm_version": FLIGHT_FEATURES_ALGORITHM_VERSION,
    }
    assert document["distance_km"] == 720.0


@pytest.mark.asyncio
async def test_materialize_flight_features_ignores_phase_aggregation_times():
    """v3 阶段秒数只来自 RDP 分段：聚合行的起止时间（旧口径用来求秒数）不再参与计算。"""
    iso_phases = [
        {**PHASES[0], "start_time": "2016-10-16T16:00:00", "end_time": "2016-10-16T16:10:00"},
        {**PHASES[1], "start_time": "2016-10-16T16:10:00", "end_time": "2016-10-16T16:40:00"},
        {**PHASES[2], "start_time": "2016-10-16T16:40:00", "end_time": "2016-10-16T17:00:00"},
    ]
    grouped = await materialize_flight_features(
        batch=BATCH,
        external_id=EXTERNAL_ID,
        flight_repository=_flight_repository(basis_by_id={EXTERNAL_ID: BASIS}, phases=iso_phases),
        materialization_repository=_materialization_repository(),
    )
    flat = await materialize_flight_features(
        batch=BATCH,
        external_id=EXTERNAL_ID,
        flight_repository=_flight_repository(
            basis_by_id={EXTERNAL_ID: BASIS},
            phases=[{**PHASES[0], "start_time": None, "end_time": None}],
        ),
        materialization_repository=_materialization_repository(),
    )

    assert grouped.climb_seconds == flat.climb_seconds
    assert grouped.cruise_seconds == flat.cruise_seconds
    assert grouped.descent_seconds == flat.descent_seconds
    # 旧口径（按聚合行时间求和）会得到 600/1800/1200；新口径与聚合时间无关
    assert grouped.cruise_seconds != 1800.0


@pytest.mark.asyncio
async def test_materialize_flight_features_keeps_version_isolation():
    upsert_calls: list[tuple[dict, dict]] = []
    await materialize_flight_features(
        batch=BATCH,
        external_id=EXTERNAL_ID,
        flight_repository=_flight_repository(basis_by_id={EXTERNAL_ID: BASIS}),
        materialization_repository=_materialization_repository(upserts=upsert_calls),
        source_version="scat-import-2",
        algorithm_version="2",
    )

    filter_doc, _ = upsert_calls[0]
    # 版本变化写入不同唯一键，旧版本结果不被覆盖
    assert filter_doc["algorithm_version"] == "2"
    assert filter_doc["source_version"] == "scat-import-2"


@pytest.mark.asyncio
async def test_materialize_flight_features_rejects_empty_flight():
    with pytest.raises(ValueError, match="无有效轨迹点"):
        await materialize_flight_features(
            batch=BATCH,
            external_id="no-such",
            flight_repository=_flight_repository(basis_by_id={}),
            materialization_repository=_materialization_repository(),
        )


@pytest.mark.asyncio
async def test_batch_materialization_isolates_per_flight_failures():
    # 第二个航班的 basis 聚合返回空 → 该航班失败，第一个航班结果保留
    result = await materialize_batch_flight_features(
        batch=BATCH,
        flight_repository=_flight_repository(
            basis_by_id={EXTERNAL_ID: BASIS},
            pages=[_page(EXTERNAL_ID, "100066"), []],
        ),
        materialization_repository=_materialization_repository(),
    )

    assert result["succeeded_count"] == 1
    assert result["failed_count"] == 1
    assert result["skipped_count"] == 0
    assert result["failed"][0]["external_id"] == "100066"
    assert "无有效轨迹点" in result["failed"][0]["error"]


@pytest.mark.asyncio
async def test_batch_materialization_skips_existing_records_incrementally():
    upsert_calls: list[tuple[dict, dict]] = []
    progress: list[dict[str, Any]] = []

    result = await materialize_batch_flight_features(
        batch=BATCH,
        flight_repository=_flight_repository(
            basis_by_id={EXTERNAL_ID: BASIS},
            pages=[_page(EXTERNAL_ID), []],
        ),
        materialization_repository=_materialization_repository(
            upserts=upsert_calls,
            existing={EXTERNAL_ID},
        ),
        skip_existing=True,
        on_progress=_async_recorder(progress),
    )

    assert result["skipped_count"] == 1
    assert result["succeeded_count"] == 0
    # 已有结果不重复写入（幂等增量），也不触发进度回调
    assert upsert_calls == []
    assert progress == []


@pytest.mark.asyncio
async def test_batch_materialization_reports_failure_in_progress_payload():
    progress: list[dict[str, Any]] = []

    await materialize_batch_flight_features(
        batch=BATCH,
        flight_repository=_flight_repository(
            basis_by_id={EXTERNAL_ID: BASIS},
            pages=[_page(EXTERNAL_ID, "100066"), []],
        ),
        materialization_repository=_materialization_repository(),
        on_progress=_async_recorder(progress),
    )

    assert [item["processed"] for item in progress] == [1, 2]
    assert "failure" not in progress[0]
    assert progress[1]["failure"]["external_id"] == "100066"
    assert progress[1]["failed"] == 1


@pytest.mark.asyncio
async def test_batch_materialization_honours_limit_and_targets():
    progress: list[dict[str, Any]] = []
    repository = _materialization_repository()

    result = await materialize_batch_flight_features(
        batch=BATCH,
        flight_repository=_flight_repository(
            basis_by_id={EXTERNAL_ID: BASIS, "100066": BASIS, "100067": BASIS},
            pages=[_page(EXTERNAL_ID, "100066", "100067"), []],
        ),
        materialization_repository=repository,
        limit=2,
        on_progress=_async_recorder(progress),
    )

    assert result["succeeded_count"] == 2
    assert len(progress) == 2
    features_collection = repository.collection("flight_features")
    assert features_collection.replace_one.await_count == 2  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_batch_materialization_accepts_explicit_external_ids():
    flight_repository = _flight_repository(basis_by_id={"100066": BASIS})
    result = await materialize_batch_flight_features(
        batch=BATCH,
        flight_repository=flight_repository,
        materialization_repository=_materialization_repository(),
        external_ids=["100066"],
    )

    assert result["succeeded_count"] == 1
    # 定向重算不扫描整个批次
    flight_repository.collection.find.assert_not_called()


@pytest.mark.asyncio
async def test_iter_batch_external_ids_paginates_with_id_cursor():
    flight_repository = _flight_repository(
        basis_by_id={},
        pages=[_page(EXTERNAL_ID, "100066"), _page("100067"), []],
    )

    collected = [
        external_id
        async for external_id in iter_batch_external_ids(
            flight_repository,
            batch=BATCH,
            page_size=2,
        )
    ]

    assert collected == [EXTERNAL_ID, "100066", "100067"]
    second_filter = flight_repository.collection.find.call_args_list[1][0][0]
    assert second_filter["_id"] == {"$gt": 2}
    assert second_filter["source.batch"] == BATCH


@pytest.mark.asyncio
async def test_iter_batch_external_ids_rejects_invalid_page_size():
    with pytest.raises(ValueError, match="page_size"):
        async for _ in iter_batch_external_ids(
            _flight_repository(basis_by_id={}),
            batch=BATCH,
            page_size=0,
        ):
            pass


def test_basis_pipeline_uses_scat_wgs84_position_paths():
    """经纬度同在 I062/105（I062/100 是 Mode-3/A 码，不含坐标）。

    真实数据证明：写错路径会让所有航班因「无有效轨迹点」失败，
    因此这里固定住与 ScatFlightRepository 轨迹投影一致的字段名：
    过滤阶段与投影阶段各引用一次。
    """
    pipeline_text = json.dumps(FLIGHT_FEATURE_BASIS_PIPELINE, ensure_ascii=False)

    assert pipeline_text.count("$plots.I062/105.lat") == 2
    assert pipeline_text.count("$plots.I062/105.lon") == 2
    assert "$plots.I062/100" not in pipeline_text


def test_basis_pipeline_parses_scat_iso_time_strings():
    """SCAT 的 time_of_track 是无时区 ISO-8601 文本：必须先转日期再算时长。

    真实数据证明：直接用文本相减会报 "can't $subtract string from string"。
    """
    pipeline_text = json.dumps(FLIGHT_FEATURE_BASIS_PIPELINE, ensure_ascii=False)

    assert '"$convert": {"input": "$plots.time_of_track", "to": "date"' in pipeline_text
    assert '"$divide": [{"$subtract": ["$end_t", "$start_t"]}, 1000]' in pipeline_text


def test_basis_pipeline_extracts_reduce_accumulator_for_distance():
    """$reduce 的最终值是累加器文档：distance_m 必须取 $$acc.total。

    真实数据证明：直接返回 $reduce 会让 distance_m 变成字典，任务报
    "float() argument must be a string or a real number, not 'dict'"。
    """
    project_stage = next(
        stage
        for stage in FLIGHT_FEATURE_BASIS_PIPELINE
        if "$project" in stage and "distance_m" in stage["$project"]
    )
    distance_m = project_stage["$project"]["distance_m"]

    assert set(distance_m) == {"$let"}
    assert distance_m["$let"]["in"] == "$$acc.total"
    assert "$reduce" in distance_m["$let"]["vars"]["acc"]


def _async_recorder(target: list[dict[str, Any]]):
    async def record(update: dict[str, Any]) -> None:
        target.append(update)

    return record
