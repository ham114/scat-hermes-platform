from datetime import UTC, datetime, timedelta

import pytest

from scat.preprocessing.gbar_v35 import SCAT_GBAR_FEATURES
from scat_model_runtime.contracts import DatasetKind, ModelInputReference
from scat_model_runtime.packages.comparison_v1 import ComparisonV1ModelPackage
from scat_model_runtime.packages.gbar_v23 import (
    GBAR_V23_INPUT_UNITS,
    GBAR_V23_OUTPUT_UNITS,
)
from scat_model_runtime.task_io import (
    ComparisonSampleInput,
    ComparisonTaskInput,
    ComparisonTrajectoryPoint,
    RegressionPredictionSeries,
    RegressionTaskOutput,
)

ORIGIN = datetime(2016, 10, 20, 20, 0, tzinfo=UTC)


def _point(seconds: float, lat: float, lon: float = 20.0) -> ComparisonTrajectoryPoint:
    return ComparisonTrajectoryPoint(
        timestamp=ORIGIN + timedelta(seconds=seconds),
        latitude_deg=lat,
        longitude_deg=lon,
        altitude_m=3_000.0,
    )


def _sample(external_id: str = "100002") -> ComparisonSampleInput:
    return ComparisonSampleInput(
        sample_id=f"{external_id}:cutoff-1",
        input_ref=ModelInputReference(
            dataset=DatasetKind.SCAT,
            batch="scat20161015_20161021",
            external_id=external_id,
        ),
        forecast_origin=ORIGIN,
        observed_timestamps=(ORIGIN - timedelta(seconds=5), ORIGIN),
        observed_values=(
            (59.999, 19.999, 100.0, 100.0, 0.0, 0.0),
            (60.0, 20.0, 100.0, 100.0, 0.0, 0.0),
        ),
        source_indices=(69, 70),
        actual_future_candidates=(_point(10, 60.001), _point(20, 60.002)),
        cfpl_points=(_point(0, 60.0), _point(30, 60.003)),
    )


def _task_input(*samples: ComparisonSampleInput) -> ComparisonTaskInput:
    return ComparisonTaskInput(
        feature_names=SCAT_GBAR_FEATURES,
        feature_units=GBAR_V23_INPUT_UNITS,
        samples=samples or (_sample(),),
    )


class _FakeGbarPackage:
    def __init__(self, *, missing_truth_for: str | None = None) -> None:
        self.missing_truth_for = missing_truth_for
        self.started = 0
        self.stopped = 0

    def start(self) -> None:
        self.started += 1

    def run(self, *, model_input, parameters):
        assert parameters == {}
        output = []
        for series in model_input.series:
            missing = series.input_ref.external_id == self.missing_truth_for
            expected = (
                ((None,) * 6, (None,) * 6)
                if missing
                else (
                    (60.001, 20.0, 3_000.0, None, None, None),
                    (60.002, 20.0, 3_000.0, None, None, None),
                )
            )
            output.append(
                RegressionPredictionSeries(
                    input_ref=series.input_ref,
                    forecast_origin=ORIGIN,
                    time_offsets_seconds=(10.0, 20.0),
                    values=(
                        (60.0011, 20.0, 3_000.0, 0.0, 0.0, 0.0),
                        (60.0021, 20.0, 3_000.0, 0.0, 0.0, 0.0),
                    ),
                    expected_values=expected,
                    annotations={
                        "observed_trajectory": [
                            {
                                "time_step_s": -5.0,
                                "lat": 59.999,
                                "lon": 19.999,
                                "altitude": 3_000.0,
                            },
                            {
                                "time_step_s": 0.0,
                                "lat": 60.0,
                                "lon": 20.0,
                                "altitude": 3_000.0,
                            },
                        ]
                    },
                )
            )
        return RegressionTaskOutput(
            model_key="scat-gbar-v23",
            model_version="23",
            target_names=SCAT_GBAR_FEATURES,
            target_units=GBAR_V23_OUTPUT_UNITS,
            series=tuple(output),
        )

    def stop(self) -> None:
        self.stopped += 1


def test_comparison_package_runs_four_methods_on_the_gbar_axis():
    gbar = _FakeGbarPackage()
    package = ComparisonV1ModelPackage(gbar)  # type: ignore[arg-type]

    package.start()
    result = package.run(model_input=_task_input(), parameters={})
    package.stop()

    assert gbar.started == 1
    assert gbar.stopped == 1
    assert result.model_key == "scat-comparison-v1"
    assert result.protocol_version == "gbar-align-v1"
    assert len(result.sample_set_sha256) == 64
    assert result.excluded_counts == {}
    sample = result.samples[0]
    assert sample.time_offsets_seconds == (10.0, 20.0)
    assert {item.method_id for item in sample.method_results} == {
        "scat-gbar-v23",
        "kalman-cv",
        "constant-velocity-heading",
        "cfpl",
    }
    expected_times = (ORIGIN + timedelta(seconds=10), ORIGIN + timedelta(seconds=20))
    assert tuple(point.timestamp for point in sample.actual_points) == expected_times
    assert all(
        tuple(point.timestamp for point in method.predicted_points) == expected_times
        for method in sample.method_results
    )
    for method in sample.method_results:
        assert len(method.horizontal_errors_metres) == len(expected_times)
        assert method.ade_metres == pytest.approx(
            sum(method.horizontal_errors_metres) / len(method.horizontal_errors_metres)
        )
        assert method.fde_metres == pytest.approx(method.horizontal_errors_metres[-1])
    assert {item.method_id for item in result.method_summaries} == {
        "scat-gbar-v23",
        "kalman-cv",
        "constant-velocity-heading",
        "cfpl",
    }


def test_comparison_package_excludes_a_sample_for_all_methods_when_truth_is_missing():
    package = ComparisonV1ModelPackage(  # type: ignore[arg-type]
        _FakeGbarPackage(missing_truth_for="100003")
    )

    result = package.run(
        model_input=_task_input(_sample("100002"), _sample("100003")),
        parameters={},
    )

    assert [item.input_ref.external_id for item in result.samples] == ["100002"]
    assert result.excluded_counts == {"truth_missing": 1}
    assert all(item.sample_count == 1 for item in result.method_summaries)


def test_comparison_package_rejects_runtime_parameter_overrides():
    package = ComparisonV1ModelPackage(_FakeGbarPackage())  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="does not accept"):
        package.run(model_input=_task_input(), parameters={"extra": True})


def test_comparison_package_accepts_resolver_only_cutoff_parameter():
    package = ComparisonV1ModelPackage(_FakeGbarPackage())  # type: ignore[arg-type]

    result = package.run(
        model_input=_task_input(),
        parameters={"cutoffs_per_flight": 1},
    )

    assert result.batch_size == 1
