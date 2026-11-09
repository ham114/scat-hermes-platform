"""S5 domain proposals use S4 approval/outbox and standard result repositories."""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from application.model_tasks import ModelTasks
from application.task_store import TaskConflict, digest
from hermes_runtime.release_store import planning_release
from hermes_runtime.skill_policy import load_policy
from workflow_runtime.catalog import describe, operation_names
from workflow_runtime.contracts import PRIMARY_SKILLS, WorkflowProposal, trial_manifest
from workflow_runtime.data import ScatSource, SyntheticSource, select_input, store_input


class WorkflowTasks(ModelTasks):
    def __init__(self, *args, workflow_enabled=False, workflow_synthetic=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.workflow_enabled = workflow_enabled
        self.workflow_source = SyntheticSource() if workflow_synthetic else None

    async def prepare_workflow(
        self, context, proposal, *, proposal_key, source_run_id=None, budget=None
    ):
        from analysis_runs import AnalysisRunComponents, AnalysisRunRecord, AnalysisRunStatusEvent
        from scat_model_runtime import (
            ComponentReference,
            ModelExecutionMode,
            ModelInputReference,
            ModelRunStatus,
            ModelTaskType,
        )

        if not self.workflow_enabled:
            raise TaskConflict("S5 workflow profile is not enabled")
        proposal = WorkflowProposal.model_validate(proposal)
        if proposal.kind == "forecast" or proposal.observation_last_n or proposal.as_of:
            require_pages = getattr(self.artifacts, "require_multipage", None)
            if require_pages is not None:
                try:
                    await require_pages()
                except ValueError:
                    raise TaskConflict(
                        "Forecast requires a multipage artifact collection before approval"
                    ) from None
        gpu = None
        if any(m.model == "scat-gbar-v23" for m in proposal.models):
            from workflow_runtime.gpu import registration

            gpu = registration(self.skills.root, required=True)
            require_multipage = getattr(self.artifacts, "require_multipage", None)
            if require_multipage is not None:
                await require_multipage()
            backend = gpu.backend()
            try:
                if not (await asyncio.to_thread(backend.health)).ready:
                    raise TaskConflict("Pinned CUDA model is not ready")
            finally:
                backend.close()
        if proposal.kind == "report":
            sources = []
            input_refs = []
            for task_id in proposal.source_task_ids:
                old = self.store.get(context.scope_key, task_id)
                if "workflow" not in old["snapshot"]:
                    raise TaskConflict("Report reuse requires a verified S5 workflow result")
                record = await self.runs.get_for_owner(old["run_id"], owner_user_id=context.user_id)
                if (
                    record is None
                    or record.thread_id != context.thread_id
                    or record.status not in {ModelRunStatus.SUCCEEDED, ModelRunStatus.PARTIAL}
                ):
                    raise TaskConflict("Source task has no verified completed result")
                if not record.result_artifact_ids:
                    raise TaskConflict("Source result unavailable")
                artifacts = [
                    await self.artifact(context, task_id, identity)
                    for identity in record.result_artifact_ids
                ]
                for artifact in artifacts:
                    if len(artifacts) > 1 and artifact["analysis_result"]["context"].get(
                        "report_overview"
                    ):
                        continue
                    sources.append({"source_task_id": task_id, **artifact})
                input_refs.extend(record.input_refs)
            frozen = {
                "schema_version": "workflow-input/v1",
                "scope": context.scope_key,
                "batch": proposal.batch,
                "synthetic": all(
                    s["analysis_result"]["context"].get("synthetic", False) for s in sources
                ),
                "sources": sources,
                "flights": [],
                "coverage": {"selected_count": 0},
                "source_version": "verified-result-reuse-v1",
            }
            references = tuple({r.model_dump_json(): r for r in input_refs}.values())
        else:
            source = self.workflow_source or ScatSource()
            frozen = await select_input(context, proposal, source)
            if proposal.kind == "forecast" or proposal.observation_last_n or proposal.as_of:
                if (
                    sum(len(f.get("observation_windows", [])) for f in frozen["flights"])
                    * len(trial_manifest(proposal))
                    > 80
                ):
                    raise TaskConflict("Prediction report exceeds 80 approved window/model pages")
            if gpu is not None:
                windows = sum(len(f["gbar_windows"]) for f in frozen["flights"])
                if (
                    windows
                    * sum(m.model == "scat-gbar-v23" for m in proposal.models)
                    * proposal.repeats
                    > gpu.max_windows_per_job
                ):
                    raise TaskConflict("GPU job exceeds the configured window ceiling")
            references = tuple(
                ModelInputReference(
                    dataset="scat",
                    batch=proposal.batch,
                    external_id=f["external_id"],
                    **(
                        {
                            "limit": proposal.observation_last_n,
                            "start_time": f["observation_windows"][0]["start_time"],
                            "end_time": f["observation_windows"][0]["forecast_origin"],
                        }
                        if proposal.kind == "forecast"
                        else {}
                    ),
                )
                for f in frozen["flights"]
            )
        identity = store_input(self.skills.root, frozen)
        policy = load_policy(self.skills.root)
        release = planning_release(self.skills.root, context.scope_key, source_run_id)
        primary = PRIMARY_SKILLS[proposal.kind]
        required = {primary, "scat-report"}
        if proposal.models:
            required.add("scat-evaluation")
        if proposal.materializations:
            required.add("scat-data-materialization")
        if required - release.entries.keys():
            raise TaskConflict("Required workflow skills are not published")
        parent = None
        if source_run_id:
            path = self.skills.parent_budget_path
            if path is None or not path.is_relative_to(self.skills.root) or not path.is_file():
                raise TaskConflict("Trusted parent budget unavailable")
            with closing(sqlite3.connect(path)) as db:
                if not db.execute(
                    "SELECT 1 FROM skill_run_budgets WHERE scope=? AND run_id=?",
                    (context.scope_key, source_run_id),
                ).fetchone():
                    raise TaskConflict("Parent budget ownership mismatch")
            parent = {
                "path": path.relative_to(self.skills.root).as_posix(),
                "run_id": source_run_id,
            }
        from hermes_runtime.trace_references import track_parent

        track_parent(self.skills.root, context.scope_key, parent, self.store.path)
        task_types = {
            "forecast": ModelTaskType.TRAJECTORY_PREDICTION,
            "comparison": ModelTaskType.MODEL_COMPARISON,
            "experiment": ModelTaskType.EXPERIMENT,
            "materialization": ModelTaskType.DATA_MATERIALIZATION,
            "report": ModelTaskType.REPORT_GENERATION,
        }
        now = datetime.now(UTC)
        run = AnalysisRunRecord(
            run_id=uuid4().hex,
            owner_user_id=context.user_id,
            thread_id=context.thread_id,
            task_type=task_types[proposal.kind],
            execution_mode=ModelExecutionMode.ASYNC,
            model_key="scat-workflow-" + proposal.kind,
            model_version="1",
            input_schema="workflow-input/v1",
            output_schema="AnalysisResult/1.4",
            input_refs=references,
            components=AnalysisRunComponents(
                execution_backend=ComponentReference(
                    key="durable-workflow-gpu-http" if gpu else "durable-workflow-cpu", version="1"
                )
            ),
            code_version="skills-s5-v1",
            status=ModelRunStatus.QUEUED,
            progress=0,
            status_history=(
                AnalysisRunStatusEvent(status=ModelRunStatus.QUEUED, progress=0, at=now),
            ),
            created_at=now,
            updated_at=now,
            queued_at=now,
            expires_at=now + timedelta(days=30),
        )
        catalog = self.skills.root / "services/hermes/workflow-operations.v1.json"
        snapshot = {
            "proposal": proposal.model_dump(mode="json"),
            "run": run.model_dump(mode="json"),
            "source": {
                "runtime": "hermes",
                "run_id": source_run_id,
                "thread_id": context.thread_id,
            },
            "workflow": {
                "schema_version": "workflow-plan/v1",
                "proposal": proposal.model_dump(mode="json"),
                "plan_sha256": digest(proposal.model_dump(mode="json")),
                "trials": trial_manifest(proposal),
                "input_sha256": identity,
                "synthetic": frozen["synthetic"],
                "coverage": frozen["coverage"],
                "catalog_sha256": hashlib.sha256(catalog.read_bytes()).hexdigest(),
            },
            "skills": {
                "schema_version": "skill-job/v1",
                "skill": primary,
                "skill_version": release.entries[primary]["version"],
                "approved_skills": {s: release.entries[s]["version"] for s in sorted(required)},
                "bundle_sha256": release.bundle_sha256,
                "operations": operation_names(proposal),
                "sources": self.skills.sources(),
                "input_sha256": identity,
                "operation_catalog_sha256": hashlib.sha256(
                    (self.skills.root / "services/hermes/model-operations.v1.json").read_bytes()
                ).hexdigest(),
                "budget": policy.effective(
                    category="background", user=context.user_id, request=budget or {}
                ).model_dump(),
                "budget_ceiling": policy.background_ceiling.model_dump(),
                "policy_sha256": policy.sha256,
                "parent_budget": parent,
                "max_concurrent_jobs": policy.resources.max_concurrent_jobs,
                "max_depth": policy.composition.max_depth,
                "expires": self.store.clock() + 86400,
            },
        }
        snapshot["workflow"]["prediction_contract"] = {
            "mode": "future_forecast"
            if proposal.kind == "forecast"
            else "historical_backtest"
            if proposal.models
            else "not_applicable",
            "forecast_origin": proposal.forecast_origin,
            "observation_last_n": proposal.observation_last_n,
            "as_of": proposal.as_of.isoformat() if proposal.as_of else None,
            "metrics_require_verified_truth": True,
            "windows": [
                {
                    key: value
                    for key, value in w.items()
                    if key not in {"observation_sha256", "source_indices_sha256"}
                }
                for f in frozen["flights"]
                for w in f.get("observation_windows", [])
            ]
            if proposal.kind == "forecast" or proposal.observation_last_n or proposal.as_of
            else [],
        }
        if gpu is not None:
            snapshot["workflow"]["gpu"] = gpu.pin()
        return self.view(self.store.prepare(context.scope_key, proposal_key, snapshot))

    def view(self, row):
        if "workflow" not in row["snapshot"]:
            return super().view(row)
        snap = row["snapshot"]
        p = snap["workflow"]["proposal"]
        return {
            "task_id": row["task_id"],
            "run_id": row["run_id"],
            "state": row["state"],
            "expires_at": row["expires"],
            "snapshot_sha256": row["sha256"],
            "confirmation_required": row["state"] == "pending",
            "model": {
                "model_key": snap["run"]["model_key"],
                "version": "1",
                "display_name": "未来轨迹预测"
                if p["kind"] == "forecast"
                else "业务流程 · " + p["kind"],
                "limits": {
                    "max_flights": p["max_flights"],
                    "trials": len(snap["workflow"]["trials"]),
                },
            },
            "task_type": snap["run"]["task_type"],
            "input_refs": snap["run"]["input_refs"],
            "parameters": {
                key: p[key]
                for key in ("observation_last_n", "forecast_origin", "as_of", "horizons_s")
                if key in p
            },
            "evaluator": None if p["kind"] == "forecast" else {"key": "m6-eval", "version": "1"},
            "components": snap["run"]["components"],
            "code_version": snap["run"]["code_version"],
            "source": snap["source"],
            "workflow": {
                **snap["workflow"],
                "tool_groups": {
                    group: [op for op in ops if op in snap["skills"]["operations"]]
                    for group, ops in describe()["groups"].items()
                    if any(op in snap["skills"]["operations"] for op in ops)
                },
            },
            "skill_job": self.skills.public(row),
        }
