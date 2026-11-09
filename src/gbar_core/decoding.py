"""Shared v35 inverse normalization and cumulative decoding (NumPy only)."""

from __future__ import annotations
import numpy as np
from gbar_core.preprocessing import SCAT_GBAR_FEATURES, SCAT_GBAR_ENCODING


def decode_gbar_v23_prediction(
    normalized_prediction: np.ndarray,
    last_absolute_binary: np.ndarray,
) -> np.ndarray:
    """Apply the v35 inverse normalization and cumulative bin reconstruction."""
    prediction = np.asarray(normalized_prediction, dtype=np.float64)
    last_binary = np.asarray(last_absolute_binary, dtype=np.float64)
    if prediction.ndim != 2 or prediction.shape[1] != len(SCAT_GBAR_FEATURES):
        raise ValueError("normalized prediction must have shape [horizon, 6]")
    expected_bits = sum(SCAT_GBAR_ENCODING[name].bits for name in SCAT_GBAR_FEATURES)
    if last_binary.shape != (expected_bits,):
        raise ValueError(f"last absolute binary input must have shape [{expected_bits}]")
    if not np.all(np.isfinite(prediction)) or not np.all(np.isfinite(last_binary)):
        raise ValueError("GBAR inverse decoding inputs must be finite")
    if np.any((prediction < 0.0) | (prediction > 1.0)):
        raise ValueError("normalized prediction values must be within [0, 1]")
    if np.any((last_binary != 0.0) & (last_binary != 1.0)):
        raise ValueError("last absolute binary input must contain only 0 or 1")

    real_prediction = np.empty_like(prediction, dtype=np.float64)
    offset = 0
    for column, feature in enumerate(SCAT_GBAR_FEATURES):
        encoding = SCAT_GBAR_ENCODING[feature]
        bits = last_binary[offset : offset + encoding.bits]
        weights = np.power(2, np.arange(encoding.bits - 1, -1, -1), dtype=np.int64)
        current_index = int(np.rint(bits @ weights))
        delta = (
            prediction[:, column] * (encoding.delta_maximum - encoding.delta_minimum)
            + encoding.delta_minimum
        )
        maximum_index = 2**encoding.bits - 1
        for horizon_index, step in enumerate(delta):
            current_index += int(np.rint(step))
            current_index = min(max(current_index, 0), maximum_index)
            real_prediction[horizon_index, column] = encoding.minimum + (
                (current_index + 0.5) / (2**encoding.bits) * (encoding.maximum - encoding.minimum)
            )
        offset += encoding.bits
    return real_prediction.astype(np.float32)
