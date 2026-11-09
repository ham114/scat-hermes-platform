"""Validated deployment policy and bounded, explicit readonly budget reductions."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from hermes_runtime.skills import yaml_load

Positive = Annotated[int, Field(strict=True, gt=0, le=10_000_000)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False)


class Budget(StrictModel):
    max_turns: Positive = 16
    max_llm_tokens: Positive = 1000000
    max_tool_calls: Positive = 32
    wall_seconds: Positive = 240
    max_cost: Annotated[float, Field(gt=0)] | None = None
    currency: Literal["CNY", "USD"] = "CNY"

    @model_validator(mode="after")
    def supported_currency_limit(self):
        if self.max_cost is not None:
            raise ValueError("strict_cost_limit_requires_verified_provider_metering")
        return self


class TracePolicy(StrictModel):
    max_hot_records: Positive = 10000
    hot_retention_days: Positive = 30
    archive_retention_days: Positive = 180
    archive_high_watermark: Annotated[float, Field(gt=0, lt=1)] = 0.8

    @model_validator(mode="after")
    def ordered_retention(self):
        if self.archive_retention_days < self.hot_retention_days:
            raise ValueError("archive_retention_before_hot_retention")
        return self


class SessionPolicy(StrictModel):
    max_active: Positive = 1000
    idle_ttl_seconds: Positive = 3600


class CachePolicy(StrictModel):
    max_entries: Positive = 128
    ttl_seconds: Positive = 3600


class CompositionPolicy(StrictModel):
    max_depth: Annotated[int, Field(strict=True, gt=0, le=16)] = 6


class ResourcesPolicy(StrictModel):
    max_concurrent_jobs: Positive = 1


class Budgets(StrictModel):
    readonly: Budget = Field(default_factory=Budget)
    background: Budget = Field(
        default_factory=lambda: Budget(
            max_turns=24, max_llm_tokens=1000000, max_tool_calls=80, wall_seconds=1800
        )
    )


class SkillPolicy(StrictModel):
    schema_version: Literal[1] = 1
    trace: TracePolicy = Field(default_factory=TracePolicy)
    sessions: SessionPolicy = Field(default_factory=SessionPolicy)
    cache: CachePolicy = Field(default_factory=CachePolicy)
    composition: CompositionPolicy = Field(default_factory=CompositionPolicy)
    budgets: Budgets = Field(default_factory=Budgets)
    resources: ResourcesPolicy = Field(default_factory=ResourcesPolicy)
    readonly_ceiling: Budget = Field(default_factory=Budget)
    background_ceiling: Budget = Field(
        default_factory=lambda: Budget(
            max_turns=48, max_llm_tokens=2000000, max_tool_calls=160, wall_seconds=3600
        )
    )
    category_overrides: dict[str, dict[str, Positive]] = Field(default_factory=dict)
    user_preferences: dict[str, dict[str, Positive]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def bounded_defaults(self):
        lower_budget(self.readonly_ceiling, self.budgets.readonly.model_dump())
        lower_budget(self.background_ceiling, self.budgets.background.model_dump())
        for overrides in [self.category_overrides, self.user_preferences]:
            if len(overrides) > 128:
                raise ValueError("too_many_policy_overrides")
            for name, values in overrides.items():
                if not name or len(name) > 128:
                    raise ValueError("invalid_policy_subject")
                lower_budget(
                    self.budgets.background if name == "background" else self.budgets.readonly,
                    values,
                )
        return self

    @property
    def sha256(self):
        return hashlib.sha256(
            json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def effective(self, *, category="readonly", user="scat-researcher", request=None):
        budget = self.budgets.background if category == "background" else self.budgets.readonly
        for overrides in [
            self.category_overrides.get(category, {}),
            self.user_preferences.get(user, {}),
            request or {},
        ]:
            budget = lower_budget(budget, overrides)
        return budget

    def describe(self, **kwargs):
        return {
            "policy_sha256": self.sha256,
            "policy": self.model_dump(),
            "effective_readonly": self.effective(**kwargs).model_dump(),
            "enforced_in_s2": [
                "trace.max_hot_records (loads and operations combined)",
                "sessions.max_active",
                "sessions.idle_ttl_seconds (finished only)",
                "composition.max_depth",
                "budgets.readonly",
            ],
            "enforced_in_s4": [
                "budgets.background (shared cumulative job ledger)",
                "resources.max_concurrent_jobs",
                "background_ceiling and explicit budget revisions",
            ],
            "enforced_in_s6": [
                "terminal-run atomic cold archive, integrity and scoped reads",
                "active/unknown/unresolved dispatch and explicit holds protected",
                "trace hot/total retention and policy preview",
                "scope/release catalogue LRU/TTL cache; live snapshot verification on every hit",
            ],
            "reserved_for_later_stages": [],
            "token_accounting": "conservative UTF-8 request byte reservation plus output cap; provider usage recorded separately",
        }


def lower_budget(base: Budget, values: dict) -> Budget:
    if not isinstance(values, dict):
        raise TypeError("invalid_budget_override")
    if set(values) - set(Budget.model_fields):
        raise ValueError("unknown_budget_field")
    result = Budget.model_validate({**base.model_dump(), **values})
    for name in ["max_turns", "max_llm_tokens", "max_tool_calls", "wall_seconds"]:
        if getattr(result, name) > getattr(base, name):
            raise ValueError("budget_override_exceeds_authorized_ceiling:" + name)
    if result.currency != base.currency:
        raise ValueError("budget_currency_mismatch")
    return result


def load_policy(root: Path, path: Path | None = None) -> SkillPolicy:
    root = root.resolve()
    path = path or root / "config/skills-policy.yaml"
    if not path.is_absolute():
        path = root / path
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError("policy_path_outside_workspace")
    if path.stat().st_size > 65536:
        raise ValueError("policy_file_too_large")
    return SkillPolicy.model_validate(yaml_load(path.read_text(encoding="utf-8")))
