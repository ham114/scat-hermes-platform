from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from scat.preprocessing.gbar_v35 import (
    SCAT_GBAR_ENCODING,
    SCAT_GBAR_FEATURES,
    ScatGbarV35DataProcessor,
)
from scat_model_runtime.gbar_v23_adapter import decode_gbar_v23_prediction


def _points(count: int = 80) -> list[dict[str, object]]:
    started = datetime(2016, 10, 20, 19, 30, tzinfo=UTC)
    return [
        {
            "source_index": index,
            "timestamp": (started + timedelta(seconds=5 * index)).isoformat(),
            "lat": 60.0 + index * 0.001,
            "lon": 20.0 + index * 0.001,
            "measured_flight_level": 300.0,
            "vx": 100.0,
            "vy": 50.0,
            "rocd": 600.0,
        }
        for index in range(count)
    ]


def test_decoder_reconstructs_zero_delta_from_last_observation():
    window = ScatGbarV35DataProcessor().prepare(
        _points(), batch="batch", external_id="flight"
    )
    normalized_zero_deltas = np.asarray(
        [
            -SCAT_GBAR_ENCODING[feature].delta_minimum
            / (
                SCAT_GBAR_ENCODING[feature].delta_maximum
                - SCAT_GBAR_ENCODING[feature].delta_minimum
            )
            for feature in SCAT_GBAR_FEATURES
        ],
        dtype=np.float32,
    )
    normalized = np.tile(normalized_zero_deltas, (36, 1))

    decoded = decode_gbar_v23_prediction(normalized, window.absolute_binary[-1])

    assert decoded.shape == (36, 6)
    assert np.all(np.isfinite(decoded))
    assert np.all(decoded == decoded[0])
    for column, feature in enumerate(SCAT_GBAR_FEATURES):
        encoding = SCAT_GBAR_ENCODING[feature]
        assert encoding.minimum <= decoded[0, column] <= encoding.maximum


@pytest.mark.parametrize(
    ("prediction", "binary", "message"),
    [
        (np.zeros((36, 5)), np.zeros(75), "prediction"),
        (np.zeros((36, 6)), np.zeros(74), "binary"),
        (np.full((36, 6), np.nan), np.zeros(75), "finite"),
        (np.full((36, 6), 1.1), np.zeros(75), "within"),
        (np.zeros((36, 6)), np.full(75, 0.5), "only 0 or 1"),
    ],
)
def test_decoder_rejects_invalid_inputs(prediction, binary, message):
    with pytest.raises(ValueError, match=message):
        decode_gbar_v23_prediction(prediction, binary)
