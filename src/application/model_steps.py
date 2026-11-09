"""Five independently callable coarse model operations for trusted task controllers.

No business graph, LangChain tool, database credentials, or LLM authorization fields.
S3 admits a server-created synthetic CPU grant only. S4 supplies durable approved-job grants.
"""

from __future__ import annotations

import hashlib
import inspect
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from application.model_step_store import ModelStepStore, StepError, canonical, digest
from scat_model_runtime import ModelRunStatus, ModelTaskRequest
from scat_model_runtime.step_codec import decode, encode
from scat_model_runtime.steps import ModelStepKernel
from schema.analysis import AnalysisResult, MetricItem, MetricsBlock

OPERATIONS = {
    "scat_model_prepare_input": (None, "input", "derived_write"),
    "scat_model_run": ("input", "prediction", "compute"),
    "scat_model_evaluate": ("prediction", "evaluation", "compute"),
    "scat_model_save_result": ("evaluation", "result", "derived_write"),
    "scat_model_read_result": ("result", None, "query"),
}


def implementation_snapshot(kernel):
    """Bind component source versions as well as registry keys, across subprocesses."""
    entries = []
    for name, registry in sorted(vars(kernel).items()):
        if name == "models":
            continue
        for registration in registry.list():
            cls = type(registration.component)
            source = inspect.getsourcefile(cls)
            if source is None:
                raise StepError("component_source_not_versioned")
            entries.append(
                {
                    "registry": name,
                    "id": registration.reference.registry_id,
                    "class": cls.__module__ + "." + cls.__qualname__,
                    "source_sha256": hashlib.sha256(Path(source).read_bytes()).hexdigest(),
                }
            )
    return digest(entries)


def spec_digest(spec):
    data = spec.model_dump(mode="json")
    data["supported_datasets"] = sorted(data["supported_datasets"])
    return digest(data)


@dataclass(frozen=True)
class ModelStepGrant:
    """Trusted controller value, intentionally absent from every tool argument schema."""

    scope: str
    run_id: str
    request_json: str
    spec_sha256: str
    expires: float
    synthetic_cpu: bool
    components_sha256: str
    job_id: str | None = None


