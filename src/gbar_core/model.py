"""Shared model loader and encoded inference; imports PyTorch only on load."""

from __future__ import annotations
import contextlib
import random
import sys

INFERENCE_SEED = 0


def load_model(arguments):
    source_dir = arguments.source_dir.expanduser().resolve(strict=True)
    config_path = arguments.config.expanduser().resolve(strict=True)
    checkpoint_path = arguments.checkpoint.expanduser().resolve(strict=True)
    sys.path.insert(0, str(source_dir))

    # These imports intentionally exist only in the isolated mamba environment.
    import torch  # type: ignore[import-not-found]
    import numpy as np

    # Both subprocess and Bento load this function in their isolated worker.
    # Seed construction as well as inference; strict checkpoint loading and
    # eval/inference_mode remain the numerical contract used by the baseline.
    random.seed(INFERENCE_SEED)
    np.random.seed(INFERENCE_SEED)
    torch.manual_seed(INFERENCE_SEED)

    with contextlib.redirect_stdout(sys.stderr):
        from models.GeoHashBARTNet_v23 import GeohashBART  # type: ignore[import-not-found]
        from utils import load_config_from_json  # type: ignore[import-not-found]

        config = load_config_from_json(str(config_path))
        expected_features = ["lat", "lon", "measured_flight_level", "vx", "vy", "rocd"]
        if list(config.features) != expected_features:
            raise ValueError("GBAR config feature contract mismatch")
        if int(config.inp_seq_len) != 36 or int(config.horizon) != 36:
            raise ValueError("GBAR config sequence contract mismatch")
        config.is_training = False
        config.model_path = str(checkpoint_path)
        config.inference_strict_load = True
        config.shuffle_confidence_during_eval = False
        model = GeohashBART(config)
        if arguments.device == "cuda" and not torch.cuda.is_available():
            raise RuntimeError("CUDA is unavailable")
        device = torch.device(arguments.device)
        model = model.to(device).eval()
    return torch, model, config, device


def predict_encoded(torch, model, config, device, message):
    encoder_input = torch.as_tensor(
        message.get("encoder_input"), dtype=torch.float32, device=device
    )
    decoder_input = torch.as_tensor(
        message.get("decoder_input"), dtype=torch.float32, device=device
    )
    expected_encoder = (1, int(config.inp_seq_len), 75)
    expected_decoder = (1, int(config.inp_seq_len) - 1, len(config.features))
    if tuple(encoder_input.shape) != expected_encoder:
        raise ValueError(
            "encoder_input shape mismatch: "
            f"expected {expected_encoder}, got {tuple(encoder_input.shape)}"
        )
    if tuple(decoder_input.shape) != expected_decoder:
        raise ValueError(
            "decoder_input shape mismatch: "
            f"expected {expected_decoder}, got {tuple(decoder_input.shape)}"
        )
    with torch.inference_mode(), contextlib.redirect_stdout(sys.stderr):
        prediction = model(encoder_input, decoder_input)
    if tuple(prediction.shape) != (1, int(config.horizon), len(config.features)):
        raise RuntimeError(f"unexpected model output shape: {tuple(prediction.shape)}")
    if not bool(torch.isfinite(prediction).all().item()):
        raise RuntimeError("model output contains non-finite values")
    class_probabilities = getattr(model, "class_probs", None)
    if class_probabilities is not None and not bool(
        torch.isfinite(class_probabilities).all().item()
    ):
        raise RuntimeError("model class probabilities contain non-finite values")
    return {
        "type": "prediction",
        "request_id": message.get("request_id"),
        "prediction": prediction.detach().cpu().tolist(),
        "class_probabilities": (
            class_probabilities.detach().cpu().tolist() if class_probabilities is not None else None
        ),
    }
