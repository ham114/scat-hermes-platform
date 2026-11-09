"""GBAR v23 adapter backed by a persistent, environment-isolated worker."""

from __future__ import annotations

import atexit
import json
import select
import subprocess
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import numpy as np

from core.file_integrity import verify_sha256
from core.resilience import CircuitBreaker
from gbar_core.decoding import decode_gbar_v23_prediction
from scat.preprocessing.gbar_v35 import (
    SCAT_GBAR_FEATURES,
    ScatGbarPreparedWindow,
)
from scat_model_runtime.adapters import ModelInferenceOutput
from scat_model_runtime.contracts import ModelParameterValue
from scat_model_runtime.pipelines import PreparedModelInput

GBAR_V23_INPUT_SCHEMA = "scat-gbar-v35-input/v2"
GBAR_V23_OUTPUT_SCHEMA = "scat-gbar-v23-prediction/v1"


@dataclass(frozen=True, slots=True)
class GbarV23WorkerConfig:
    python_executable: Path
    worker_script: Path
    source_dir: Path
    model_config: Path
    checkpoint: Path
    device: str = "cuda"
    startup_timeout_seconds: float = 120.0
    prediction_timeout_seconds: float = 120.0
    circuit_failure_threshold: int = 3
    circuit_recovery_seconds: float = 30.0
    source_uri: str | None = None
    worker_script_sha256: str | None = None
    model_config_sha256: str | None = None
    checkpoint_sha256: str | None = None

    def validated(self) -> GbarV23WorkerConfig:
        if self.device not in {"cpu", "cuda"}:
            raise ValueError("GBAR worker device must be cpu or cuda")
        if self.startup_timeout_seconds <= 0 or self.prediction_timeout_seconds <= 0:
            raise ValueError("GBAR worker timeouts must be positive")
        if self.circuit_failure_threshold < 1 or self.circuit_recovery_seconds <= 0:
            raise ValueError("GBAR worker circuit settings must be positive")
        resolved = GbarV23WorkerConfig(
            python_executable=self.python_executable.expanduser().resolve(strict=True),
            worker_script=self.worker_script.expanduser().resolve(strict=True),
            source_dir=self.source_dir.expanduser().resolve(strict=True),
            model_config=self.model_config.expanduser().resolve(strict=True),
            checkpoint=self.checkpoint.expanduser().resolve(strict=True),
            device=self.device,
            startup_timeout_seconds=self.startup_timeout_seconds,
            prediction_timeout_seconds=self.prediction_timeout_seconds,
            circuit_failure_threshold=self.circuit_failure_threshold,
            circuit_recovery_seconds=self.circuit_recovery_seconds,
            source_uri=self.source_uri,
            worker_script_sha256=self.worker_script_sha256,
            model_config_sha256=self.model_config_sha256,
            checkpoint_sha256=self.checkpoint_sha256,
        )
        for path, expected, label in (
            (resolved.worker_script, resolved.worker_script_sha256, "GBAR worker script"),
            (resolved.model_config, resolved.model_config_sha256, "GBAR model config"),
            (resolved.checkpoint, resolved.checkpoint_sha256, "GBAR checkpoint"),
        ):
            if expected is not None:
                verify_sha256(path, expected, label=label)
        return resolved


