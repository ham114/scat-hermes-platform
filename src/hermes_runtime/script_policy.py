"""Finite trusted deployment limits; no interpreter supplied by a model."""

import hashlib
import json

from pydantic import BaseModel, ConfigDict, Field

from hermes_runtime.skills import checked_path


class ScriptPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    version: int = Field(default=1, ge=1, le=1)
    enabled: bool = True
    max_runs_per_run: int = Field(default=4, ge=1, le=16)
    max_steps: int = Field(default=2000, ge=1, le=10000)
    max_iterations: int = Field(default=64, ge=1, le=256)
    max_tool_calls: int = Field(default=12, ge=1, le=32)
    wall_seconds: int = Field(default=120, ge=1, le=300)
    cpu_seconds: int = Field(default=2, ge=1, le=10)
    memory_mb: int = Field(default=256, ge=64, le=512)
    max_input_bytes: int = Field(default=32768, ge=256, le=65536)
    max_output_bytes: int = Field(default=65536, ge=256, le=131072)
    max_frame_bytes: int = Field(default=262144, ge=1024, le=524288)
    max_records: int = Field(default=10000, ge=1, le=100000)

    @property
    def sha256(self):
        return hashlib.sha256(json.dumps(self.model_dump(), sort_keys=True).encode()).hexdigest()


def load_script_policy(root):
    return ScriptPolicy.model_validate(
        json.loads(checked_path(root, "config/skill-scripts-policy.json").read_text())
    )
