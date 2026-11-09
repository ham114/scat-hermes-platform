"""Trusted, project-scoped policy for memory and automatic skill drafting."""

from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from hermes_runtime.skills import checked_path


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class MemoryPolicy(Strict):
    enabled: bool = True
    state: str = "var/autonomy/memory/state.sqlite"
    identity_key: str = "var/deploy/autonomy/secrets/identity.key"
    signing_key: str = "var/deploy/honcho-production/secrets/auth-signing.key"
    base_url: str = "http://127.0.0.1:18140"
    context_chars: int = Field(default=4000, ge=256, le=12000)
    max_pending: int = Field(default=1000, ge=1, le=10000)
    inactivity_days: int = Field(default=180, ge=1, le=3650)


class LearningPolicy(Strict):
    enabled: bool = True
    min_successes: int = Field(default=3, ge=1, le=20)
    min_distinct_inputs: int = Field(default=2, ge=1, le=20)
    cooldown_hours: int = Field(default=24, ge=0, le=8760)
    max_drafts_per_day: int = Field(default=3, ge=1, le=20)
    state: str = "var/autonomy/learning/state.sqlite"
    author_url: str = "http://127.0.0.1:18124"
    author_key: str = "var/deploy/h7b/secrets/author.key"
    author_state: str = "var/deploy/h7b/shared/skills/catalog.sqlite"
    registry: str = "var/deploy/h7b/shared/state/bindings.sqlite"
    results: str = "var/deploy/h7b/shared/state/results.sqlite"
    traces: str = "var/deploy/h7b/shared/homes/main/skill-loads.sqlite"
    tasks: str = "var/deploy/h7b/shared/state/tasks.sqlite"
    native_url: str = "http://127.0.0.1:18121"
    native_key: str = "var/deploy/h7b/secrets/hermes.key"
    owner: str = "scat-researcher"
    auto_trials: bool = True


class AutonomyPolicy(Strict):
    memory: MemoryPolicy = Field(default_factory=MemoryPolicy)
    learning: LearningPolicy = Field(default_factory=LearningPolicy)


def private_text(root: Path, value: str) -> str:
    path = checked_path(root, value)
    if os.name == "posix" and path.stat().st_mode & 0o077:
        raise ValueError("autonomy_private_file_requires_0600")
    key = path.read_text().strip()
    if len(key) < 32:
        raise ValueError("autonomy_private_key_required")
    return key


def load_autonomy(root: Path, filename: str | None = None) -> AutonomyPolicy | None:
    filename = filename or os.environ.get("SCAT_AUTONOMY_CONFIG")
    if not filename:
        return None
    policy = AutonomyPolicy.model_validate(json.loads(checked_path(root, filename).read_text()))
    for item in (policy.memory, policy.learning):
        checked_path(root, item.state)
    return policy
