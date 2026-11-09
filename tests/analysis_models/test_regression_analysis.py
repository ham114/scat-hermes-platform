from datetime import UTC, datetime

from agents.analysis_results import build_regression_model_analysis
from scat_model_runtime import DatasetKind, ModelInputReference
from scat_model_runtime.task_io import RegressionPredictionSeries, RegressionTaskOutput
from schema.analysis import ActionLinkBlock, TableBlock, Trajectory2dBlock


def _prediction(external_id: str, *, include_actual: bool = False):
    annotations = {
        "observed_trajectory": [
            {"time_step_s": -10.0, "lat": 60.0, "lon": 20.0, "altitude": 1000.0},
            {"time_step_s": 0.0, "lat": 60.1, "lon": 20.1, "altitude": 1100.0},
        ]
    }
    if include_actual:
        annotations["actual_future_trajectory"] = [
            {"time_step_s": 10.0, "lat": 60.2, "lon": 20.2, "altitude": 1200.0}
        ]
    return RegressionPredictionSeries(
        input_ref=ModelInputReference(
            dataset=DatasetKind.SCAT,
            batch="batch",
            external_id=external_id,
        ),
        forecast_origin=datetime(2016, 10, 20, 20, tzinfo=UTC),
        time_offsets_seconds=(10.0,),
        values=((60.15, 20.15, 1150.0),),
        annotations=annotations,
    )


def _output(*series):
    return RegressionTaskOutput(
        model_key="test-model",
        model_version="1",
        target_names=("lat", "lon", "measured_flight_level"),
        target_units={
            "lat": "degree",
            "lon": "degree",
            "measured_flight_level": "metre",
        },
        series=series,
    )


def test_single_prediction_map_contains_observed_predicted_and_actual_segments():
    result = build_regression_model_analysis(_output(_prediction("100001", include_actual=True)))

    assert result.schema_version == "1.4"
    trajectory = next(block for block in result.blocks if isinstance(block, Trajectory2dBlock))
    assert {point["segment"] for point in trajectory.points} == {
        "observed",
        "predicted",
        "actual_future",
    }


def test_batch_prediction_map_groups_flights_and_omits_actual_future():
    result = build_regression_model_analysis(
        _output(_prediction("100001", include_actual=True), _prediction("100002"))
    )

    trajectory = next(block for block in result.blocks if isinstance(block, Trajectory2dBlock))
    assert {point["trajectory_id"] for point in trajectory.points} == {"100001", "100002"}
    assert {point["segment"] for point in trajectory.points} == {"observed", "predicted"}
    assert any(isinstance(block, TableBlock) for block in result.blocks)


def test_persisted_prediction_contains_vue_visualization_action_link():
    artifact_id = "a" * 32
    result = build_regression_model_analysis(
        _output(_prediction("100001", include_actual=True)),
        artifact_id=artifact_id,
        thread_id="thread with space",
        visualization_base_url="http://localhost:5173/",
    )

    action = next(block for block in result.blocks if isinstance(block, ActionLinkBlock))
    assert action.artifact_id == artifact_id
    assert action.thread_id == "thread with space"
    assert action.url == (
        f"http://localhost:5173/visualizations/{artifact_id}/3d?thread_id=thread+with+space"
    )


def test_auxiliary_classification_keeps_real_probabilities_without_inventing_class_meanings():
    series = _prediction("sample")
    series.annotations["class_probabilities"] = [[0.1, 0.7, 0.2]]
    result = build_regression_model_analysis(_output(series))
    block = next(b for b in result.blocks if b.type == "classification")
    assert block.class_labels == ["类别索引 0", "类别索引 1", "类别索引 2"]
    assert block.items[0].label == "类别索引 1" and block.items[0].confidence == 0.7
    table = next(b for b in result.blocks if b.title == "辅助分类头原始概率")
    assert [r["probability"] for r in table.rows] == [0.1, 0.7, 0.2]
    assert "类别语义未登记" in result.context["classification_semantics"]
    series.annotations["class_probabilities"] = [[-0.1, 1.1], [0.2, 0.2]]
    assert not any(
        b.type == "classification" for b in build_regression_model_analysis(_output(series)).blocks
    )