class ModelSteps:
    def __init__(self, kernel: ModelStepKernel, store: ModelStepStore, grant: ModelStepGrant):
        self.kernel, self.store, self.grant = kernel, store, grant

    def _authorize(self):
        g = self.grant
        if not g.synthetic_cpu and not g.job_id:
            raise StepError("approved_job_required_s4")
        if g.job_id:
            if self.store.fence is None:
                raise StepError("trusted_job_fence_required")
            with closing(self.store.connect()) as db:
                self.store.fence(db)
        if not g.scope or not g.run_id or g.expires <= time.time():
            raise StepError("step_grant_expired")
        request = ModelTaskRequest.model_validate_json(g.request_json)
        if not request.model_version or request.confirmed:
            raise StepError("invalid_step_grant")
        spec = self.kernel.models.match(request)
        if spec_digest(spec) != g.spec_sha256:
            raise StepError("model_registration_changed")
        if implementation_snapshot(self.kernel) != g.components_sha256:
            raise StepError("component_implementation_changed")
        # confirmed is assigned here, after trusted controller authorization, never from the LLM.
        return request.model_copy(update={"confirmed": True}), spec

    def _load(self, ref, kind, request, spec):
        body = self.store.read(self.grant.scope, self.grant.run_id, ref, kind=kind)
        if body.get("schema_version") != "model-step-artifact/v1":
            raise StepError("step_artifact_protocol_mismatch")
        if body.get("components_sha256") != self.grant.components_sha256:
            raise StepError("step_artifact_lineage_mismatch")
        if body.get("request_sha256") != digest(request.model_dump(mode="json")) or body.get(
            "spec_sha256"
        ) != spec_digest(spec):
            raise StepError("step_artifact_lineage_mismatch")
        state = decode(body["state"])
        if (
            state.get("request") != request
            or state.get("model_spec") != spec
            or state.get("run_id") != self.grant.run_id
        ):
            raise StepError("step_artifact_lineage_mismatch")
        return body, state

    @staticmethod
    def _update(state, patch):
        if patch.get("status") == ModelRunStatus.FAILED:
            raise StepError(patch["error_code"])
        state.update(patch)

    @staticmethod
    def _validate_prediction(state):
        """Retain units and temporal semantics inside the indivisible model operation."""
        if "regression_output" in state:
            src, out, spec = (
                state["regression_input"],
                state["regression_output"],
                state["model_spec"],
            )
            if (out.model_key, out.model_version) != (spec.model_key, spec.version):
                raise StepError("model_output_identity_mismatch")
            refs = {canonical(s.input_ref.model_dump(mode="json")): s for s in src.series}
            for series in out.series:
                original = refs.get(canonical(series.input_ref.model_dump(mode="json")))
                if original is None or series.forecast_origin != original.timestamps[-1]:
                    raise StepError("model_output_time_or_reference_mismatch")
            if any(
                name in src.feature_units and out.target_units[name] != src.feature_units[name]
                for name in out.target_names
            ):
                raise StepError("model_output_unit_mismatch")
        if "comparison_output" in state:
            src, out = state["comparison_input"], state["comparison_output"]
            if src.protocol_version != out.protocol_version:
                raise StepError("comparison_protocol_mismatch")

    @staticmethod
    def analysis(state):
        # These are pure deterministic renderers, not old Tool or graph entry points.
        from agents.analysis_results import (
            build_comparison_model_analysis,
            build_regression_model_analysis,
        )

        if "comparison_output" in state:
            return build_comparison_model_analysis(state["comparison_output"]).model_dump(
                mode="json"
            )
        if "regression_output" in state:
            return build_regression_model_analysis(
                state["regression_output"], state.get("regression_evaluation")
            ).model_dump(mode="json")
        evaluation = state.get("evaluation")
        items = [
            MetricItem(key=k, label=k, value=v)
            for k, v in (
                evaluation.metrics if evaluation else {"points": state["prediction"].point_count}
            ).items()
        ]
        return AnalysisResult(
            dataset=state["request"].primary_input_ref.dataset.value,
            operation="run_registered_model",
            title="Registered model result",
            blocks=[MetricsBlock(title="Metrics", items=items[:12])],
        ).model_dump(mode="json")

    async def invoke(self, operation, arguments):
        if operation not in OPERATIONS:
            raise StepError("unknown_model_operation")
        expected, output, _ = OPERATIONS[operation]
        if set(arguments) != ({"reference"} if expected else set()):
            raise StepError("invalid_model_step_arguments")
        request, spec = self._authorize()
        body, state = ({}, {"request": request, "model_spec": spec, "run_id": self.grant.run_id})
        if expected:
            body, state = self._load(arguments["reference"], expected, request, spec)
        if operation == "scat_model_read_result":
            analysis = AnalysisResult.model_validate(body["analysis_result"]).model_dump(
                mode="json"
            )
            return {
                "status": "ok",
                "synthetic": True,
                "reference": arguments["reference"],
                "analysis_result": analysis,
            }
        g = self.grant
        key, cached = self.store.reserve(g.scope, g.run_id, operation, arguments)
        if cached:
            self._load(cached.model_dump(), output, request, spec)
            return self._reply(cached, reused=True)
        try:
            if operation == "scat_model_prepare_input":
                self._update(state, self.kernel.validate_request(state))
                self._update(state, self.kernel.resolve_components(state))
                self._update(state, await self.kernel.resolve_input(state))
                self._update(state, self.kernel.prepare_features(state))
            elif operation == "scat_model_run":
                self._update(state, await self.kernel.execute_model(state))
                self._validate_prediction(state)
            elif operation == "scat_model_evaluate":
                self._update(state, self.kernel.evaluate_model(state))
            new_body = {
                "schema_version": "model-step-artifact/v1",
                "operation": operation,
                "parent": arguments.get("reference"),
                "request_sha256": digest(request.model_dump(mode="json")),
                "spec_sha256": spec_digest(spec),
                "components_sha256": g.components_sha256,
                "state": encode(state),
            }
            if operation == "scat_model_save_result":
                new_body["analysis_result"] = self.analysis(state)
            ref = self.store.save(g.scope, g.run_id, key, output, new_body, expires=g.expires)
            return self._reply(ref, reused=False)
        except StepError as exc:
            # A computation failure/timeout may have continued in a backend thread. Never retry it.
            self.store.fail(g.scope, g.run_id, key, str(exc), unknown=operation == "scat_model_run")
            raise
        except BaseException:
            self.store.fail(g.scope, g.run_id, key, "step_execution_unknown", unknown=True)
            raise

    @staticmethod
    def _reply(ref, *, reused):
        return {"status": "ok", "synthetic": True, "reference": ref.model_dump(), "reused": reused}
