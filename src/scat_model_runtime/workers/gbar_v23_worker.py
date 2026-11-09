"""Persistent Python 3.11 worker for the external FlightGPT GBAR v23 model."""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

# Direct script execution does not inherit the parent Python module search path.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from gbar_core.model import load_model as _load_model
from gbar_core.model import predict_encoded as _predict


def _write(message: dict) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cuda")
    arguments = parser.parse_args()

    try:
        torch, model, config, device = _load_model(arguments)
        _write(
            {
                "type": "ready",
                "model": "scat-gbar-v23",
                "device": str(device),
                "torch_version": torch.__version__,
                "input_shape": [1, int(config.inp_seq_len), 75],
                "decoder_shape": [
                    1,
                    int(config.inp_seq_len) - 1,
                    len(config.features),
                ],
                "output_shape": [1, int(config.horizon), len(config.features)],
            }
        )
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        _write({"type": "startup_error", "error": type(exc).__name__})
        raise SystemExit(1) from exc

    for line in sys.stdin:
        message = {}
        try:
            message = json.loads(line)
            if not isinstance(message, dict):
                raise ValueError("worker request must be a JSON object")
            command = message.get("command")
            if command == "shutdown":
                _write({"type": "shutdown", "request_id": message.get("request_id")})
                return
            if command != "predict":
                raise ValueError("unsupported command")
            _write(_predict(torch, model, config, device, message))
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            _write(
                {
                    "type": "error",
                    "request_id": message.get("request_id"),
                    "error": type(exc).__name__,
                }
            )


if __name__ == "__main__":
    main()
