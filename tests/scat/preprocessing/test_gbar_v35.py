from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import numpy as np
import pytest

from scat.preprocessing.gbar_v35 import (
    FPM_TO_MPS,
    SCAT_GBAR_FEATURES,
    ScatGbarDataError,
    ScatGbarV35DataProcessor,
    ScatGbarV35DataTool,
)


def _points(count: int = 80) -> list[dict[str, object]]:
    started = datetime(2016, 10, 20, 19, 30, tzinfo=UTC)
    return [
        {
            "source_index": index,
            "timestamp": (started + timedelta(seconds=5 * index)).isoformat(),
            "lat": 60.0 + index * 0.001,
            "lon": 20.0 + index * 0.001,
            "measured_flight_level": 300.0,
            "vx": 100.0,
            "vy": 50.0,
            "rocd": 600.0,
        }
        for index in range(count)
    ]


def test_processor_converts_units_and_reproduces_v35_tensor_shapes():
    prepared = ScatGbarV35DataProcessor().prepare(
        _points(),
        batch="scat20161015_20161021",
        external_id="100002",
    )

    assert prepared.feature_names == SCAT_GBAR_FEATURES
    assert prepared.feature_values.shape == (36, 6)
    assert prepared.absolute_binary.shape == (36, 75)
    assert prepared.absolute_binary.dtype == np.uint8
    assert prepared.interval_indices.shape == (36, 6)
    assert prepared.delta_indices.shape == (35, 6)
    assert prepared.delta_normalized.shape == (35, 6)
    assert prepared.feature_values[0, 2] == pytest.approx(9_144.0)
    assert prepared.feature_values[0, 5] == pytest.approx(600.0 * FPM_TO_MPS)
    assert prepared.source_indices == tuple(range(9, 80, 2))
    assert prepared.time_offsets_seconds == tuple(float(value) for value in range(0, 351, 10))
    assert prepared.quality.source_period_seconds == 5.0
    assert prepared.quality.model_period_seconds == 10.0
    assert np.all((prepared.delta_normalized >= 0.0) & (prepared.delta_normalized <= 1.0))


def _feature_quality(prepared, feature: str):
    return next(item for item in prepared.quality.feature_quality if item.feature == feature)


def test_processor_repairs_latest_position_spike_without_backtracking_window():
    points = _points()
    points[-1]["lat"] = 69.0

    prepared = ScatGbarV35DataProcessor().prepare(
        points,
        batch="batch",
        external_id="flight",
    )

    assert prepared.source_indices == tuple(range(9, 80, 2))
    assert prepared.feature_values[-1, 0] == pytest.approx(60.079)
    assert _feature_quality(prepared, "lat").spike == 1
    assert _feature_quality(prepared, "lat").extrapolated == 1


def test_processor_repairs_latest_velocity_spike_without_backtracking_window():
    points = _points()
    points[-1]["vx"] = 350.0

    prepared = ScatGbarV35DataProcessor().prepare(
        points,
        batch="batch",
        external_id="flight",
    )

    assert prepared.source_indices == tuple(range(9, 80, 2))
    assert prepared.feature_values[-1, 3] == pytest.approx(100.0)
    assert _feature_quality(prepared, "vx").spike == 1
    assert _feature_quality(prepared, "vx").extrapolated == 1


def test_processor_interpolates_missing_feature_before_selecting_window():
    points = _points()
    points[61]["vx"] = None

    prepared = ScatGbarV35DataProcessor().prepare(
        points,
        batch="batch",
        external_id="flight",
    )

    assert prepared.source_indices == tuple(range(9, 80, 2))
    assert prepared.feature_values[26, 3] == pytest.approx(100.0)
    assert _feature_quality(prepared, "vx").missing_or_non_finite == 1
    assert _feature_quality(prepared, "vx").interpolated == 1


def test_processor_refuses_to_cross_a_hard_time_gap_or_use_older_segment():
    points = _points(80)
    for point in points[40:]:
        point["timestamp"] = (
            datetime.fromisoformat(str(point["timestamp"])) + timedelta(seconds=60)
        ).isoformat()

    with pytest.raises(ScatGbarDataError, match="不会自动改用") as exc_info:
        ScatGbarV35DataProcessor().prepare(
            points,
            batch="batch",
            external_id="flight",
        )

    assert exc_info.value.code == "insufficient_latest_segment"
    assert exc_info.value.quality is not None
    assert exc_info.value.quality.hard_gap_count == 1


def test_processor_reports_unrecoverable_long_missing_run():
    points = _points()
    for index in range(50, 54):
        points[index]["rocd"] = None

    with pytest.raises(ScatGbarDataError) as exc_info:
        ScatGbarV35DataProcessor().prepare(
            points,
            batch="batch",
            external_id="flight",
        )

    assert exc_info.value.code == "unrecoverable_values"
    assert exc_info.value.quality is not None
    rocd = next(item for item in exc_info.value.quality.feature_quality if item.feature == "rocd")
    assert rocd.unrepaired == 4


@pytest.mark.asyncio
async def test_data_tool_reads_a_bounded_candidate_tail_from_repository():
    repository = AsyncMock()
    repository.get_gbar_input_candidates.return_value = {"points": _points()}
    tool = ScatGbarV35DataTool(repository=repository)

    prepared = await tool.prepare(
        batch="batch",
        external_id="flight",
        end_time="2016-10-20T20:00:00",
        source_limit=300,
    )

    repository.get_gbar_input_candidates.assert_awaited_once_with(
        batch="batch",
        external_id="flight",
        end_time="2016-10-20T20:00:00",
        limit=300,
    )
    assert prepared.summary()["absolute_binary_shape"] == [36, 75]


@pytest.mark.asyncio
async def test_data_tool_reports_missing_flight_with_stable_error_code():
    repository = AsyncMock()
    repository.get_gbar_input_candidates.return_value = None

    with pytest.raises(ScatGbarDataError) as exc_info:
        await ScatGbarV35DataTool(repository=repository).prepare(
            batch="batch",
            external_id="missing",
        )

    assert exc_info.value.code == "flight_not_found"


@pytest.mark.asyncio
async def test_data_tool_quality_inspection_uses_same_repair_pipeline():
    points = _points()
    points[61]["vx"] = None
    repository = AsyncMock()
    repository.get_gbar_input_candidates.return_value = {"points": points}

    result = await ScatGbarV35DataTool(repository=repository).inspect_quality(
        batch="batch",
        external_id="flight",
    )

    assert result["model_ready"] is True
    assert result["quality"]["repaired_values"] == 1
    vx = next(item for item in result["quality"]["features"] if item["feature"] == "vx")
    assert vx["missing_or_non_finite"] == 1
    assert vx["interpolated"] == 1


@pytest.mark.asyncio
async def test_data_tool_collection_quality_delegates_to_flight_service(monkeypatch):
    from scat.preprocessing.gbar_v35 import ScatGbarV35DataTool
    from scat.services import flight_service as fs_module

    fake_service = Mock()
    fake_service.get_model_data_quality_summary = AsyncMock(
        return_value={
            "status": "ok",
            "query": {"batch": "b"},
            "data": {"scope": "batch", "temporal_spikes_included": True},
        }
    )
    fake_class = Mock(return_value=fake_service)
    monkeypatch.setattr(fs_module, "ScatFlightService", fake_class)

    tool = ScatGbarV35DataTool()
    result = await tool.inspect_collection_quality(batch=" b ")

    fake_class.assert_called_once_with()
    fake_service.get_model_data_quality_summary.assert_awaited_once_with(batch=" b ")
    assert result["data"]["temporal_spikes_included"] is True
