from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from scat.preprocessing.gbar_v35 import SCAT_GBAR_FEATURES
from scat_model_runtime.contracts import (
    DatasetKind,
    ModelInputReference,
    ModelTaskRequest,
    ModelTaskType,
)
from scat_model_runtime.resolvers.scat_comparison import ScatComparisonInputResolver
from scat_model_runtime.task_io import RegressionInputSeries

START = datetime(2016, 10, 20, 19, 0, tzinfo=UTC)


def _reference(external_id: str = "100002", *, end_time=None) -> ModelInputReference:
    return ModelInputReference(
        dataset=DatasetKind.SCAT,
        batch="scat20161015_20161021",
        external_id=external_id,
        end_time=end_time,
        limit=200,
    )


def _request(*references: ModelInputReference, parameters=None) -> ModelTaskRequest:
    return ModelTaskRequest(
        task_type=ModelTaskType.MODEL_COMPARISON,
        model_key="scat-comparison-v1",
        input_refs=references or (_reference(),),
        parameters=parameters or {},
        confirmed=True,
    )


def _scan_result(count: int = 175):
    return {
        "points": [
            {"timestamp": (START + timedelta(seconds=5 * index)).isoformat()}
            for index in range(count)
        ]
    }


class _FakeGbarResolver:
    def __init__(self) -> None:
        self.resolve_reference = AsyncMock(side_effect=self._resolve)

    @staticmethod
    async def _resolve(reference, *, include_actual_future):
        assert include_actual_future is True
        origin = reference.end_time
        timestamps = tuple(origin - timedelta(seconds=5 * index) for index in range(70, -1, -1))
        values = tuple((60.0, 20.0, 100.0, 100.0, 0.0, 0.0) for _ in timestamps)
        return RegressionInputSeries(
            input_ref=reference,
            timestamps=timestamps,
            values=values,
            source_indices=tuple(range(71)),
            annotations={
                "actual_future_candidates": [
                    {
                        "timestamp": (origin + timedelta(seconds=10)).isoformat(),
                        "lat": 60.01,
                        "lon": 20.01,
                        "measured_flight_level": 3_000.0,
                    },
                    {
                        "timestamp": (origin + timedelta(seconds=20)).isoformat(),
                        "lat": 60.02,
                        "lon": 20.02,
                        "measured_flight_level": 3_000.0,
                    },
                ]
            },
        )


def _snapshot(timestamp: str):
    origin = datetime.fromisoformat(timestamp).replace(tzinfo=UTC)
    return {
        "snapshot": {
            "time_stamp": origin.isoformat(),
            "route": [
                {
                    "eto": origin.isoformat(),
                    "lat": 60.0,
                    "lon": 20.0,
                    "afl_value": 100.0,
                    "afl_unit": "F",
                },
                {
                    "eto": (origin + timedelta(seconds=400)).isoformat(),
                    "lat": 60.4,
                    "lon": 20.4,
                    "afl_value": 100.0,
                    "afl_unit": "F",
                },
            ],
        }
    }


@pytest.mark.asyncio
async def test_resolver_builds_three_even_cutoffs_with_truth_and_cfpl():
    repository = AsyncMock()
    repository.get_gbar_input_candidates.return_value = _scan_result()
    repository.get_prediction_snapshot_at_or_before.side_effect = lambda **kwargs: _snapshot(
        kwargs["timestamp"]
    )
    gbar_resolver = _FakeGbarResolver()
    resolver = ScatComparisonInputResolver(repository, gbar_resolver)  # type: ignore[arg-type]

    result = await resolver.resolve(request=_request())

    assert result.feature_names == SCAT_GBAR_FEATURES
    assert result.batch_size == 3
    assert result.excluded_counts == {}
    assert len({sample.sample_id for sample in result.samples}) == 3
    assert all(len(sample.observed_values) == 71 for sample in result.samples)
    assert all(len(sample.actual_future_candidates) == 2 for sample in result.samples)
    assert all(
        sample.annotations["cutoff_selection"] == "evenly_spaced" for sample in result.samples
    )
    assert repository.get_prediction_snapshot_at_or_before.await_count == 3


@pytest.mark.asyncio
async def test_resolver_uses_one_explicit_cutoff_without_scanning():
    repository = AsyncMock()
    repository.get_prediction_snapshot_at_or_before.side_effect = lambda **kwargs: _snapshot(
        kwargs["timestamp"]
    )
    resolver = ScatComparisonInputResolver(  # type: ignore[arg-type]
        repository,
        _FakeGbarResolver(),
    )
    cutoff = START + timedelta(seconds=500)

    result = await resolver.resolve(request=_request(_reference(end_time=cutoff)))

    assert result.batch_size == 1
    assert result.samples[0].forecast_origin == cutoff
    assert result.samples[0].annotations["cutoff_selection"] == "explicit_end_time"
    repository.get_gbar_input_candidates.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolver_skips_all_cutoffs_for_a_flight_without_cfpl():
    repository = AsyncMock()
    repository.get_gbar_input_candidates.return_value = _scan_result()

    async def snapshot_for_flight(**kwargs):
        if kwargs["external_id"] == "100003":
            return None
        return _snapshot(kwargs["timestamp"])

    repository.get_prediction_snapshot_at_or_before.side_effect = snapshot_for_flight
    resolver = ScatComparisonInputResolver(  # type: ignore[arg-type]
        repository,
        _FakeGbarResolver(),
    )

    result = await resolver.resolve(request=_request(_reference("100002"), _reference("100003")))

    assert result.batch_size == 3
    assert {sample.input_ref.external_id for sample in result.samples} == {"100002"}
    assert result.excluded_counts == {"cfpl_unavailable": 3}


@pytest.mark.asyncio
async def test_resolver_rejects_unbounded_cutoff_parameters():
    resolver = ScatComparisonInputResolver(AsyncMock(), _FakeGbarResolver())  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="between 1 and 3"):
        await resolver.resolve(request=_request(parameters={"cutoffs_per_flight": 4}))


@pytest.mark.asyncio
async def test_resolver_deduplicates_cfpl_eto_before_building_strict_sample():
    repository = AsyncMock()
    repository.get_gbar_input_candidates.return_value = _scan_result()

    def duplicate_snapshot(**kwargs):
        result = _snapshot(kwargs["timestamp"])
        route = result["snapshot"]["route"]
        route.insert(1, {**route[0], "lat": 60.1})
        return result

    repository.get_prediction_snapshot_at_or_before.side_effect = duplicate_snapshot
    resolver = ScatComparisonInputResolver(  # type: ignore[arg-type]
        repository,
        _FakeGbarResolver(),
    )

    result = await resolver.resolve(request=_request(parameters={"cutoffs_per_flight": 1}))

    assert len(result.samples[0].cfpl_points) == 2
    assert result.samples[0].cfpl_points[0].latitude_deg == 60.1
