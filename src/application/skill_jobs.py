"""S4 approved CPU job controller; job/lease/delegation identities never enter tool schemas."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path

from application.model_step_store import ModelStepStore, StepError
from application.model_steps import OPERATIONS, ModelSteps, implementation_snapshot, spec_digest
from application.skill_cpu_backend import DurableCPUPackage, process_start, request_stop
from application.skill_job_store import SkillJobStore
from application.task_store import TaskConflict, canonical, digest
from hermes_runtime.job_budget import ensure_account, import_parent, revise
from hermes_runtime.release_store import planning_release, resolve_release
from hermes_runtime.skill_policy import load_policy
from scat_model_runtime import (
    ComponentReference,
    ComponentRegistration,
    ComponentRegistry,
    ModelRegistry,
    ModelTaskRequest,
)
from scat_model_runtime.cpu_fixture import SyntheticInput, fixture_runtime

SOURCES = (
    "src/application/skill_jobs.py",
    "src/application/skill_job_store.py",
    "src/application/skill_cpu_backend.py",
    "src/deployment_runtime/client.py",
    "src/deployment_runtime/actions.py",
    "src/deployment_runtime/jobs.py",
    "src/hermes_runtime/task_bridge.py",
    "src/application/model_steps.py",
    "src/application/model_step_store.py",
    "src/scat_model_runtime/cpu_fixture.py",
    "src/scat_model_runtime/steps.py",
    "src/scat_model_runtime/step_codec.py",
    "src/hermes_runtime/job_budget.py",
    "src/hermes_runtime/skill_budget.py",
    "src/hermes_runtime/validation_campaign.py",
    "src/hermes_runtime/job_context.py",
    "src/hermes_runtime/native_budget.py",
    "src/hermes_runtime/skill_guard.py",
    "src/hermes_runtime/script_language.py",
    "src/hermes_runtime/script_resources.py",
    "src/hermes_runtime/script_policy.py",
    "src/hermes_runtime/script_runtime.py",
    "src/hermes_runtime/script_bridge.py",
    "scripts/run_skill_script_child.py",
    "config/skill-scripts-policy.json",
    "src/hermes_runtime/skills.py",
    "src/hermes_runtime/release_store.py",
    "src/hermes_runtime/release_bridge.py",
    "src/hermes_runtime/skill_policy.py",
    "src/hermes_runtime/skill_cache.py",
    "src/hermes_runtime/trace_lifecycle.py",
    "src/hermes_runtime/trace_references.py",
    "src/hermes_runtime/entrypoint.py",
    "src/hermes_runtime/profiles.py",
    "src/hermes_runtime/native_profiles.py",
    "services/hermes/profiles.lock.json",
    "config/hermes-profiles/scat-worker/config.yaml",
    "config/hermes-profiles/scat-worker/SOUL.md",
    "config/hermes-profiles/scat-worker/operations.md",
    "src/application/skill_worker.py",
    "src/application/skill_job_factory.py",
    "src/hermes_runtime/model_step_bridge.py",
    "src/hermes_runtime/workflow_bridge.py",
    "scripts/run_workflow_tool.py",
    "scripts/run_model_step_tool.py",
    "scripts/run_skill_cpu_backend.py",
    "scripts/run_hermes_api.py",
    "scripts/run_model_worker.py",
    "src/application/task_store.py",
    "src/application/workflow_tasks.py",
    "src/application/workflow_operations.py",
    "src/workflow_runtime/contracts.py",
    "src/workflow_runtime/catalog.py",
    "src/workflow_runtime/data.py",
    "src/workflow_runtime/windows.py",
    "src/workflow_runtime/forecast.py",
    "src/workflow_runtime/numerics.py",
    "src/workflow_runtime/report.py",
    "src/workflow_runtime/backend.py",
    "src/workflow_runtime/gpu.py",
    "src/scat_model_runtime/backends/base.py",
    "src/scat_model_runtime/backends/bento_http.py",
    "src/scat_model_runtime/backends/gbar_codec.py",
    "src/scat_model_runtime/task_io.py",
    "src/gbar_core/preprocessing.py",
    "src/gbar_core/transport.py",
    "src/scat_model_runtime/regression_evaluators.py",
    "src/scat_model_runtime/alignment_core.py",
    "src/scat_model_runtime/resolvers/scat_gbar.py",
    "src/agents/analysis_results.py",
    "services/hermes/gpu-models.v1.json",
    "src/analysis_runs/artifact_repository.py",
    "scripts/run_workflow_backend.py",
    "src/scat_model_runtime/contracts.py",
    "src/scat/experiments/evaluation.py",
    "src/scat/experiments/grouping.py",
    "src/scat/prediction/alignment.py",
    "src/scat/prediction/baselines.py",
    "src/scat/prediction/interfaces.py",
    "src/scat/materialization/tasks.py",
    "src/scat/materialization/contracts.py",
    "src/scat/materialization/aggregates.py",
    "src/scat/preprocessing/phases.py",
)


class SkillJobs:
    def __init__(self, tasks, root, *, parent_budget_path=None):
        self.root = Path(root).resolve()
        self.store = SkillJobStore(tasks)
        self.parent_budget_path = Path(parent_budget_path).resolve() if parent_budget_path else None

    def kernel(self, *, row=None, session=None):
        kernel, grant, _ = fixture_runtime(
            row["scope"] if row else "prepare", row["run_id"] if row else "prepare"
        )
        spec = kernel.models.list()[0]
        spec = spec.model_copy(
            update={
                "execution_backend": ComponentReference(key="durable-cpu", version="1"),
                "resource_limits": spec.resource_limits.model_copy(update={"timeout_seconds": 30}),
            }
        )
        kernel.models = ModelRegistry([spec])
        package = DurableCPUPackage(
            self.store,
            root=self.root,
            task_id=row["task_id"] if row else None,
            worker=session["worker"] if session else None,
            generation=session["generation"] if session else None,
        )
        kernel.regression_model_packages = ComponentRegistry(
            "durable CPU package", [ComponentRegistration(spec.model_package, package)]
        )
        return kernel, replace(
            grant, components_sha256=implementation_snapshot(kernel), spec_sha256=spec_digest(spec)
        )

    def sources(self):
        return {p: hashlib.sha256((self.root / p).read_bytes()).hexdigest() for p in SOURCES}

    def snapshot(self, context, request, spec, source_run_id, *, budget=None):
        kernel, expected = self.kernel()
        sample = ModelTaskRequest.model_validate_json(expected.request_json)
        if spec_digest(spec) != expected.spec_sha256:
            raise TaskConflict("CPU model registration changed")
        if (
            request.model_key != sample.model_key
            or request.model_version != sample.model_version
            or request.references != sample.references
            or request.parameters
            or request.evaluator != sample.evaluator
        ):
            # The registry default evaluator is resolved before snapshot; compare its exact identity.
            allowed_evaluator = kernel.models.list()[0].evaluator
            if (
                request.model_key != sample.model_key
                or request.model_version != sample.model_version
                or request.references != sample.references
                or request.parameters
                or request.evaluator != allowed_evaluator
            ):
                raise TaskConflict("S4 CPU profile requires the reviewed fixed sample")
        policy = load_policy(self.root)
        release = planning_release(self.root, context.scope_key, source_run_id)
        parent = None
        if source_run_id:
            if (
                self.parent_budget_path is None
                or not self.parent_budget_path.is_relative_to(self.root)
                or not self.parent_budget_path.is_file()
            ):
                raise TaskConflict("Trusted planner budget is unavailable")
            with closing(sqlite3.connect(self.parent_budget_path)) as db:
                found = db.execute(
                    "SELECT 1 FROM skill_run_budgets WHERE scope=? AND run_id=?",
                    (context.scope_key, source_run_id),
                ).fetchone()
            if found is None:
                raise TaskConflict("Planner budget does not belong to this run")
            parent = {
                "path": self.parent_budget_path.relative_to(self.root).as_posix(),
                "run_id": source_run_id,
            }
        entry = release.entries["scat-model-analysis"]
        from hermes_runtime.trace_references import track_parent

        track_parent(self.root, context.scope_key, parent, self.store.path)
        return {
            "schema_version": "skill-job/v1",
            "skill": "scat-model-analysis",
            "skill_version": entry["version"],
            "bundle_sha256": release.bundle_sha256,
            "operations": list(OPERATIONS),
            "operation_catalog_sha256": hashlib.sha256(
                (self.root / "services/hermes/model-operations.v1.json").read_bytes()
            ).hexdigest(),
            "components_sha256": implementation_snapshot(kernel),
            "sources": self.sources(),
            "input_sha256": digest(SyntheticInput.build(request=request).model_dump(mode="json")),
            "budget": policy.effective(
                category="background", user=context.user_id, request=budget or {}
            ).model_dump(),
            "budget_ceiling": policy.background_ceiling.model_dump(),
            "policy_sha256": policy.sha256,
            "parent_budget": parent,
            "max_concurrent_jobs": policy.resources.max_concurrent_jobs,
            "max_depth": policy.composition.max_depth,
            "expires": self.store.clock() + 86400,
        }

    def validate(self, row):
        if "workflow" in row["snapshot"]:
            from application.workflow_operations import WorkflowOperations

            return WorkflowOperations(self).validate(row)
        snap = row["snapshot"]["skills"]
        try:
            release = resolve_release(self.root, snap["bundle_sha256"])
        except (ValueError, OSError):
            raise TaskConflict("Approved skill release unavailable") from None
        if (
            snap["bundle_sha256"] != release.bundle_sha256
            or snap["sources"] != self.sources()
            or snap["operation_catalog_sha256"]
            != hashlib.sha256(
                (self.root / "services/hermes/model-operations.v1.json").read_bytes()
            ).hexdigest()
        ):
            raise TaskConflict("Approved skill/operation/implementation changed")
        kernel, _ = self.kernel()
        if snap["components_sha256"] != implementation_snapshot(kernel):
            raise TaskConflict("Approved component changed")
        request = ModelTaskRequest.model_validate(row["snapshot"]["request"])
        if snap["input_sha256"] != digest(
            SyntheticInput.build(request=request).model_dump(mode="json")
        ):
            raise TaskConflict("Approved CPU sample changed")
        if snap["expires"] <= self.store.clock():
            raise TaskConflict("Approved job expired")

    def confirm(self, context, row, sha):
        if row["state"] == "pending":
            self.validate(row)
        result = self.store.confirm(context.scope_key, row["task_id"], sha)
        ensure_account(self.store, result)
        import_parent(self.store, result, self.root)
        return result

    def ready_parents(self):
        with closing(self.store.tasks.connect()) as db:
            rows = db.execute(
                "SELECT t.* FROM model_tasks t JOIN skill_jobs j USING(task_id) WHERE j.state='queued' AND j.parent_imported=0"
            ).fetchall()
        for row in rows:
            import_parent(self.store, self.store.get(row["scope"], row["task_id"]), self.root)

    def public(self, row):
        full = self.store.get(row["scope"], row["task_id"])
        if full["job"] is None:
            snap = row["snapshot"]["skills"]
            return {
                "state": "awaiting_confirmation",
                "approved": False,
                "skill": snap["skill"],
                "skill_version": snap["skill_version"],
                "budget": snap["budget"],
                "operations": snap["operations"],
                "input_sha256": snap["input_sha256"],
                "bundle_sha256": snap["bundle_sha256"],
                "expires": snap["expires"],
                "includes_parent_planning_usage": bool(snap["parent_budget"]),
            }
        with closing(self.store.tasks.connect()) as db:
            steps = []
            # A newly confirmed job may not have created the step tables yet.
            if db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='model_step_receipts'"
            ).fetchone():
                steps = [
                    dict(r)
                    for r in db.execute(
                        "SELECT operation,state,reference,error FROM model_step_receipts WHERE scope=? AND run=? ORDER BY rowid",
                        (row["scope"], row["run_id"]),
                    )
                ]
            backend = [
                dict(r)
                for r in db.execute(
                    "SELECT id,state,output_sha,error,calls FROM skill_backend_executions WHERE task_id=?",
                    (row["task_id"],),
                )
            ]
        budget = ensure_account(self.store, full).snapshot()
        return {
            "state": full["job"]["state"],
            "approved": True,
            "generation": full["job"]["generation"],
            "budget_revision": full["job"]["budget_revision"],
            "budget": budget,
            "steps": steps,
            "backend_executions": backend,
            "result_reference": json.loads(full["job"]["result_reference"])
            if full["job"]["result_reference"]
            else None,
            "error": full["job"]["error"],
        }

    async def invoke(self, session_id, operation, arguments, *, scope, native_run):
        session = self.store.session(session_id, operation=operation)
        if session["scope"] != scope or session["native_run"] != native_run:
            raise TaskConflict("Delegated native run mismatch")
        with closing(self.store.tasks.connect()) as db:
            row = db.execute(
                "SELECT * FROM model_tasks WHERE task_id=?", (session["task_id"],)
            ).fetchone()
        row = self.store.get(row["scope"], session["task_id"])
        self.validate(row)
        if "workflow" in row["snapshot"]:
            from application.workflow_operations import WorkflowOperations

            return await WorkflowOperations(self).invoke(
                row, session, session_id, operation, arguments
            )
        kernel, grant = self.kernel(row=row, session=session)
        request = ModelTaskRequest.model_validate(row["snapshot"]["request"])
        grant = replace(
            grant,
            request_json=canonical(request.model_dump(mode="json")),
            expires=row["snapshot"]["skills"]["expires"],
            job_id=row["task_id"],
        )

        def fence(db):
            self.store.fence(
                db, row["task_id"], session["worker"], session["generation"], session["revision"]
            )
            # Recheck revocation on every effect, in the same SQLite write transaction.
            self.store.session(session_id, operation=operation)
            budget = db.execute(
                "SELECT * FROM skill_run_budgets WHERE scope=? AND run_id=?",
                (row["scope"], row["run_id"]),
            ).fetchone()
            if (
                budget is None
                or budget["exhausted"]
                or budget["finished_at"] is not None
                or self.store.clock() - budget["started_at"]
                >= json.loads(budget["limits_json"])["wall_seconds"]
            ):
                raise StepError("job_budget_exhausted")

        artifacts = ModelStepStore(self.store.path, workspace_root=self.root, fence=fence)
        result = await ModelSteps(kernel, artifacts, grant).invoke(operation, arguments)
        with self.store.transaction() as db:
            self.store.fence(
                db, row["task_id"], session["worker"], session["generation"], session["revision"]
            )
            self.store.event(
                db,
                row["task_id"],
                "step_returned",
                operation=operation,
                session=session_id,
                reference=result.get("reference"),
            )
        result["execution_path"] = "approved_skill_job/v1"
        return result

    def reconcile(self, context, task_id, *, resume=True):
        """Evidence-based recovery, never an LLM-provided terminal assertion."""
        row = self.store.get(context.scope_key, task_id)
        job = row["job"]
        if job is None or job["state"] in {"succeeded", "partial", "failed", "cancelled"}:
            return row
        if job["state"] == "running" and job["lease"] > self.store.clock():
            raise TaskConflict("Worker lease is still active")
        with self.store.transaction() as db:
            current = db.execute(
                "SELECT state,generation FROM skill_jobs WHERE task_id=?", (task_id,)
            ).fetchone()
            if (
                current is None
                or current["generation"] != job["generation"]
                or current["state"] != job["state"]
            ):
                raise TaskConflict("Skill job changed during reconciliation")
            backend = db.execute(
                "SELECT * FROM skill_backend_executions WHERE task_id=?", (task_id,)
            ).fetchall()
            for item in backend:
                if item["state"] == "succeeded" and (
                    digest(json.loads(item["request"])) != item["request_sha"]
                    or digest(json.loads(item["output"])) != item["output_sha"]
                ):
                    raise TaskConflict("Backend completion integrity failed")
            if job["state"] == "cancel_requested":
                for item in backend:
                    if (
                        item["state"] == "running"
                        and item["pid"]
                        and item["process_start"]
                        and process_start(item["pid"]) != item["process_start"]
                    ):
                        db.execute(
                            "UPDATE skill_backend_executions SET state='cancelled',finished=?,error='verified_process_stopped_result_unknown' WHERE id=? AND state='running'",
                            (self.store.clock(), item["id"]),
                        )
                backend = db.execute(
                    "SELECT * FROM skill_backend_executions WHERE task_id=?", (task_id,)
                ).fetchall()
            if any(b["state"] in {"running", "unknown"} for b in backend):
                if job["state"] == "cancel_requested":
                    request_stop(self.store, task_id)
                return row
            if job["state"] == "cancel_requested":
                target = "cancelled"
            elif (
                any(b["state"] in {"failed", "cancelled"} for b in backend)
                or "workflow" in row["snapshot"]
                and row["snapshot"]["workflow"]["proposal"]["failure_policy"] == "stop"
                and any(
                    b["state"] == "succeeded"
                    and json.loads(b["output"]).get("status") != "succeeded"
                    for b in backend
                )
            ):
                target = "failed"
            else:
                target = "queued" if resume else "paused"
            # Every CPU effect must first have a durable backend row. No row/prepared proves
            # no calculation began; succeeded is a verified external result, so retrieval is safe.
            if db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='model_step_receipts'"
            ).fetchone():
                pending = db.execute(
                    "SELECT * FROM model_step_receipts WHERE scope=? AND run=? AND state IN ('running','unknown')",
                    (row["scope"], row["run_id"]),
                ).fetchall()
                for step in pending:
                    execution = next(
                        (b for b in backend if b["step_key"] == step["step_key"]), None
                    )
                    if (
                        execution
                        and execution["state"] == "succeeded"
                        and digest(json.loads(execution["output"])) != execution["output_sha"]
                    ):
                        raise TaskConflict("Backend completion integrity failed")
                    db.execute(
                        "UPDATE model_step_receipts SET state='prepared',error=NULL WHERE scope=? AND run=? AND step_key=?",
                        (row["scope"], row["run_id"], step["step_key"]),
                    )
        if target == "queued":
            budget = ensure_account(self.store, row).snapshot()
            limits = json.loads(budget["limits_json"])
            if (
                budget["exhausted"]
                or self.store.clock() - budget["started_at"] >= limits["wall_seconds"]
            ):
                target = "paused"
        self.store.state(
            context.scope_key,
            task_id,
            target,
            error="cancel_acknowledged" if target == "cancelled" else None,
            expected_generation=job["generation"],
        )
        if target in {"failed", "cancelled"}:
            ensure_account(self.store, row).finish()
        return self.store.get(context.scope_key, task_id)

    def revise_budget(self, context, task_id, limits):
        row = self.store.get(context.scope_key, task_id)
        policy = load_policy(self.root)
        frozen = row["snapshot"]["skills"]["budget_ceiling"]
        ceiling = policy.background_ceiling.model_copy(
            update={
                key: min(getattr(policy.background_ceiling, key), frozen[key])
                for key in ["max_turns", "max_llm_tokens", "max_tool_calls", "wall_seconds"]
            }
        )
        revise(self.store, row, limits, ceiling.model_dump())
        return self.reconcile(context, task_id, resume=True)

    def read_result(self, context, task_id):
        row = self.store.get(context.scope_key, task_id)
        ref = row["job"]["result_reference"] if row["job"] else None
        if not ref:
            raise TaskConflict("No verified result reference")
        store = ModelStepStore(self.store.path, workspace_root=self.root)
        return store.read(row["scope"], row["run_id"], json.loads(ref), kind="result")
