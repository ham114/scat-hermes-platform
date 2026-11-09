"""Independent S5 operations; Hermes chooses business order and explicit reference flow."""

from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import closing

from application.model_step_store import ModelStepStore, StepError
from application.task_store import TaskConflict, digest
from hermes_runtime.release_store import resolve_release
from workflow_runtime import backend
from workflow_runtime.catalog import OPERATIONS, operation_names
from workflow_runtime.contracts import WorkflowProposal, trial_manifest
from workflow_runtime.data import load_input
from workflow_runtime.numerics import summarize
from workflow_runtime.report import build


class WorkflowOperations:
    def __init__(self, skills):
        self.skills = skills
        self.jobs = skills.store
        self.root = skills.root

    def validate(self, row):
        snap = row["snapshot"]
        plan = snap["workflow"]
        grant = snap["skills"]
        proposal = WorkflowProposal.model_validate(plan["proposal"])
        if "gpu" in plan:
            from workflow_runtime.gpu import registration

            registration(self.root, required=True, expected=plan["gpu"])
        try:
            release = resolve_release(self.root, grant["bundle_sha256"])
        except (ValueError, OSError):
            raise TaskConflict("Approved workflow skill release unavailable") from None
        if (
            grant["bundle_sha256"] != release.bundle_sha256
            or grant["sources"] != self.skills.sources()
        ):
            raise TaskConflict("Approved workflow skill or implementation changed")
        if (
            plan["catalog_sha256"]
            != hashlib.sha256(
                (self.root / "services/hermes/workflow-operations.v1.json").read_bytes()
            ).hexdigest()
        ):
            raise TaskConflict("Approved workflow operation contract changed")
        if (
            grant["operations"] != operation_names(proposal)
            or plan["trials"] != trial_manifest(proposal)
            or plan["plan_sha256"] != digest(proposal.model_dump(mode="json"))
        ):
            raise TaskConflict("Approved workflow plan changed")
        if grant["expires"] <= self.jobs.clock():
            raise TaskConflict("Approved workflow expired")
        if any(
            name not in release.entries or release.entries[name]["version"] != version
            for name, version in grant["approved_skills"].items()
        ):
            raise TaskConflict("Approved workflow skill set changed")
        return load_input(self.root, plan["input_sha256"], scope=row["scope"])

    def artifacts(self, row, session=None, session_id=None, operation=None):
        def fence(db):
            self.jobs.fence(
                db, row["task_id"], session["worker"], session["generation"], session["revision"]
            )
            self.jobs.session(session_id, operation=operation)
            account = db.execute(
                "SELECT * FROM skill_run_budgets WHERE scope=? AND run_id=?",
                (row["scope"], row["run_id"]),
            ).fetchone()
            if (
                account is None
                or account["exhausted"]
                or account["finished_at"] is not None
                or self.jobs.clock() - account["started_at"]
                >= json.loads(account["limits_json"])["wall_seconds"]
            ):
                raise StepError("workflow_job_budget_exhausted")

        return ModelStepStore(
            self.jobs.path, workspace_root=self.root, fence=fence if session else None
        )

    @staticmethod
    def load(store, row, reference, *, kinds=None):
        body = store.read(row["scope"], row["run_id"], reference, kind=reference["kind"])
        plan = row["snapshot"]["workflow"]
        if (
            body.get("schema_version") != "workflow-artifact/v1"
            or body.get("plan_sha256") != plan["plan_sha256"]
            or body.get("input_sha256") != plan["input_sha256"]
        ):
            raise StepError("workflow_artifact_lineage_mismatch")
        if kinds and body["workflow_kind"] not in kinds:
            raise StepError("workflow_artifact_kind_mismatch")
        return body

    async def invoke(self, row, session, session_id, operation, arguments):
        if operation not in OPERATIONS:
            raise StepError("unknown_workflow_operation")
        args = OPERATIONS[operation][1].model_validate(arguments).model_dump(mode="json")
        frozen = self.validate(row)
        plan = row["snapshot"]["workflow"]
        proposal = WorkflowProposal.model_validate(plan["proposal"])
        store = self.artifacts(row, session, session_id, operation)
        refs = args.get("references", [args["reference"]] if "reference" in args else [])
        if len({r["artifact_id"] for r in refs}) != len(refs):
            raise StepError("duplicate_workflow_references")
        bodies = [self.load(store, row, ref) for ref in refs]
        if operation in {"scat_report_read", "scat_materialization_read"}:
            expected = "result" if operation == "scat_report_read" else "materialization"
            self.load(store, row, refs[0], kinds={expected})
            result = {"status": "ok", "reference": refs[0], "synthetic": frozen["synthetic"]}
            if expected == "result":
                result["analysis_result"] = bodies[0]["analysis_result"]
                if "report_pages" in bodies[0]:
                    overview = bodies[0]["analysis_result"]["context"]
                    result["report_manifest"] = {
                        "schema_version": "analysis-report/v1",
                        "page_count": overview["report_page_count"],
                        "pages_sha256": overview["report_pages_sha256"],
                        "pages": [
                            {
                                "index": index,
                                "schema_version": page["schema_version"],
                                "dataset": page["dataset"],
                                "start_time": page.get("start_time"),
                                "payload_sha256": digest(page),
                            }
                            for index, page in enumerate(bodies[0]["report_pages"], 1)
                        ],
                    }
            else:
                result["coverage"] = {
                    k: v for k, v in bodies[0]["payload"].items() if k != "records"
                }
                records = bodies[0]["payload"]["records"]
                if bodies[0]["payload"]["kind"] == "prediction_metrics":
                    records = sorted(
                        records, key=lambda r: r.get("horizontal_error_m") or 0, reverse=True
                    )
                preview = []
                for record in records[:20]:
                    item = dict(record)
                    if "segments" in item:
                        item["segments_total"] = len(item["segments"])
                        item["segments"] = item["segments"][:5]
                    preview.append(item)
                result["records_preview"] = preview
                result["records_total"] = len(records)
                result["preview_record_limit"] = 20
                result["preview_segments_per_flight_limit"] = 5
        else:
            key, cached = store.reserve(row["scope"], row["run_id"], operation, args)
            if cached:
                cached_body = self.load(store, row, cached.model_dump())
                result = {
                    "status": "ok",
                    "reference": cached.model_dump(),
                    "reused": True,
                    "synthetic": frozen["synthetic"],
                }
                if cached_body["workflow_kind"] == "sample":
                    result["plan"] = cached_body["payload"]
                elif cached_body["workflow_kind"] in {"trial", "materialization"}:
                    result["unit_outcome"] = cached_body["payload"]["status"]
            else:
                try:
                    payload, kind, execution = None, None, None
                    if operation == "scat_sample_prepare":
                        payload = {
                            "prediction_contract": plan.get("prediction_contract"),
                            "coverage": frozen["coverage"],
                            "trial_ids": [t["trial_id"] for t in plan["trials"]],
                            "approved_materializations": list(proposal.materializations),
                            "source_count": len(frozen.get("sources", [])),
                        }
                        kind = "sample"
                    elif operation in {"scat_trial_run", "scat_materialize"}:
                        self.load(store, row, refs[0], kinds={"sample"})
                        unit = args["trial_id"] if operation == "scat_trial_run" else args["kind"]
                        allowed = (
                            {t["trial_id"] for t in plan["trials"]}
                            if operation == "scat_trial_run"
                            else set(proposal.materializations)
                        )
                        if unit not in allowed:
                            raise StepError("workflow_unit_outside_approval")
                        execution, payload = await asyncio.to_thread(
                            backend.run, self.jobs, self.root, row, session, key, operation, unit
                        )
                        kind = "trial" if operation == "scat_trial_run" else "materialization"
                    elif operation == "scat_evaluation_summarize":
                        if any(
                            b["workflow_kind"] not in {"trial", "materialization"} for b in bodies
                        ):
                            raise StepError("workflow_evaluation_input_kind")
                        trials = [b["payload"] for b in bodies if b["workflow_kind"] == "trial"]
                        if {t["trial_id"] for t in trials} != {
                            t["trial_id"] for t in plan["trials"]
                        } or len(trials) != len(plan["trials"]):
                            raise StepError("workflow_evaluation_requires_all_approved_trials")
                        phase_bodies = [
                            b
                            for b in bodies
                            if b["workflow_kind"] == "materialization"
                            and b["payload"]["kind"] == "phase_segments"
                        ]
                        if proposal.missing_phase == "build" and not phase_bodies:
                            raise StepError("approved_phase_materialization_required")
                        phases = (
                            {
                                f["external_id"]: f["data"].get("existing_phase_segments", [])
                                for f in frozen["flights"]
                            }
                            if proposal.missing_phase != "build"
                            else {}
                        )
                        phases.update(
                            {
                                r["external_id"]: r["segments"]
                                for b in phase_bodies
                                for r in b["payload"]["records"]
                            }
                        )
                        payload = await asyncio.to_thread(
                            summarize, trials, plan["proposal"], phases=phases
                        )
                        kind = "evaluation"
                    elif operation == "scat_evaluation_diagnose":
                        body = self.load(store, row, refs[0], kinds={"evaluation"})
                        payload = {
                            "trials": [
                                {
                                    "trial_id": t["trial_id"],
                                    "worst_cases": t["worst_cases"],
                                    "failed_flights": t["failed_flights"],
                                    "exclusions": t["exclusions"],
                                }
                                for t in body["payload"]["trials"]
                            ]
                        }
                        kind = "diagnosis"
                    elif operation == "scat_report_build":
                        evaluation = [b for b in bodies if b["workflow_kind"] == "evaluation"]
                        mats = [
                            b["payload"]["kind"]
                            for b in bodies
                            if b["workflow_kind"] == "materialization"
                        ]
                        if plan["trials"] and len(evaluation) != 1:
                            raise StepError("verified_evaluation_required_for_report")
                        diagnosis = [b for b in bodies if b["workflow_kind"] == "diagnosis"]
                        if plan["trials"] and (
                            len(diagnosis) != 1
                            or diagnosis[0]["parents"] != [refs[bodies.index(evaluation[0])]]
                        ):
                            raise StepError("verified_diagnosis_required_for_report")
                        if set(mats) != set(proposal.materializations) or len(mats) != len(
                            proposal.materializations
                        ):
                            raise StepError("all_approved_materializations_required_for_report")
                        if proposal.kind == "report" and (
                            len(bodies) != 1 or bodies[0]["workflow_kind"] != "sample"
                        ):
                            raise StepError("frozen_source_results_required")
                        pages = []
                        analysis, outcome = build(proposal, frozen, bodies, refs, pages=pages)
                        payload = {"outcome": outcome}
                        kind = "result"
                    body = {
                        "schema_version": "workflow-artifact/v1",
                        "workflow_kind": kind,
                        "plan_sha256": plan["plan_sha256"],
                        "input_sha256": plan["input_sha256"],
                        "parents": refs,
                        "payload": payload,
                        "backend_execution_id": execution,
                    }
                    if kind == "result":
                        body["analysis_result"] = analysis
                        body["report_pages"] = pages
                    reference = store.save(
                        row["scope"],
                        row["run_id"],
                        key,
                        OPERATIONS[operation][3],
                        body,
                        expires=row["snapshot"]["skills"]["expires"],
                    )
                    result = {
                        "status": "ok",
                        "reference": reference.model_dump(),
                        "reused": False,
                        "synthetic": frozen["synthetic"],
                    }
                    if kind == "sample":
                        result["plan"] = payload
                    elif kind in {"trial", "materialization"}:
                        result["unit_outcome"] = payload["status"]
                        result["record_count"] = payload.get(
                            "record_count", len(payload.get("samples", []))
                        )
                        result["failed_flights"] = payload.get("failed_flights", [])
                except BaseException as exc:
                    store.fail(
                        row["scope"],
                        row["run_id"],
                        key,
                        "workflow_operation_outcome_unknown",
                        unknown=True,
                    )
                    if isinstance(exc, StepError) and str(exc) in {
                        "workflow_resource_busy",
                        "workflow_prerequisites_incomplete",
                    }:
                        with self.jobs.transaction() as db:
                            self.jobs.fence(
                                db,
                                row["task_id"],
                                session["worker"],
                                session["generation"],
                                session["revision"],
                            )
                            db.execute(
                                "UPDATE model_step_receipts SET state='prepared',error=NULL WHERE scope=? AND run=? AND step_key=? AND state='unknown'",
                                (row["scope"], row["run_id"], key),
                            )
                    raise
        with self.jobs.transaction() as db:
            self.jobs.fence(
                db, row["task_id"], session["worker"], session["generation"], session["revision"]
            )
            self.jobs.event(
                db,
                row["task_id"],
                "step_returned",
                operation=operation,
                session=session_id,
                reference=result["reference"],
            )
        result["execution_path"] = "approved_skill_workflow/v1"
        return result

    def completion_reference(self, row):
        store = self.artifacts(row)
        with closing(self.jobs.tasks.connect()) as db:
            saved = db.execute(
                "SELECT reference FROM model_step_receipts WHERE scope=? AND run=? AND operation='scat_report_build' AND state='succeeded'",
                (row["scope"], row["run_id"]),
            ).fetchall()
            events = [
                json.loads(e[0])
                for e in db.execute(
                    "SELECT data FROM skill_job_events WHERE task_id=? AND kind='step_returned'",
                    (row["task_id"],),
                )
            ]
            backend_rows = [
                dict(b)
                for b in db.execute(
                    "SELECT * FROM skill_backend_executions WHERE task_id=?", (row["task_id"],)
                )
            ]
        for candidate in saved:
            ref = json.loads(candidate[0])
            self.load(store, row, ref, kinds={"result"})
            if not any(
                e.get("operation") == "scat_report_read" and e.get("reference") == ref
                for e in events
            ):
                continue
            plan = row["snapshot"]["workflow"]
            expected = {("scat_trial_run", t["trial_id"]) for t in plan["trials"]} | {
                ("scat_materialize", k) for k in plan["proposal"]["materializations"]
            }
            if len(expected) != len(backend_rows):
                continue
            actual = set()
            for execution in backend_rows:
                if (
                    execution["state"] != "succeeded"
                    or digest(json.loads(execution["request"])) != execution["request_sha"]
                    or digest(json.loads(execution["output"])) != execution["output_sha"]
                ):
                    return None
                request = json.loads(execution["request"])
                actual.add((request["operation"], request["unit"]))
            if actual != expected:
                continue
            visited, linked = set(), set()

            def verify(reference, visited=visited, linked=linked):
                if reference["artifact_id"] in visited:
                    return
                visited.add(reference["artifact_id"])
                nested = self.load(store, row, reference)
                if nested["workflow_kind"] in {"trial", "materialization"}:
                    receipt = next(
                        (r for r in backend_rows if r["id"] == nested["backend_execution_id"]), None
                    )
                    if receipt is None or digest(nested["payload"]) != receipt["output_sha"]:
                        raise StepError("workflow_backend_lineage_mismatch")
                    linked.add(receipt["id"])
                for parent in nested["parents"]:
                    verify(parent)

            verify(ref)
            if linked != {r["id"] for r in backend_rows}:
                continue
            return ref
        return None

    def outcome(self, row, reference):
        body = self.load(self.artifacts(row), row, reference, kinds={"result"})
        return body["payload"]["outcome"]
