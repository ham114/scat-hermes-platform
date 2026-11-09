"""Finite workflow proposals. No identities, interpreter paths or confirmation fields."""

from __future__ import annotations

import math
import re
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

PRIMARY_SKILLS = {
    "forecast": "scat-model-comparison",
    "comparison": "scat-model-comparison",
    "experiment": "scat-experiment",
    "materialization": "scat-data-materialization",
    "report": "scat-report",
}


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class TrialConfiguration(Contract):
    model: Literal["constant-velocity-heading", "kalman-cv", "scat-gbar-v23"]
    version: Literal["1", "23"] = "1"
    window_s: float | None = Field(default=None, ge=10, le=1800)
    process_noise_mps2: float = Field(default=0.5, gt=0, le=20)
    measurement_noise_m: float = Field(default=30, gt=0, le=1000)

    @model_validator(mode="after")
    def supported_parameters(self):
        if self.model == "scat-gbar-v23":
            if (
                self.version != "23"
                or self.window_s is not None
                or (self.process_noise_mps2 != 0.5 or self.measurement_noise_m != 30)
            ):
                raise ValueError("GBAR requires version 23 and no runtime parameter overrides")
        elif self.version != "1":
            raise ValueError("CPU predictors require version 1")
        if self.model == "kalman-cv" and self.window_s is not None:
            raise ValueError("kalman-cv does not accept an observation-window parameter")
        if self.model == "constant-velocity-heading" and (
            self.process_noise_mps2 != 0.5 or self.measurement_noise_m != 30
        ):
            raise ValueError("Noise parameters apply to kalman-cv only")
        return self


