"""M4-12 段级阶段标注物化测试：论文口径分段、幂等写入、增量与失败隔离。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from scat.materialization.repository import MaterializationRepository
from scat.materialization.tasks import (
    FLIGHT_PHASE_SEGMENTS_ALGORITHM_VERSION,
    materialize_batch_flight_phase_segments,
    materialize_flight_phase_segments,
)

BATCH = "scat20161015_20161021"
EXTERNAL_ID = "100002"
TRACK_SECONDS = 20 * 60.0


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
    """低空爬升 → 高空平飞 → 下降：三段方向/高度各不相同，RDP 应切成多段。"""
    track: list[dict[str, Any]] = []
    minute = 0
    for index in range(5):
        track.append(
            _track_point(minute + index, 59.00 + index * 0.01, 18.00, 20.0 + index * 10, 1)
        )
    minute += 5
    for index in range(11):
        track.append(_track_point(minute + index, 59.04, 18.01 + index * 0.01, 350.0, 0))
    minute += 11
    for index in range(5):
        track.append(
            _track_point(
                minute + index, 59.04 - index * 0.01, 18.12 + index * 0.01, 90.0 - index * 20, 2
            )
        )
    return track


TRACK: list[dict[str, Any]] = _build_track()


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
    points_by_id: dict[str, list[dict[str, Any]]] | None = None,
    pages: list[list[dict[str, Any]]] | None = None,
) -> Mock:
    tracks = dict(points_by_id or {})
    collection = Mock()
    collection.find = Mock(return_value=_find_cursor(pages or []))
    repository = Mock()
    repository.collection = collection
    repository.get_trajectory_points_for_phases = AsyncMock(
        side_effect=lambda *, batch, external_id, limit=20000: list(
            tracks.get(str(external_id), [])
        )
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
async def test_materialize_flight_phase_segments_writes_paper_phases():
    upsert_calls: list[tuple[dict, dict]] = []
    record = await materialize_flight_phase_segments(
        batch=BATCH,
        external_id=EXTERNAL_ID,
        flight_repository=_flight_repository(points_by_id={EXTERNAL_ID: TRACK}),
        materialization_repository=_materialization_repository(upserts=upsert_calls),
        generated_at=datetime(2026, 9, 18, 12, 0, tzinfo=UTC),
    )

    assert record.batch == BATCH
    assert record.external_id == EXTERNAL_ID
    assert record.point_count == len(TRACK)
    assert record.segment_count == len(record.segments)
    assert record.segment_count >= 3
    # 六类秒数按点区间归属，总和等于轨迹跨度
    assert sum(record.phase_seconds.values()) == pytest.approx(TRACK_SECONDS)
    phases = {segment.phase for segment in record.segments}
    assert phases & {"takeoff_initial_climb", "climb"}
    assert "high_cruise" in phases
    assert phases & {"descent", "approach"}
    assert record.dominant_phase in {
        "takeoff_initial_climb",
        "climb",
        "high_cruise",
        "midlow_level",
        "descent",
        "approach",
    }
    # 段顺序自左向右，且每段都有起止时间
    indexes = [segment.index for segment in record.segments]
    assert indexes == sorted(indexes)
    assert all(segment.start_time is not None for segment in record.segments)

    filter_doc, document = upsert_calls[0]
    assert filter_doc == {
        "batch": BATCH,
        "external_id": EXTERNAL_ID,
        "source_version": "scat-import-2",
        "algorithm_version": FLIGHT_PHASE_SEGMENTS_ALGORITHM_VERSION,
    }
    assert document["segment_count"] == record.segment_count
    assert document["segments"][0]["phase"] == record.segments[0].phase


@pytest.mark.asyncio
async def test_materialize_flight_phase_segments_rejects_empty_track():
    with pytest.raises(ValueError, match="无有效轨迹点"):
        await materialize_flight_phase_segments(
            batch=BATCH,
            external_id="no-such-flight",
            flight_repository=_flight_repository(points_by_id={}),
            materialization_repository=_materialization_repository(),
        )


@pytest.mark.asyncio
async def test_batch_flight_phase_segments_skips_existing_and_isolates_failures():
    upsert_calls: list[tuple[dict, dict]] = []
    repository = _materialization_repository(upserts=upsert_calls, existing={EXTERNAL_ID})

    result = await materialize_batch_flight_phase_segments(
        batch=BATCH,
        flight_repository=_flight_repository(
            points_by_id={EXTERNAL_ID: TRACK},
            pages=[_page(EXTERNAL_ID, "100066"), []],
        ),
        materialization_repository=repository,
        skip_existing=True,
    )

    assert result["skipped_count"] == 1
    assert result["failed_count"] == 1
    assert result["succeeded_count"] == 0
    assert result["failed"][0]["external_id"] == "100066"
    assert "无有效轨迹点" in result["failed"][0]["error"]
    # 已有结果不重复写入；增量过滤按 phase_segments 种类查询
    assert upsert_calls == []
    assert repository.existing_values.await_args.args[0] == "phase_segments"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_batch_flight_phase_segments_honours_limit_and_targets():
    result = await materialize_batch_flight_phase_segments(
        batch=BATCH,
        flight_repository=_flight_repository(points_by_id={EXTERNAL_ID: TRACK}),
        materialization_repository=_materialization_repository(),
        external_ids=[EXTERNAL_ID],
    )

    assert result["succeeded_count"] == 1
    assert result["failed_count"] == 0
