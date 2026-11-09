"""GBAR v23 as a black-box implementation of the regression task contract."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from scat.preprocessing.gbar_v35 import (
    SCAT_GBAR_FEATURES,
    ScatGbarDataError,
    ScatGbarV35DataProcessor,
)
from scat_model_runtime.alignment_core import align_actual_future, optional_number
from scat_model_runtime.contracts import DatasetKind, ModelParameterValue
from scat_model_runtime.gbar_v23_adapter import (
    GBAR_V23_INPUT_SCHEMA,
    GbarV23SubprocessAdapter,
)
from scat_model_runtime.pipelines import PreparedModelInput
from scat_model_runtime.task_io import (
    ModelTaskDataError,
    RegressionInputSeries,
    RegressionPredictionSeries,
    RegressionTaskInput,
    RegressionTaskOutput,
)

GBAR_V23_MODEL_KEY = "scat-gbar-v23"
GBAR_V23_MODEL_VERSION = "23"
GBAR_V23_INPUT_UNITS = {
    "lat": "degree",
    "lon": "degree",
    "measured_flight_level": "hundred_feet",
    "vx": "metre_per_second",
    "vy": "metre_per_second",
    "rocd": "feet_per_minute",
}
GBAR_V23_OUTPUT_UNITS = {
    "lat": "degree",
    "lon": "degree",
    "measured_flight_level": "metre",
    "vx": "metre_per_second",
    "vy": "metre_per_second",
    "rocd": "metre_per_second",
}


class GbarV23RegressionPackage:
    """Hide GBAR-specific repair, encoding, inference and decoding."""

    def __init__(
        self,
        adapter: GbarV23SubprocessAdapter,
        processor: ScatGbarV35DataProcessor | None = None,
    ) -> None:
        self.adapter = adapter
        self.processor = processor or ScatGbarV35DataProcessor()

    def start(self) -> None:
        self.adapter.load()

    def run(
        self,
        *,
        model_input: RegressionTaskInput,
        parameters: Mapping[str, ModelParameterValue],
    ) -> RegressionTaskOutput:
        if parameters:
            raise ValueError("GBAR v23 does not accept runtime parameter overrides")
        if model_input.feature_names != SCAT_GBAR_FEATURES:
            raise ValueError("GBAR v23 regression input feature order mismatch")
        if model_input.feature_units != GBAR_V23_INPUT_UNITS:
            raise ValueError("GBAR v23 regression input units mismatch")
        predictions = []
        for series in model_input.series:
            try:
                predictions.append(self._run_series(series))
            except ScatGbarDataError as exc:
                raise ModelTaskDataError(f"轨迹 {series.input_ref.external_id}：{exc}") from exc
        return RegressionTaskOutput(
            model_key=GBAR_V23_MODEL_KEY,
            model_version=GBAR_V23_MODEL_VERSION,
            target_names=SCAT_GBAR_FEATURES,
            target_units=GBAR_V23_OUTPUT_UNITS,
            series=tuple(predictions),
        )

    def _run_series(self, series: RegressionInputSeries) -> RegressionPredictionSeries:
        if series.input_ref.dataset != DatasetKind.SCAT or series.input_ref.batch is None:
            raise ValueError("GBAR v23 requires SCAT trajectory references")
        source_indices = series.source_indices or tuple(range(len(series.values)))
        points = [
            {
                "source_index": source_index,
                "timestamp": timestamp,
                **dict(zip(SCAT_GBAR_FEATURES, values, strict=True)),
            }
            for source_index, timestamp, values in zip(
                source_indices,
                series.timestamps,
                series.values,
                strict=True,
            )
        ]
        window = self.processor.prepare(
            points,
            batch=series.input_ref.batch,
            external_id=series.input_ref.external_id,
        )
        inference = self.adapter.predict(
            model_input=PreparedModelInput(
                input_schema=GBAR_V23_INPUT_SCHEMA,
                feature_names=SCAT_GBAR_FEATURES,
                point_count=len(window.timestamps),
                payload=window,
            ),
            parameters={},
        )
        payload = inference.payload
        if not isinstance(payload, dict):
            raise RuntimeError("GBAR v23 adapter returned an invalid payload")
        raw_points = payload.get("predicted_points")
        if not isinstance(raw_points, list) or not raw_points:
            raise RuntimeError("GBAR v23 adapter returned no prediction points")
        offsets: list[float] = []
        values: list[tuple[float, ...]] = []
        for point in raw_points:
            if not isinstance(point, dict):
                raise RuntimeError("GBAR v23 adapter returned an invalid prediction point")
            offsets.append(float(point["time_offset_seconds"]))
            values.append(tuple(float(point[name]) for name in SCAT_GBAR_FEATURES))

        annotations: dict[str, Any] = {
            "adapter_output_schema": inference.output_schema,
            "model_input_schema": GBAR_V23_INPUT_SCHEMA,
            "observed_start_time": window.timestamps[0].isoformat(),
            "observed_end_time": window.timestamps[-1].isoformat(),
            "quality": window.quality.as_dict(),
            "observed_trajectory": [
                {
                    "time_step_s": (timestamp - window.timestamps[-1]).total_seconds(),
                    "lat": float(row[0]),
                    "lon": float(row[1]),
                    "altitude": float(row[2]),
                }
                for timestamp, row in zip(
                    window.timestamps,
                    window.feature_values,
                    strict=True,
                )
            ],
        }
        actual, expected_values = self._align_actual_future(
            series.annotations.get("actual_future_candidates"),
            forecast_origin=window.timestamps[-1],
            offsets=offsets,
        )
        if actual:
            annotations["actual_future_trajectory"] = actual
        class_probabilities = payload.get("class_probabilities")
        if class_probabilities is not None:
            annotations["class_probabilities"] = class_probabilities
        return RegressionPredictionSeries(
            input_ref=series.input_ref,
            forecast_origin=window.timestamps[-1],
            time_offsets_seconds=tuple(offsets),
            values=tuple(values),
            expected_values=expected_values,
            annotations=annotations,
        )

    @staticmethod
    def _align_actual_future(
        raw_candidates: Any,
        *,
        forecast_origin: datetime,
        offsets: list[float],
    ) -> tuple[list[dict[str, float]], tuple[tuple[float | None, ...], ...]]:
        # 对齐规则由共享评估核单一实现（D-22）：GBAR 与预测对比包必须逐行一致。
        return align_actual_future(
            raw_candidates,
            forecast_origin=forecast_origin,
            offsets=offsets,
            feature_names=SCAT_GBAR_FEATURES,
        )

    @staticmethod
    def _optional_number(value: Any) -> float | None:
        return optional_number(value)

    def stop(self) -> None:
        self.adapter.shutdown()


__all__ = [
    "GBAR_V23_INPUT_UNITS",
    "GBAR_V23_MODEL_KEY",
    "GBAR_V23_MODEL_VERSION",
    "GBAR_V23_OUTPUT_UNITS",
    "GbarV23RegressionPackage",
]