class WorkflowProposal(Contract):
    kind: Literal["forecast", "comparison", "experiment", "materialization", "report"]
    batch: str = Field(default="SYNTH-S5", min_length=1, max_length=100)
    external_ids: tuple[str, ...] = Field(default=(), max_length=100)
    selection: Literal["explicit", "random"] = "explicit"
    candidate_limit: int = Field(default=100, ge=1, le=100)
    max_flights: int = Field(default=20, ge=1, le=20)
    min_flights: int = Field(default=1, ge=1, le=20)
    sampling_seed: int = Field(default=42, ge=0, le=2147483647)
    models: tuple[TrialConfiguration, ...] = Field(default=(), max_length=8)
    repeats: int = Field(default=1, ge=1, le=4)
    horizons_s: tuple[float, ...] = Field(default=(10, 30, 60), min_length=1, max_length=6)
    stride_s: float = Field(default=60, ge=10, le=600)
    min_observed_s: float = Field(default=60, ge=10, le=1800)
    max_cutoffs_per_flight: int = Field(default=4, ge=1, le=12)
    max_track_gap_s: float = Field(default=120, ge=1, le=600)
    observation_last_n: int | None = Field(default=None, strict=True, ge=3, le=2000)
    forecast_origin: Literal["last_observation", "historical_cutoffs", "as_of"] | None = None
    as_of: AwareDatetime | None = None
    materializations: tuple[
        Literal[
            "batch_statistics",
            "flight_features",
            "phase_segments",
            "prediction_metrics",
            "prediction_metric_summaries",
        ],
        ...,
    ] = ()
    missing_phase: Literal["report_missing", "require", "build"] = "report_missing"
    failure_policy: Literal["keep_partial", "stop"] = "keep_partial"
    worst_cases: int = Field(default=3, ge=1, le=20)
    source_task_ids: tuple[str, ...] = Field(default=(), max_length=8)
    title: str = Field(default="航空数据分析报告", min_length=1, max_length=120)

    @model_validator(mode="before")
    @classmethod
    def default_origin(cls, values):
        if isinstance(values, dict):
            values = dict(values)
            if "min_observed_s" not in values and any(
                (m.get("model") if isinstance(m, dict) else getattr(m, "model", None))
                == "scat-gbar-v23"
                for m in (
                    values.get("models") if isinstance(values.get("models"), (list, tuple)) else ()
                )
            ):
                values["min_observed_s"] = 350
        if (
            isinstance(values, dict)
            and values.get("forecast_origin") is None
            and values.get("kind") in {"forecast", "comparison", "experiment"}
        ):
            return {
                **values,
                "forecast_origin": "last_observation"
                if values["kind"] == "forecast"
                else "historical_cutoffs",
            }
        return values

    @field_validator("external_ids", "source_task_ids")
    @classmethod
    def unique_ids(cls, values):
        if len(values) != len(set(values)):
            raise ValueError("Identifiers must be unique")
        if any(not 1 <= len(v) <= 100 or any(c in v for c in "/\\\x00") for v in values):
            raise ValueError("Invalid identifier")
        return values

    @field_validator("source_task_ids")
    @classmethod
    def valid_task_ids(cls, values):
        if any(not re.fullmatch("[0-9a-f]{32}", v) for v in values):
            raise ValueError("Invalid source task identifier")
        return values

    @field_validator("horizons_s")
    @classmethod
    def finite_horizons(cls, values):
        if any(not math.isfinite(v) or not 0 < v <= 1800 for v in values):
            raise ValueError("Invalid prediction horizon")
        if tuple(sorted(set(values))) != values:
            raise ValueError("Horizons must be unique and increasing")
        return values

    @model_validator(mode="after")
    def bounded_plan(self):
        predictive = self.kind in {"forecast", "comparison", "experiment"}
        if not predictive and (
            self.observation_last_n is not None or self.forecast_origin is not None or self.as_of
        ):
            raise ValueError("Observation windows apply to prediction workflows only")
        if predictive:
            if self.kind == "forecast":
                if self.forecast_origin != "last_observation" or self.as_of is not None:
                    raise ValueError("Future forecast must originate at the final observed point")
                if self.observation_last_n is None:
                    raise ValueError("Future forecast requires an exact observation_last_n")
                if self.materializations:
                    raise ValueError(
                        "Future forecast does not construct historical materializations"
                    )
            elif self.forecast_origin == "last_observation":
                raise ValueError("Use kind=forecast for prediction from the last observation")
            if (self.forecast_origin == "as_of") != (self.as_of is not None):
                raise ValueError(
                    "Historical as_of requires forecast_origin=as_of and a UTC timestamp"
                )
            if self.observation_last_n is not None and any(
                m.window_s is not None for m in self.models
            ):
                raise ValueError("Exact point windows cannot also use a model window_s truncation")
        if self.min_flights > self.max_flights:
            raise ValueError("Minimum sample exceeds maximum")
        if len(set(self.materializations)) != len(self.materializations):
            raise ValueError("Duplicate materialization")
        if self.kind == "report":
            if (
                not self.source_task_ids
                or self.external_ids
                or self.selection != "explicit"
                or self.models
                or self.materializations
            ):
                raise ValueError("Report reuses scoped source tasks only")
        else:
            if (
                self.source_task_ids
                or (self.selection == "explicit" and not self.external_ids)
                or (self.selection == "random" and self.external_ids)
            ):
                raise ValueError("An explicit bounded candidate list is required")
            if self.kind == "materialization":
                if not self.materializations or self.models:
                    raise ValueError("Materialization requires derived kinds and no model trials")
            elif not self.models:
                raise ValueError("Comparison/experiment requires registered model configurations")
        if self.kind != "experiment" and self.repeats != 1:
            raise ValueError("Explicit repetitions belong to experiment workflows")
        if self.missing_phase == "build" and "phase_segments" not in self.materializations:
            raise ValueError("Phase construction must be explicitly included in approval")
        if "prediction_metric_summaries" in self.materializations and not {
            "prediction_metrics",
            "phase_segments",
            "flight_features",
        } <= set(self.materializations):
            raise ValueError(
                "Summary construction requires explicitly approved metrics, phases and features"
            )
        if len(self.models) * self.repeats > 32:
            raise ValueError("Trial ceiling exceeded")
        if any(m.model == "scat-gbar-v23" for m in self.models):
            if self.observation_last_n is not None and self.observation_last_n > 500:
                raise ValueError("GBAR accepts at most 500 exact raw observation points")
            if self.kind == "forecast" and any(h % 10 for h in self.horizons_s):
                raise ValueError("GBAR future forecast horizons must be on its 10-second grid")
            if self.min_observed_s < 350 or max(self.horizons_s) > 360:
                raise ValueError("GBAR requires min_observed_s >= 350 and horizons <= 360 seconds")
            if self.max_cutoffs_per_flight > 4:
                raise ValueError("GBAR admits at most four frozen cutoffs per flight")
        identities = [m.model_dump_json() for m in self.models]
        if len(identities) != len(set(identities)):
            raise ValueError("Use explicit repeats instead of duplicate configurations")
        return self


def trial_manifest(proposal: WorkflowProposal):
    from application.task_store import digest

    return [
        {
            "trial_id": "trial-" + digest({"model": model.model_dump(), "repeat": repeat})[:24],
            "configuration": model.model_dump(mode="json"),
            "repeat": repeat,
        }
        for model in proposal.models
        for repeat in range(proposal.repeats)
    ]
