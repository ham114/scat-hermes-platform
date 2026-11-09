from datetime import UTC, datetime, timedelta

import pytest

from scat.preprocessing.gbar_v35 import SCAT_GBAR_FEATURES
from scat_model_runtime.adapters import ModelInferenceOutput
from scat_model_runtime.contracts import DatasetKind, ModelInputReference
from scat_model_runtime.packages.gbar_v23 import (
    GBAR_V23_INPUT_UNITS,
    GBAR_V23_OUTPUT_UNITS,
    GbarV23RegressionPackage,
)
from scat_model_runtime.task_io import (
    REGRESSION_TASK_OUTPUT_SCHEMA,
    RegressionInputSeries,
    RegressionTaskInput,
)


def _points(count: int = 80):
    started = datetime(2016, 10, 20, 19, 30, tzinfo=UTC)
    timestamps = tuple(started + timedelta(seconds=5 * index) for index in range(count))
    values = tuple(
        (
            60.0 + index * 0.001,
            20.0 + index * 0.001,
            300.0,
            100.0,
            50.0,
            600.0,
        )
        for index in range(count)
    )
    return timestamps, values


def _task_input(*, units=None) -> RegressionTaskInput:
    timestamps, values = _points()
    return RegressionTaskInput(
        feature_names=SCAT_GBAR_FEATURES,
        feature_units=units or GBAR_V23_INPUT_UNITS,
        series=(
            RegressionInputSeries(
                input_ref=ModelInputReference(
                    dataset=DatasetKind.SCAT,
                    batch="scat20161015_20161021",
                    external_id="100002",
                ),
                timestamps=timestamps,
                values=values,
                source_indices=tuple(range(len(values))),
            ),
        ),
    )


class _FakeGbarAdapter:
    def __init__(self):
        self.loaded = 0
        self.stopped = 0
        self.model_input = None

    def load(self):
        self.loaded += 1

    def predict(self, *, model_input, parameters):
        self.model_input = model_input
        window = model_input.payload
        points = []
        for index in range(1, 37):
            points.append(
                {
                    "time_offset_seconds": index * 10.0,
                    **{
                        feature: float(window.feature_values[-1, column])
                        for column, feature in enumerate(SCAT_GBAR_FEATURES)
                    },
                }
            )
        return ModelInferenceOutput(
            output_schema="scat-gbar-v23-prediction/v1",
            point_count=36,
            payload={"predicted_points": points, "class_probabilities": [[1.0]]},
        )

    def close(self):
        pass

    def shutdown(self):
        self.stopped += 1


def test_gbar_package_hides_preprocessing_inference_and_output_mapping():
    adapter = _FakeGbarAdapter()
    package = GbarV23RegressionPackage(adapter)  # type: ignore[arg-type]

    package.start()
    result = package.run(model_input=_task_input(), parameters={})
    package.stop()

    assert adapter.loaded == 1
    assert adapter.stopped == 1
    assert adapter.model_input.input_schema == "scat-gbar-v35-input/v2"
    assert adapter.model_input.payload.absolute_binary.shape == (36, 75)
    assert adapter.model_input.payload.delta_normalized.shape == (35, 6)
    assert result.schema_version == REGRESSION_TASK_OUTPUT_SCHEMA
    assert result.model_key == "scat-gbar-v23"
    assert result.target_names == SCAT_GBAR_FEATURES
    assert result.target_units == GBAR_V23_OUTPUT_UNITS
    assert result.batch_size == 1
    assert len(result.series[0].values) == 36
    assert result.series[0].time_offsets_seconds == tuple(
        float(value) for value in range(10, 361, 10)
    )
    assert len(result.series[0].annotations["observed_trajectory"]) == 36


def test_gbar_package_runs_multiple_series_in_one_loaded_batch():
    adapter = _FakeGbarAdapter()
    package = GbarV23RegressionPackage(adapter)  # type: ignore[arg-type]
    first = _task_input().series[0]
    second = first.model_copy(
        update={"input_ref": first.input_ref.model_copy(update={"external_id": "100003"})}
    )
    task_input = _task_input().model_copy(update={"series": (first, second)})

    package.start()
    result = package.run(model_input=task_input, parameters={})

    assert adapter.loaded == 1
    assert result.batch_size == 2
    assert [item.input_ref.external_id for item in result.series] == ["100002", "100003"]


def test_gbar_package_aligns_all_six_expected_targets_on_prediction_axis():
    adapter = _FakeGbarAdapter()
    package = GbarV23RegressionPackage(adapter)  # type: ignore[arg-type]
    source = _task_input().series[0]
    origin = source.timestamps[-1]
    source = source.model_copy(
        update={
            "annotations": {
                "actual_future_candidates": [
                    {
                        "timestamp": (origin + timedelta(seconds=10)).isoformat(),
                        "lat": 60.2,
                        "lon": 20.2,
                        "measured_flight_level": 1000.0,
                        "vx": 101.0,
                        "vy": 51.0,
                        "rocd": 5.0,
                    }
                ]
            }
        }
    )

    result = package.run(
        model_input=_task_input().model_copy(update={"series": (source,)}),
        parameters={},
    )

    assert result.series[0].expected_values[0] == (
        60.2,
        20.2,
        1000.0,
        101.0,
        51.0,
        5.0,
    )
    assert result.series[0].expected_values[1] == (None,) * 6


def test_gbar_package_rejects_task_input_with_wrong_units():
    units = dict(GBAR_V23_INPUT_UNITS)
    units["rocd"] = "metre_per_second"
    package = GbarV23RegressionPackage(_FakeGbarAdapter())  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="units mismatch"):
        package.run(model_input=_task_input(units=units), parameters={})


def test_regression_input_rejects_rows_that_do_not_match_feature_width():
    timestamps, values = _points()
    bad_values = (values[0][:-1], *values[1:])

    with pytest.raises(ValueError, match="feature_names width"):
        RegressionTaskInput(
            feature_names=SCAT_GBAR_FEATURES,
            feature_units=GBAR_V23_INPUT_UNITS,
            series=(
                RegressionInputSeries(
                    input_ref=ModelInputReference(
                        dataset=DatasetKind.SCAT,
                        batch="batch",
                        external_id="flight",
                    ),
                    timestamps=timestamps,
                    values=bad_values,
                ),
            ),
        )
