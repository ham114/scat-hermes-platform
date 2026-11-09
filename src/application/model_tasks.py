"""Trusted task application service; only HTTP human actions enqueue execution."""

from __future__ import annotations

from uuid import uuid4

from pydantic import Field

from application.context import ExecutionContext
from application.task_store import TaskConflict, TaskStore, digest
from scat_model_runtime.contracts import (
    ComponentReference,
    ModelContract,
    ModelInputReference,
    ModelParameterValue,
    ModelSpec,
    ModelTaskRequest,
    ModelTaskType,
)


class TaskProposal(ModelContract):
    task_type: ModelTaskType
    model_key: str = Field(min_length=1, max_length=100)
    model_version: str | None = Field(default=None, min_length=1, max_length=64)
    input_ref: ModelInputReference | None = None
    input_refs: tuple[ModelInputReference, ...] = Field(default=(), max_length=100)
    parameters: dict[str, ModelParameterValue] = Field(default_factory=dict, max_length=50)
    evaluator: ComponentReference | None = None

    def request(self, context):
        return ModelTaskRequest(
            **self.model_dump(),
            thread_id=context.thread_id,
            requested_by=context.user_id,
            confirmed=False,
        )


class ModelTasks:
    def __init__(self, store: TaskStore, *, models=None, runs=None, artifacts=None, skills=None):
        from analysis_runs import analysis_artifact_repository, analysis_run_repository
        from scat_model_runtime import model_registry

        self.store = store
        self.models = models if models is not None else model_registry
        self.runs = runs if runs is not None else analysis_run_repository
        self.artifacts = artifacts if artifacts is not None else analysis_artifact_repository
        self.skills = skills

    def catalog(self):
        from application.model_records import _model_summary

        return [_model_summary(spec) for spec in self.models.list()]

    def prepare(
        self,
        context: ExecutionContext,
        proposal: TaskProposal,
        *,
        proposal_key: str,
        source_run_id=None,
        budget=None,
    ):
        from application.model_records import _new_analysis_run

        request = proposal.request(context)
        spec = self.models.match(request)
        evaluator = request.evaluator or spec.evaluator
        if evaluator is not None and evaluator not in spec.allowed_evaluators:
            raise TaskConflict("Evaluator is not permitted")
        request = request.model_copy(update={"model_version": spec.version, "evaluator": evaluator})
        run = _new_analysis_run(
            run_id=uuid4().hex,
            request=request,
            spec=spec,
            thread_id=context.thread_id,
            user_id=context.user_id,
        )
        snapshot = {
            "proposal": proposal.model_dump(mode="json"),
            "request": request.model_dump(mode="json"),
            "spec": spec.model_dump(mode="json"),
            "run": run.model_dump(mode="json"),
            "source": {
                "runtime": "hermes",
                "run_id": source_run_id,
                "thread_id": context.thread_id,
            },
        }
        if self.skills is not None:
            snapshot["skills"] = self.skills.snapshot(
                context, request, spec, source_run_id, budget=budget
            )
        return self.view(self.store.prepare(context.scope_key, proposal_key, snapshot))

    def view(self, row):
        from application.model_records import _model_summary

        snapshot = row["snapshot"]
        request = snapshot["request"]
        result = {
            "task_id": row["task_id"],
            "run_id": row["run_id"],
            "state": row["state"],
            "snapshot_sha256": row["sha256"],
            "created_at": row["created"],
            "expires_at": row["expires"],
            "error_code": row["error_code"],
            "model": _model_summary(ModelSpec.model_validate(snapshot["spec"])),
            "input_refs": (
                [request["input_ref"]] if request["input_ref"] else request["input_refs"]
            ),
            "parameters": request["parameters"],
            "evaluator": request["evaluator"],
            "components": snapshot["run"]["components"],
            "code_version": snapshot["run"]["code_version"],
            "source": snapshot["source"],
            "confirmation_required": row["state"] == "pending",
        }
        if self.skills is not None and "skills" in snapshot:
            result["skill_job"] = self.skills.public(row)
        return result

    async def get(self, context, task_id):
        row = self.store.get(context.scope_key, task_id)
        view = self.view(row)
        record = await self.runs.get_for_owner(row["run_id"], owner_user_id=context.user_id)
        from datetime import UTC, datetime

        if (
            record is not None
            and record.thread_id == context.thread_id
            and (record.expires_at is None or record.expires_at > datetime.now(UTC))
        ):
            from application.model_records import _public_run_record

            view["run"] = _public_run_record(record)
        else:
            view["run"] = None
        view["execution_outcome_unknown"] = row["state"] == "unknown"
        return view

    async def artifact(self, context, task_id, artifact_id):
        row = self.store.get(context.scope_key, task_id)
        record = await self.artifacts.get_for_owner(artifact_id, owner_user_id=context.user_id)
        import hashlib
        from datetime import UTC, datetime

        if (
            record is None
            or record.run_id != row["run_id"]
            or record.thread_id != context.thread_id
            or (record.expires_at is not None and record.expires_at <= datetime.now(UTC))
        ):
            from application.task_store import TaskNotFound

            raise TaskNotFound("Artifact not found")
        payload = record.canonical_payload(record.analysis_result)
        if (
            len(payload) != record.payload_size_bytes
            or hashlib.sha256(payload).hexdigest() != record.payload_sha256
        ):
            raise TaskConflict("Artifact integrity check failed")
        return {
            "artifact_id": record.artifact_id,
            "run_id": record.run_id,
            "analysis_result": record.analysis_result.model_dump(mode="json"),
            "payload_sha256": record.payload_sha256,
        }

    def confirm(self, context, task_id, sha256):
        row = self.store.get(context.scope_key, task_id)
        if self.skills is not None and "skills" in row["snapshot"]:
            return self.view(self.skills.confirm(context, row, sha256))
        if row["state"] != "pending":
            return self.view(self.store.confirm(context.scope_key, task_id, sha256))
        request = ModelTaskRequest.model_validate(row["snapshot"]["request"])
        current = self.models.match(request)
        if digest(current.model_dump(mode="json")) != digest(row["snapshot"]["spec"]):
            raise TaskConflict("Model registration changed; prepare again")
        return self.view(self.store.confirm(context.scope_key, task_id, sha256))
