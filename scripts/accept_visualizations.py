"""Bounded P6 acceptance. Default prints the frozen plan; --live uses official APIs."""

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


async def run(args, plan):
    from application.context import ExecutionContext
    from application.visualization_tasks import VisualizationTasks
    from hermes_runtime.results import ToolResultStore
    from visualization_runtime.acceptance import (
        RemoteSandbox,
        SyntheticArtifacts,
        defect_source,
        prior_campaign_usage,
        synthetic,
    )
    from visualization_runtime.launcher import RenderLauncher
    from visualization_runtime.ledger import ExecutionLedger
    from visualization_runtime.paths import confined_path
    from visualization_runtime.providers import Completion
    from visualization_runtime.release_acceptance import candidate_stamp
    from visualization_runtime.sandbox import DockerSandbox
    from visualization_runtime.settings import load_settings
    from visualization_runtime.snapshots import SnapshotStore, digest
    from visualization_runtime.store import VisualizationJobStore
    from visualization_runtime.worker import VisualizationWorker

    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", args.run_id):
        raise ValueError("invalid_acceptance_run_id")
    candidate = candidate_stamp(ROOT)
    if candidate["dirty"]:
        raise ValueError("acceptance_candidate_source_dirty")
    pinned = json.loads(
        (ROOT / "services/visualization-renderer/runtime.lock.json").read_text(encoding="utf-8")
    )["validated_renderer_image_id"]
    if args.image != pinned:
        raise ValueError("acceptance_runtime_image_not_validated")
    output = ROOT / ".tmp/visualization-p6" / args.run_id
    output.mkdir(parents=True, exist_ok=args.resume)
    if (
        args.resume
        and json.loads((output / "frozen-plan.json").read_text(encoding="utf-8")) != plan
    ):
        raise ValueError("acceptance_frozen_plan_changed")
    settings = load_settings(ROOT).model_copy(
        update={
            "enabled": True,
            "sandbox_image": args.image,
            "max_revisions": 2,
            "max_provider_requests": 10,
            "max_total_tokens": 60000,
            "daily_provider_requests": 80,
            "daily_tokens": 400000,
        }
    )
    settings.provider("code")
    settings.provider("review")
    jobs = VisualizationJobStore(
        output / "tasks.sqlite", workspace_root=ROOT, limits=settings.execution_limits()
    )
    snapshots = SnapshotStore(jobs, output / "artifacts", workspace_root=ROOT)
    receipts = ToolResultStore(output / "receipts.sqlite", workspace_root=ROOT)
    sandbox = (
        RemoteSandbox(args.image, args.host, args.remote_root, args.remote_python)
        if args.host
        else DockerSandbox(ROOT, args.image)
    )
    fixtures = SyntheticArtifacts()
    app = VisualizationTasks(jobs, snapshots, receipts, artifacts=fixtures)
    launcher = RenderLauncher(jobs, snapshots, sandbox)
    worker = VisualizationWorker(app, launcher, settings)
    prior_usage = prior_campaign_usage(ROOT, args.prior_run, plan, current_run=args.run_id)
    original_reserve = worker.ledger.reserve_call

    def bounded_reserve(*call_args, **call_kwargs):
        from visualization_runtime.ledger import BudgetExceeded

        with jobs.transaction() as db:
            usage = db.execute(
                "SELECT count(*),COALESCE(sum(charged_tokens),0) FROM visualization_provider_calls WHERE call_key!='fixture-seed' AND state!='aborted'"
            ).fetchone()
        if (
            prior_usage[0] + usage[0] >= 80
            or prior_usage[1] + usage[1] + call_kwargs["tokens"] > 400000
        ):
            raise BudgetExceeded("acceptance_combined_budget_exceeded")
        return original_reserve(*call_args, **call_kwargs)

    worker.ledger.reserve_call = bounded_reserve
    context = ExecutionContext("p6-acceptance", args.run_id)
    track = (
        json.loads(confined_path(ROOT, Path(args.track)).read_text(encoding="utf-8"))
        if args.track
        else None
    )
    if track is not None and (
        len(track["data"]["points"]) != 100
        or track["query"]["external_id"] != "104363"
        or track["data"]["pagination"]["total_points"] != 683
        or track["data"]["pagination"]["has_more"] is not True
    ):
        raise ValueError("acceptance_authorized_track_scope_changed")
    report = {
        "plan_sha256": digest(plan),
        "runtime_image": args.image,
        "cases": [],
        "synthetic_uav_is_not_business_uav": True,
        "production_enabled": False,
        "candidate": candidate,
        "prior_run": args.prior_run,
        "prior_usage": list(prior_usage),
    }
    if args.resume and (output / "report.json").is_file():
        report = json.loads((output / "report.json").read_text(encoding="utf-8"))
        if report.get("candidate") != candidate:
            raise ValueError("acceptance_candidate_changed")
        if report.get("prior_run") != args.prior_run:
            raise ValueError("acceptance_budget_parent_changed")
    (output / "frozen-plan.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    for case in plan["cases"]:
        if any(row["id"] == case["id"] for row in report["cases"]):
            continue
        if args.case and case["id"] not in args.case:
            continue
        if case["source"] == "scat" and track is None:
            report["cases"].append(
                {
                    "id": case["id"],
                    "passed": False,
                    "state": "not_run",
                    "reason": "authorized_track_required",
                }
            )
            continue
        payload = track if case["source"] == "scat" else synthetic(case["source"] == "empty")
        if case["source"] == "scat":
            ref = receipts.put(
                context.scope_key, "run_p6", case["id"], "get_scat_track_points", payload
            )
            references = [
                {
                    "kind": "readonly_result",
                    "source_id": ref["result_id"],
                    "run_id": "run_p6",
                    "payload_sha256": ref["payload_sha256"],
                }
            ]
        else:
            references = [fixtures.add(context, case["id"], payload)]
        preferences = case.get("preferences", {})
        original_limits = jobs.limits
        if case.get("expected_first") == "not_pass":
            # This probe tests first-review interception, not repair convergence.
            jobs.limits = original_limits.model_copy(update={"max_revisions": 0})
        try:
            job = await app.prepare(
                context,
                case["id"],
                intent=case["intent"],
                references=references,
                preferences_sha256=digest(preferences),
                preferences=preferences,
            )
        finally:
            jobs.limits = original_limits
        job = jobs.get(context, job["task_id"])
        if job["state"] == "needs_reconciliation":
            ExecutionLedger(jobs).reconcile(
                job["task_id"], job["generation"], verify_stopped=sandbox.stopped
            )
        if case.get("defect"):
            claimed = jobs.claim("p6-worker")
            assert claimed["task_id"] == job["task_id"]
            ledger = ExecutionLedger(jobs)
            call = ledger.reserve_call(
                job["task_id"],
                "p6-worker",
                claimed["generation"],
                call_key="fixture-seed",
                role="code",
                fingerprint=digest(case["defect"]),
                tokens=1,
            )
            ledger.start_call(job["task_id"], "p6-worker", claimed["generation"], call["call_id"])
            ledger.finish_call(
                job["task_id"],
                "p6-worker",
                claimed["generation"],
                call["call_id"],
                completion=Completion(
                    defect_source(case["defect"]),
                    True,
                    {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                    "fixture:seed",
                ),
            )
            ledger.save_attempt(
                job["task_id"],
                "p6-worker",
                claimed["generation"],
                step_key="candidate-0",
                code_call_id=call["call_id"],
                source=defect_source(case["defect"]),
                activate=True,
            )
            # Resume through the normal worker cleanup/error path; no extra paid generation for the seed.
            original_claim = jobs.claim
            jobs.claim = lambda *a, captured=claimed, **k: captured
            try:
                await worker.run_once("p6-worker")
            finally:
                jobs.claim = original_claim
        else:
            await worker.run_once("p6-worker")
        current = jobs.get(context, job["task_id"])
        with jobs.transaction() as db:
            reviews = [
                json.loads(r[0])["decision"]
                for r in db.execute(
                    "SELECT v.report FROM visualization_reviews v JOIN visualization_renders r USING(render_id) WHERE r.task_id=? ORDER BY v.created",
                    (job["task_id"],),
                )
            ]
            calls = [
                dict(r)
                for r in db.execute(
                    "SELECT role,state,error_code,charged_tokens,json_extract(result,'$.request_id') AS provider_request_id FROM visualization_provider_calls WHERE task_id=?",
                    (job["task_id"],),
                )
            ]
            artifact = db.execute(
                "SELECT artifact_id FROM visualization_publications WHERE task_id=?",
                (job["task_id"],),
            ).fetchone()
        passed = (
            (bool(reviews) and reviews[0] != "pass")
            if case.get("expected_first") == "not_pass"
            else current["state"] == "succeeded"
        )
        row = {
            "id": case["id"],
            "passed": passed,
            "task_id": job["task_id"],
            "state": current["state"],
            "error_code": current["error_code"],
            "review_decisions": reviews,
            "artifact_id": artifact[0] if artifact else None,
            "calls": calls,
        }
        report["cases"].append(row)
        report["passed"] = len(report["cases"]) == len(plan["cases"]) and all(
            c["passed"] for c in report["cases"]
        )
        (output / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            json.dumps(
                {k: row[k] for k in ("id", "passed", "state", "error_code", "review_decisions")},
                ensure_ascii=False,
            ),
            flush=True,
        )
    print("Evidence:", output, flush=True)
    return 0 if report.get("passed") else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="reuse the same frozen plan, state and saved calls; unknown calls never replay",
    )
    parser.add_argument("--run-id", default="p6")
    parser.add_argument("--track", help="project-local authorized SCAT first-100-point receipt")
    parser.add_argument("--image")
    parser.add_argument("--host")
    parser.add_argument("--remote-root")
    parser.add_argument("--remote-python", default="/home/h3c/anaconda3/envs/scat-agent/bin/python")
    parser.add_argument("--case", action="append")
    parser.add_argument(
        "--prior-run",
        help="charge a completed baseline against the same 80-request/400000-token acceptance envelope",
    )
    args = parser.parse_args()
    plan = json.loads(
        (ROOT / "config/visualization-acceptance-v1.json").read_text(encoding="utf-8")
    )
    if not args.live:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    if not args.image:
        parser.error("--image is required for live acceptance")
    return asyncio.run(run(args, plan))


if __name__ == "__main__":
    raise SystemExit(main())
