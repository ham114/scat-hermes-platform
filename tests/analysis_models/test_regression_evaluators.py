from datetime import UTC, datetime
from math import sqrt

import pytest

from scat_model_runtime import DatasetKind, ModelInputReference
from scat_model_runtime.regression_evaluators import RegressionErrorEvaluator
from scat_model_runtime.task_io import (
    ModelTaskDataError,
    RegressionPredictionSeries,
    RegressionTaskOutput,
)


def _output(*, with_expected: bool = True) -> RegressionTaskOutput:
    return RegressionTaskOutput(
        model_key="model",
        model_version="1",
        target_names=("lat", "lon", "value"),
        target_units={"lat": "degree", "lon": "degree", "value": "unit"},
        series=(
            RegressionPredictionSeries(
                input_ref=ModelInputReference(
                    dataset=DatasetKind.SCAT,
                    batch="batch",
                    external_id="flight-1",
                ),
                forecast_origin=datetime(2026, 9, 15, tzinfo=UTC),
                time_offsets_seconds=(10.0, 20.0),
                values=((0.0, 0.0, 2.0), (0.001, 0.0, 4.0)),
                expected_values=((0.0, 0.0, 1.0), (0.0, 0.0, 2.0))
                if with_expected
                else (),
            ),
        ),
    )


def test_regression_evaluator_calculates_target_and_trajectory_metrics():
    result = RegressionErrorEvaluator().evaluate(output=_output())

    value = next(item for item in result.target_errors if item.target_name == "value")
    assert value.rmse == pytest.approx(sqrt(2.5))
    assert value.mae == pytest.approx(1.5)
    assert value.sample_count == 2
    assert result.ade_metres == pytest.approx(55.597, rel=1e-3)
    assert result.fde_metres == pytest.approx(111.195, rel=1e-3)
    assert result.trajectory_errors[0].external_id == "flight-1"


def test_regression_evaluator_rejects_output_without_ground_truth():
    with pytest.raises(ModelTaskDataError, match="没有可与预测点对齐"):
        RegressionErrorEvaluator().evaluate(output=_output(with_expected=False))


def test_batch_evaluator_reports_series_without_aligned_truth_as_skipped():
    output = _output()
    first = output.series[0]
    second = first.model_copy(
        update={
            "input_ref": first.input_ref.model_copy(update={"external_id": "flight-2"}),
            "expected_values": ((None, None, None), (None, None, None)),
        }
    )

    result = RegressionErrorEvaluator().evaluate(
        output=output.model_copy(update={"series": (first, second)})
    )

    assert result.evaluated_series_count == 1
    assert result.skipped_series_count == 1
    assert [item.external_id for item in result.trajectory_errors] == ["flight-1"]