class GbarV23SubprocessAdapter:
    """Keep one mamba worker/model resident and expose the ModelAdapter protocol."""

    def __init__(self, config: GbarV23WorkerConfig) -> None:
        self.config = config.validated()
        self._process: subprocess.Popen[str] | None = None
        # load(), predict() and shutdown() can call one another while protecting
        # the same worker lifecycle.  An RLock keeps that sequence atomic without
        # deadlocking on the first prediction.
        self._lock = threading.RLock()
        self._ready: dict[str, Any] | None = None
        self._circuit = CircuitBreaker(
            failure_threshold=self.config.circuit_failure_threshold,
            recovery_timeout_seconds=self.config.circuit_recovery_seconds,
        )
        atexit.register(self.shutdown)

    def _read_message(self, timeout_seconds: float) -> dict[str, Any]:
        process = self._process
        if process is None or process.stdout is None:
            raise RuntimeError("GBAR worker is not running")
        readable, _, _ = select.select([process.stdout], [], [], timeout_seconds)
        if not readable:
            raise TimeoutError("GBAR worker response timed out")
        line = process.stdout.readline()
        if not line:
            return_code = process.poll()
            raise RuntimeError(f"GBAR worker exited unexpectedly ({return_code})")
        message = json.loads(line)
        if not isinstance(message, dict):
            raise RuntimeError("GBAR worker returned an invalid message")
        return message

    def _send(self, message: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise RuntimeError("GBAR worker is not running")
        process.stdin.write(json.dumps(message, ensure_ascii=True, separators=(",", ":")) + "\n")
        process.stdin.flush()

    def load(self) -> None:
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                return
            self.shutdown()
            command = [
                str(self.config.python_executable),
                "-u",
                str(self.config.worker_script),
                "--source-dir",
                str(self.config.source_dir),
                "--config",
                str(self.config.model_config),
                "--checkpoint",
                str(self.config.checkpoint),
                "--device",
                self.config.device,
            ]
            try:
                self._process = subprocess.Popen(
                    command,
                    cwd=self.config.source_dir,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=None,
                    text=True,
                    encoding="utf-8",
                    bufsize=1,
                )
                ready = self._read_message(self.config.startup_timeout_seconds)
                if ready.get("type") != "ready":
                    raise RuntimeError("GBAR worker failed to initialize")
                self._ready = ready
            except BaseException:
                self.shutdown()
                raise

    def predict(
        self,
        *,
        model_input: PreparedModelInput,
        parameters: Mapping[str, ModelParameterValue],
    ) -> ModelInferenceOutput:
        if parameters:
            raise ValueError("GBAR v23 does not accept runtime parameter overrides")
        if model_input.input_schema != GBAR_V23_INPUT_SCHEMA:
            raise ValueError("GBAR v23 input schema mismatch")
        if model_input.feature_names != SCAT_GBAR_FEATURES:
            raise ValueError("GBAR v23 feature order mismatch")
        window = model_input.payload
        if not isinstance(window, ScatGbarPreparedWindow):
            raise TypeError("GBAR v23 requires a ScatGbarPreparedWindow payload")

        self._circuit.before_call()
        try:
            with self._lock:
                self.load()
                request_id = uuid4().hex
                self._send(
                    {
                        "command": "predict",
                        "request_id": request_id,
                        "encoder_input": window.absolute_binary[np.newaxis, ...].tolist(),
                        "decoder_input": window.delta_normalized[np.newaxis, ...].tolist(),
                    }
                )
                response = self._read_message(self.config.prediction_timeout_seconds)
            if response.get("type") != "prediction" or response.get("request_id") != request_id:
                error_type = response.get("error")
                suffix = f" ({error_type})" if isinstance(error_type, str) else ""
                raise RuntimeError(f"GBAR worker prediction failed{suffix}")
            normalized = np.asarray(response.get("prediction"), dtype=np.float32)
            if normalized.shape != (1, 36, len(SCAT_GBAR_FEATURES)):
                raise RuntimeError(
                    f"GBAR worker returned shape {normalized.shape}, expected (1, 36, 6)"
                )
            physical = decode_gbar_v23_prediction(normalized[0], window.absolute_binary[-1])
        except Exception:
            self.shutdown()
            self._circuit.record_failure()
            raise
        self._circuit.record_success()
        period_seconds = window.quality.model_period_seconds
        predicted_points = []
        for index, values in enumerate(physical, start=1):
            timestamp = window.timestamps[-1] + timedelta(seconds=period_seconds * index)
            predicted_points.append(
                {
                    "timestamp": timestamp.isoformat(),
                    "time_offset_seconds": round(period_seconds * index, 3),
                    **{
                        feature: float(value)
                        for feature, value in zip(SCAT_GBAR_FEATURES, values, strict=True)
                    },
                }
            )
        payload = {
            "batch": window.batch,
            "external_id": window.external_id,
            "observed_start_time": window.timestamps[0].isoformat(),
            "observed_end_time": window.timestamps[-1].isoformat(),
            "model_period_seconds": period_seconds,
            "feature_names": list(SCAT_GBAR_FEATURES),
            "predicted_points": predicted_points,
            "class_probabilities": response.get("class_probabilities"),
            "quality": window.quality.as_dict(),
            "model_provenance": {
                key: value
                for key, value in {
                    "source_uri": self.config.source_uri,
                    "worker_script_sha256": self.config.worker_script_sha256,
                    "model_config_sha256": self.config.model_config_sha256,
                    "checkpoint_sha256": self.config.checkpoint_sha256,
                }.items()
                if value is not None
            },
        }
        return ModelInferenceOutput(
            output_schema=GBAR_V23_OUTPUT_SCHEMA,
            point_count=len(predicted_points),
            payload=payload,
        )

    def close(self) -> None:
        """Keep the worker resident between model-agent calls."""

    def interrupt(self) -> None:
        """Interrupt this adapter's worker without waiting on its prediction lock."""
        process = self._process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass

    def shutdown(self) -> None:
        with self._lock:
            process = self._process
            self._process = None
            self._ready = None
            if process is None:
                return
            try:
                if process.poll() is None and process.stdin is not None:
                    request_id = uuid4().hex
                    process.stdin.write(
                        json.dumps({"command": "shutdown", "request_id": request_id}) + "\n"
                    )
                    process.stdin.flush()
                if process.poll() is None:
                    process.wait(timeout=10)
            except (BrokenPipeError, subprocess.TimeoutExpired):
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
            finally:
                if process.stdin is not None:
                    process.stdin.close()
                if process.stdout is not None:
                    process.stdout.close()


__all__ = [
    "GBAR_V23_INPUT_SCHEMA",
    "GBAR_V23_OUTPUT_SCHEMA",
    "GbarV23SubprocessAdapter",
    "GbarV23WorkerConfig",
    "decode_gbar_v23_prediction",
]
