"""Closed JSON codec for server-only model state; never deserializes arbitrary classes."""

from __future__ import annotations

import math

from pydantic import BaseModel

from scat_model_runtime import (
    ComparisonTaskInput,
    ComparisonTaskOutput,
    ModelEvaluation,
    ModelInferenceOutput,
    ModelInputReference,
    ModelSpec,
    ModelTaskRequest,
    PreparedModelInput,
    RegressionEvaluation,
    RegressionTaskInput,
    RegressionTaskOutput,
    ResolvedModelInput,
)

TYPES = {
    c.__name__: c
    for c in (
        ModelTaskRequest,
        ModelSpec,
        ModelInputReference,
        ResolvedModelInput,
        PreparedModelInput,
        ModelInferenceOutput,
        ModelEvaluation,
        RegressionTaskInput,
        RegressionTaskOutput,
        RegressionEvaluation,
        ComparisonTaskInput,
        ComparisonTaskOutput,
    )
}


def encode(value):
    if isinstance(value, BaseModel):
        name = type(value).__name__
        if name not in TYPES:
            raise ValueError("unsupported_step_payload")
        # Runtime payload models can contain arrays; ordinary contracts use JSON normalization.
        if name in {"ResolvedModelInput", "PreparedModelInput", "ModelInferenceOutput"}:
            body = {key: encode(getattr(value, key)) for key in type(value).model_fields}
        else:
            body = value.model_dump(mode="json")
        return {"$model": name, "body": body}
    if isinstance(value, tuple):
        return {"$tuple": [encode(v) for v in value]}
    if isinstance(value, list):
        return [encode(v) for v in value]
    if isinstance(value, dict):
        if any(not isinstance(k, str) or k.startswith("$") for k in value):
            raise ValueError("unsupported_step_payload_key")
        return {k: encode(v) for k, v in value.items()}
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    # Lazy numpy import keeps the Hermes bridge independent of numerical dependencies.
    import numpy as np

    if isinstance(value, np.ndarray) and value.dtype.kind in "biuf" and value.size <= 1_000_000:
        if not np.isfinite(value).all():
            raise ValueError("nonfinite_step_array")
        return {"$array": value.tolist(), "dtype": value.dtype.str, "shape": list(value.shape)}
    raise ValueError("unsupported_step_payload")


def decode(value):
    if isinstance(value, list):
        return [decode(v) for v in value]
    if not isinstance(value, dict):
        return value
    if "$model" in value:
        if set(value) != {"$model", "body"} or value["$model"] not in TYPES:
            raise ValueError("invalid_step_payload_type")
        return TYPES[value["$model"]].model_validate(decode(value["body"]))
    if "$tuple" in value:
        if set(value) != {"$tuple"}:
            raise ValueError("invalid_step_tuple")
        return tuple(decode(v) for v in value["$tuple"])
    if "$array" in value:
        import numpy as np

        if set(value) != {"$array", "dtype", "shape"}:
            raise ValueError("invalid_step_array")
        dtype = np.dtype(value["dtype"])
        if dtype.kind not in "biuf":
            raise ValueError("invalid_step_array_dtype")
        result = np.array(value["$array"], dtype=dtype)
        if (
            list(result.shape) != value["shape"]
            or result.size > 1_000_000
            or not np.isfinite(result).all()
        ):
            raise ValueError("invalid_step_array")
        return result
    if any(k.startswith("$") for k in value):
        raise ValueError("invalid_step_payload_tag")
    return {k: decode(v) for k, v in value.items()}
