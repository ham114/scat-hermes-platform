"""Framework-neutral resident GBAR execution, with one serialized model instance."""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from gbar_core.decoding import decode_gbar_v23_prediction
from gbar_core.model import load_model, predict_encoded


class ResidentGbarModel:
    def __init__(self, *, source_dir: Path, config: Path, checkpoint: Path, device: str):
        self.arguments = SimpleNamespace(
            source_dir=source_dir, config=config, checkpoint=checkpoint, device=device
        )
        self._lock = threading.RLock()
        self._loaded = None
        self._closed = False
        self.load_count = 0

    def load(self):
        with self._lock:
            if self._closed:
                raise RuntimeError("model is closed")
            if self._loaded is None:
                self._loaded = load_model(self.arguments)
                self.load_count += 1

    def predict(self, window):
        with self._lock:
            self.load()
            torch, model, config, device = self._loaded
            result = predict_encoded(
                torch,
                model,
                config,
                device,
                {
                    "encoder_input": window.absolute_binary[np.newaxis, ...].tolist(),
                    "decoder_input": window.delta_normalized[np.newaxis, ...].tolist(),
                },
            )
            normalized = np.asarray(result["prediction"], dtype=np.float32)
            physical = decode_gbar_v23_prediction(normalized[0], window.absolute_binary[-1])
            return physical, result["class_probabilities"]

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._loaded is not None:
                torch, _, _, device = self._loaded
                self._loaded = None
                if str(device) == "cuda":
                    torch.cuda.empty_cache()
