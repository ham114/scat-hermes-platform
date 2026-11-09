"""Map internal regression data to the single shared GBAR transport contract."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from gbar_core.transport import (
    FEATURES,
    MAX_REQUEST_BYTES,
    ErrorResponse,
    InferenceResponse,
    ModelProvenance,
    schema_digest,
)
from gbar_core.transport import (
    InferenceRequest as WireRequest,
)
from scat_model_runtime.backends.base import BackendError, BackendHealth, InferenceResult
from scat_model_runtime.packages.gbar_v23 import GbarV23RegressionPackage
from scat_model_runtime.task_io import (
    RegressionPredictionSeries,
    RegressionTaskInput,
    RegressionTaskOutput,
)


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def nonfinite(value):
        raise ValueError("non-finite JSON")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=nonfinite)


class GbarTransportCodec:
    def __init__(self, expected: ModelProvenance, *, required_device=None):
        if expected.schema_sha256 != schema_digest():
            raise ValueError("local transport Schema mismatch")
        self.expected = expected
        self.required_device = required_device

    @staticmethod
    def _identity(request):
        if (
            request.model_key,
            request.model_version,
            request.input_schema,
            request.output_schema,
        ) != ("scat-gbar-v23", "23", "regression-task-input/v1", "regression-task-output/v1"):
            raise BackendError("invalid_input")

    def encode(self, request):
        self._identity(request)
        try:
            source = RegressionTaskInput.model_validate(request.payload)
            value = WireRequest(
                contract_version="gbar-v23-request/v1",
                request_id=request.request_id,
                run_id=request.run_id,
                model_key=request.model_key,
                model_version=request.model_version,
                input_schema=request.input_schema,
                output_schema="scat-gbar-v23-prediction/v1",
                created_at=datetime.now(UTC),
                feature_names=source.feature_names,
                feature_units=source.feature_units,
                batch_size=source.batch_size,
                parameters=request.parameters,
                expected_provenance={
                    key: getattr(self.expected, key)
                    for key in (
                        "checkpoint_sha256",
                        "config_sha256",
                        "source_tree_sha256",
                        "schema_sha256",
                    )
                },
                series=[
                    {
                        "series_id": f"series-{i}",
                        "timestamps": s.timestamps,
                        "values": s.values,
                        "source_indices": s.source_indices or tuple(range(len(s.values))),
                    }
                    for i, s in enumerate(source.series)
                ],
            )
            raw = value.model_dump_json().encode()
            if len(raw) > MAX_REQUEST_BYTES:
                raise ValueError("request byte limit exceeded")
            return raw
        except (ValueError, TypeError, KeyError, IndexError, OverflowError):
            raise BackendError("invalid_input") from None

    def verify_metadata(self, metadata, release_id):
        try:
            if (
                not isinstance(metadata, dict)
                or metadata.get("contract_version") != "gbar-v23-metadata/v1"
            ):
                raise ValueError("metadata contract")
            if (
                metadata.get("model_key"),
                metadata.get("model_version"),
                metadata.get("input_schema"),
                metadata.get("output_schema"),
                metadata.get("release_id"),
            ) != (
                "scat-gbar-v23",
                "23",
                "regression-task-input/v1",
                "scat-gbar-v23-prediction/v1",
                release_id,
            ):
                raise ValueError("metadata identity")
            actual = ModelProvenance.model_validate(metadata.get("provenance"))
            if actual != self.expected:
                raise ValueError("metadata provenance")
            if (
                metadata.get("max_batch_size") != 8
                or metadata.get("max_series_points") != 500
                or metadata.get("max_total_points") != 2000
            ):
                raise ValueError("metadata capacity")
            if metadata.get("ready") is not True or metadata.get("state") != "ready":
                return BackendHealth(False, "not_ready")
            if self.required_device is not None and metadata.get("device") != self.required_device:
                return BackendHealth(False, "device_mismatch")
            return BackendHealth(
                True,
                metadata={"release_id": release_id, "provenance": actual.model_dump(mode="json")},
            )
        except (ValueError, TypeError, KeyError, IndexError, OverflowError):
            return BackendHealth(False, "metadata_mismatch")

    def decode(self, request, raw, status):
        self._identity(request)
        try:
            data = strict_json(raw)
            if status != 200:
                error = ErrorResponse.model_validate(data)
                if error.request_id not in {None, request.request_id} or error.run_id not in {
                    None,
                    request.run_id,
                }:
                    raise ValueError("error identity mismatch")
                from typing import get_args

                from gbar_core.transport import ErrorCode

                if error.error.code not in get_args(ErrorCode):
                    raise ValueError("unknown error")
                raise BackendError(error.error.code, execution_state=error.error.execution_state)
            value = InferenceResponse.model_validate(data)
            if (
                value.request_id != request.request_id
                or value.run_id != request.run_id
                or value.provenance != self.expected
            ):
                raise ValueError("response identity mismatch")
            source = RegressionTaskInput.model_validate(request.payload)
            if value.batch_size != source.batch_size:
                raise ValueError("response batch mismatch")
            series = []
            for i, (original, predicted) in enumerate(
                zip(source.series, value.series, strict=True)
            ):
                if predicted.series_id != f"series-{i}":
                    raise ValueError("response series mismatch")
                observed = predicted.observed_timestamps
                annotations = {
                    "adapter_output_schema": value.output_schema,
                    "model_input_schema": "scat-gbar-v35-input/v2",
                    "observed_start_time": observed[0].isoformat(),
                    "observed_end_time": observed[-1].isoformat(),
                    "quality": predicted.quality.model_dump(mode="json"),
                    "observed_trajectory": [
                        {
                            "time_step_s": (stamp - predicted.forecast_origin).total_seconds(),
                            "lat": row[0],
                            "lon": row[1],
                            "altitude": row[2],
                        }
                        for stamp, row in zip(observed, predicted.observed_values, strict=True)
                    ],
                }
                actual, expected = GbarV23RegressionPackage._align_actual_future(
                    original.annotations.get("actual_future_candidates"),
                    forecast_origin=predicted.forecast_origin,
                    offsets=list(predicted.time_offsets_seconds),
                )
                if actual:
                    annotations["actual_future_trajectory"] = actual
                if predicted.class_probabilities is not None:
                    annotations["class_probabilities"] = [list(predicted.class_probabilities)]
                series.append(
                    RegressionPredictionSeries(
                        input_ref=original.input_ref,
                        forecast_origin=predicted.forecast_origin,
                        time_offsets_seconds=predicted.time_offsets_seconds,
                        values=predicted.values,
                        expected_values=expected,
                        annotations=annotations,
                    )
                )
            output = RegressionTaskOutput(
                model_key=value.model_key,
                model_version=value.model_version,
                target_names=FEATURES,
                target_units=value.target_units,
                series=tuple(series),
            )
            result = InferenceResult(
                **{
                    key: getattr(request, key)
                    for key in (
                        "request_id",
                        "run_id",
                        "model_key",
                        "model_version",
                        "input_schema",
                        "output_schema",
                    )
                },
                payload=output.model_dump(mode="json"),
                provenance=value.provenance.model_dump(mode="json"),
            )
            result.verify_identity(request)
            return result
        except BackendError:
            raise
        except (ValueError, TypeError, KeyError, IndexError, OverflowError):
            raise BackendError("invalid_response", execution_state="unknown") from None
