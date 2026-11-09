from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from scat_model_runtime.contracts import (
    ComponentReference,
    DatasetKind,
    ModelInputReference,
    ModelTaskRequest,
    ModelTaskType,
)
from scat_model_runtime.packages.gbar_v23 import GBAR_V23_INPUT_UNITS
from scat_model_runtime.resolvers.scat_gbar import ScatGbarRegressionInputResolver


@pytest.mark.asyncio
async def test_resolver_returns_unified_regression_input_and_normalizes_utc():
    repository = AsyncMock()
    repository.get_gbar_input_candidates.return_value = {
        "points": [
            {
                "source_index": index,
                "timestamp": f"2016-10-20T20:00:{index:02d}",
                "lat": 60.0,
                "lon": 20.0,
                "measured_flight_level": 300.0,
                "vx": 100.0,
                "vy": 50.0,
                "rocd": None if index == 1 else 600.0,
            }
            for index in range(2)
        ]
    }
    repository.get_gbar_future_positions.return_value = []
    resolver = ScatGbarRegressionInputResolver(repository=repository)
    request = ModelTaskRequest(
        task_type=ModelTaskType.TRAJECTORY_PREDICTION,
        model_key="scat-gbar-v23",
        input_ref=ModelInputReference(
            dataset=DatasetKind.SCAT,
            batch="batch",
            external_id="100002",
            end_time=datetime(2016, 10, 20, 20, 1, tzinfo=UTC),
            limit=500,
        ),
        confirmed=True,
    )

    result = await resolver.resolve(request=request)

    assert result.feature_units == GBAR_V23_INPUT_UNITS
    assert result.series[0].input_ref == request.input_ref
    assert result.series[0].timestamps[0].tzinfo == UTC
    assert result.series[0].values[1][-1] is None
    repository.get_gbar_input_candidates.assert_awaited_once_with(
        batch="batch",
        external_id="100002",
        end_time="2016-10-20T20:01:00.000000",
        limit=500,
    )
    repository.get_gbar_future_positions.assert_awaited_once()


@pytest.mark.asyncio
async def test_batch_resolver_does_not_load_unused_actual_future_points():
    repository = AsyncMock()
    repository.get_gbar_input_candidates.return_value = {
        "points": [
            {
                "source_index": index,
                "timestamp": f"2016-10-20T20:00:{index:02d}",
                "lat": 60.0,
                "lon": 20.0,
                "measured_flight_level": 300.0,
                "vx": 100.0,
                "vy": 50.0,
                "rocd": 600.0,
            }
            for index in range(2)
        ]
    }
    references = tuple(
        ModelInputReference(
            dataset=DatasetKind.SCAT,
            batch="batch",
            external_id=external_id,
            end_time=datetime(2016, 10, 20, 20, 1, tzinfo=UTC),
            limit=500,
        )
        for external_id in ("100002", "100003")
    )
    request = ModelTaskRequest(
        task_type=ModelTaskType.TRAJECTORY_PREDICTION,
        model_key="scat-gbar-v23",
        input_refs=references,
        confirmed=True,
    )

    result = await ScatGbarRegressionInputResolver(repository=repository).resolve(request=request)

    assert result.batch_size == 2
    repository.get_gbar_future_positions.assert_not_awaited()


@pytest.mark.asyncio
async def test_batch_evaluation_loads_and_converts_ground_truth_for_every_series():
    repository = AsyncMock()
    repository.get_gbar_input_candidates.return_value = {
        "points": [
            {
                "source_index": index,
                "timestamp": f"2016-10-20T20:00:{index:02d}",
                "lat": 60.0,
                "lon": 20.0,
                "measured_flight_level": 300.0,
                "vx": 100.0,
                "vy": 50.0,
                "rocd": 600.0,
            }
            for index in range(2)
        ]
    }
    repository.get_gbar_future_positions.return_value = [
        {
            "timestamp": "2016-10-20T20:01:10",
            "lat": 600.1,
            "lon": 20.1,
            "measured_flight_level": 100.0,
            "vx": 101.0,
            "vy": 51.0,
            "rocd": 1000.0,
        }
    ]
    references = tuple(
        ModelInputReference(
            dataset=DatasetKind.SCAT,
            batch="batch",
            external_id=external_id,
            end_time=datetime(2016, 10, 20, 20, 1, tzinfo=UTC),
            limit=500,
        )
        for external_id in ("100002", "100003")
    )
    request = ModelTaskRequest(
        task_type=ModelTaskType.TRAJECTORY_PREDICTION,
        model_key="scat-gbar-v23",
        input_refs=references,
        evaluator=ComponentReference(key="regression-errors", version="1"),
        confirmed=True,
    )

    result = await ScatGbarRegressionInputResolver(repository=repository).resolve(request=request)

    assert repository.get_gbar_future_positions.await_count == 2
    actual = result.series[0].annotations["actual_future_candidates"][0]
    assert actual["lat"] is None
    assert actual["measured_flight_level"] == pytest.approx(3048.0)
    assert actual["rocd"] == pytest.approx(5.08)


@pytest.mark.asyncio
async def test_evaluation_without_cutoff_automatically_reserves_tail_truth():
    repository = AsyncMock()
    origin = datetime(2016, 10, 20, 20, tzinfo=UTC)
    holdout = [
        {
            "source_index": index + 300,
            "timestamp": origin + timedelta(seconds=index * 5),
            "lat": 60.0,
            "lon": 20.0,
            "measured_flight_level": 300.0,
            "vx": 100.0,
            "vy": 50.0,
            "rocd": 600.0,
        }
        for index in range(100)
    ]
    observed = [
        {
            "source_index": index,
            "timestamp": origin - timedelta(seconds=(71 - index) * 5),
            "lat": 60.0,
            "lon": 20.0,
            "measured_flight_level": 300.0,
            "vx": 100.0,
            "vy": 50.0,
            "rocd": 600.0,
        }
        for index in range(71)
    ]
    repository.get_gbar_input_candidates.side_effect = [
        {"points": holdout},
        {"points": observed},
    ]
    repository.get_gbar_future_positions.return_value = holdout[1:]
    request = ModelTaskRequest(
        task_type=ModelTaskType.TRAJECTORY_PREDICTION,
        model_key="scat-gbar-v23",
        input_ref=ModelInputReference(
            dataset=DatasetKind.SCAT,
            batch="batch",
            external_id="100002",
        ),
        evaluator=ComponentReference(key="regression-errors", version="1"),
        confirmed=True,
    )

    result = await ScatGbarRegressionInputResolver(repository=repository).resolve(
        request=request
    )

    series = result.series[0]
    assert len(series.values) == 71
    assert series.input_ref.end_time is None
    assert series.annotations["evaluation_window"] == "automatic_tail_holdout"
    assert series.annotations["evaluation_cutoff"] == origin.isoformat()
    assert len(series.annotations["actual_future_candidates"]) == 99
    assert repository.get_gbar_input_candidates.await_count == 2
    repository.get_gbar_future_positions.assert_awaited_once_with(
        batch="batch",
        external_id="100002",
        after_time="2016-10-20T20:00:00.000000",
        limit=100,
    )
