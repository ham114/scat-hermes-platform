"""Bounded real CUDA workflow verification. Raw data and artifacts stay on the server."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class SyntheticGPUSource:
    version, synthetic = "synthetic-gpu-dense-v1", True

    def points(self):
        start = datetime(2020, 1, 1, tzinfo=UTC)
        return [
            {
                "source_index": i,
                "timestamp": (start + timedelta(seconds=i * 5)).isoformat(),
                "lat": 60 + i * 0.001,
                "lon": 20 + i * 0.001,
                "measured_flight_level": 300.0,
                "vx": 100.0,
                "vy": 50.0,
                "rocd": 0.0,
            }
            for i in range(241)
        ]

    async def candidates(self, batch, limit):
        return ["SYNTH-GPU-001", "SYNTH-GPU-002"][:limit]

    async def get(self, batch, external_id):
        return {
            "plots": [
                {
                    "timestamp": p["timestamp"],
                    "lat": p["lat"],
                    "lon": p["lon"],
                    "altitude_m": 9144.0,
                }
                for p in self.points()
            ]
        }

    async def gbar_points(self, batch, external_id, cutoff):
        return [p for p in self.points() if datetime.fromisoformat(p["timestamp"]) <= cutoff][-500:]


async def deterministic_engine(app, row, worker, generation):
    from application.context import ExecutionContext

    jobs = app.skills.store
    child = ExecutionContext(row["snapshot"]["run"]["owner_user_id"], "gpu-child-" + uuid4().hex)
    delegation = jobs.delegate(
        row["scope"], row["task_id"], child.scope_key, row["snapshot"]["skills"]["operations"]
    )
    capability = jobs.bind(
        delegation, child.scope_key, "run_" + uuid4().hex, uuid4().hex, worker, generation
    )
    session = jobs.session(capability)

    async def call(name, args):
        return await app.skills.invoke(
            capability, name, args, scope=session["scope"], native_run=session["native_run"]
        )

    sample = await call("scat_sample_prepare", {})
    trials = []
    for identity in sample["plan"]["trial_ids"]:
        args = {"reference": sample["reference"], "trial_id": identity}
        result = await call("scat_trial_run", args)
        assert result["unit_outcome"] in {"succeeded", "partial"}
        again = await call("scat_trial_run", args)
        assert again["reference"] == result["reference"] and again["reused"]
        trials.append(result["reference"])
    evaluation = await call("scat_evaluation_summarize", {"references": trials})
    diagnosis = await call("scat_evaluation_diagnose", {"reference": evaluation["reference"]})
    report = await call(
        "scat_report_build", {"references": [evaluation["reference"], diagnosis["reference"]]}
    )
    return await call("scat_report_read", {"reference": report["reference"]})


async def probe(args):
    os.environ.update(
        ANALYSIS_IDENTITY_MODE="single_user",
        ANALYSIS_SINGLE_USER_ID="scat-researcher",
        SCAT_S5_WORKFLOWS="1",
    )
    from analysis_runs.artifact_repository import AnalysisArtifactRepository
    from analysis_runs.repository import AnalysisRunRepository
    from application.context import ExecutionContext
    from application.skill_job_factory import create_skill_tasks
    from application.skill_worker import BackgroundHermes, SkillWorker
    from application.task_store import TaskNotFound
    from scat.infrastructure.database import flight_database
    from workflow_runtime.contracts import WorkflowProposal
    from workflow_runtime.gpu import GPURequests, registration

    assert (Path(sys.prefix) / "conda-meta").is_dir()
    if not re.fullmatch("[a-z0-9-]{1,40}", args.case) or (args.real and args.native):
        raise ValueError("Native provider verification accepts synthetic input only")
    directory = ROOT / "var/validation/20261027-production-gpu" / args.case
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "started.json").open("x") as stream:
        stream.write(
            json.dumps(
                {
                    "case": args.case,
                    "real": args.real,
                    "native": args.native,
                    "single": args.single,
                    "max_gpu_requests": 2,
                    "max_llm_requests": 24 if args.native else 0,
                    "max_conservative_tokens": 1000000 if args.native else 0,
                }
            )
        )
    os.environ.setdefault("SCAT_GPU_CONFIG_FILE", "var/gpu/production.json")
    os.environ["SCAT_SKILL_RELEASE_STATE"] = (
        (directory / "catalog.sqlite").relative_to(ROOT).as_posix()
    )
    home = ROOT / ".tmp/production-gpu" / args.case
    home.mkdir(parents=True, exist_ok=True)
    os.environ.update(TMPDIR=str(home), TMP=str(home), TEMP=str(home), PYTHONDONTWRITEBYTECODE="1")
    config = registration(ROOT, required=True)
    ledger = GPURequests(ROOT, config)
    before = {r["id"] for r in ledger.records()}
    if any(r["state"] in {"running", "unknown"} for r in ledger.records()):
        raise RuntimeError("An existing GPU request requires reconciliation")
    identity = uuid4().hex[:12]
    collection_names = ["analysis_runs_gpu_" + identity, "analysis_artifacts_gpu_" + identity]
    os.environ.update(
        ANALYSIS_RUNS_COLLECTION=collection_names[0],
        ANALYSIS_ARTIFACTS_COLLECTION=collection_names[1],
    )
    report = {
        "status": "running",
        "case": args.case,
        "synthetic": not args.real,
        "driver": "native_hermes_worker" if args.native else "trusted_deterministic_worker",
        "gpu": config.pin(),
        "external_llm_requests": None if args.native else 0,
        "original_collections_written": False,
    }
    db = None
    app = None
    try:
        await flight_database.connect()
        db = flight_database.get_collection(collection_names[0]).database
        runs = AnalysisRunRepository(collection=db[collection_names[0]])
        artifacts = AnalysisArtifactRepository(collection=db[collection_names[1]])
        state = directory / "jobs.sqlite"
        app = create_skill_tasks(
            ROOT,
            state,
            runs=runs,
            artifacts=artifacts,
            workflows_enabled=True,
            workflow_synthetic=False,
        )
        if not args.real:
            app.workflow_source = SyntheticGPUSource()
        p = WorkflowProposal(
            kind="comparison",
            batch="scat20161015_20161021" if args.real else "SYNTH-GPU",
            selection="random",
            candidate_limit=16,
            max_flights=1 if args.single else 2,
            min_flights=1,
            models=[
                {"model": "scat-gbar-v23", "version": "23"},
            ]
            + ([] if args.single else [{"model": "constant-velocity-heading"}]),
            min_observed_s=350,
            max_cutoffs_per_flight=1 if args.single else 2,
            horizons_s=[10, 30, 60],
        )
        context = ExecutionContext("scat-researcher", "gpu-probe-" + identity)
        view = await app.prepare_workflow(
            context,
            p,
            proposal_key=identity,
            budget={
                "max_turns": 24,
                "max_llm_tokens": 1000000,
                "max_tool_calls": 48,
                "wall_seconds": 600,
            },
        )
        report.update(task_id=view["task_id"], stage="prepared")
        assert not [r for r in ledger.records() if r["id"] not in before]
        assert app.skills.store.claim("unconfirmed-probe") is None
        for _ in range(2):
            app.confirm(context, view["task_id"], view["snapshot_sha256"])
        engine = (
            BackgroundHermes(ROOT, home_base=home / "native", port=18137)
            if args.native
            else deterministic_engine
        )
        assert await SkillWorker(app, engine).once()
        final = await app.get(context, view["task_id"])
        assert final["skill_job"]["state"] in {"succeeded", "partial"}, final["skill_job"]["state"]
        executions = final["skill_job"]["backend_executions"]
        assert len(executions) == len(p.models) and all(
            e["state"] == "succeeded" and e["calls"] == 1 for e in executions
        )
        artifact_ids = final["run"]["result_artifact_ids"]
        saved = [await app.artifact(context, view["task_id"], x) for x in artifact_ids]
        assert len(saved) >= 2
        gpu_pages = [x for x in saved if x["analysis_result"]["context"].get("gpu_release")]
        assert len(gpu_pages) == 1
        for index, artifact in enumerate(saved):
            (directory / f"server-only-artifact-{index}.json").write_text(
                json.dumps(artifact, ensure_ascii=False, default=str), encoding="utf-8"
            )
        try:
            await app.artifact(
                ExecutionContext(context.user_id, "other-thread"), view["task_id"], artifact_ids[0]
            )
        except TaskNotFound:
            pass
        else:
            raise AssertionError("Cross-thread artifact access was not rejected")
        new_records = [r for r in ledger.records() if r["id"] not in before]
        assert len(new_records) <= 2 and all(
            r["state"] == "succeeded" and r["calls"] == 1 for r in new_records
        )
        # Reopen all stores and reread results without sending another GPU request.
        reopened = create_skill_tasks(
            ROOT,
            state,
            runs=AnalysisRunRepository(collection=db[collection_names[0]]),
            artifacts=AnalysisArtifactRepository(collection=db[collection_names[1]]),
            workflows_enabled=True,
        )
        reread = await reopened.artifact(context, view["task_id"], artifact_ids[0])
        assert reread["payload_sha256"] == saved[0]["payload_sha256"]
        assert [
            r for r in GPURequests(ROOT, config).records() if r["id"] not in before
        ] == new_records
        with closing(app.store.connect()) as state_db:
            outputs = [
                json.loads(r[0])
                for r in state_db.execute(
                    "SELECT output FROM skill_backend_executions WHERE task_id=?",
                    (view["task_id"],),
                )
            ]
        summary = {
            "status": "passed",
            "case": args.case,
            "synthetic": not args.real,
            "driver": report["driver"],
            "selected_flights": final["workflow"]["coverage"]["selected_count"],
            "single_model": args.single,
            "gpu_http_requests": len(new_records),
            "all_gpu_calls_one": True,
            "artifacts": len(saved),
            "gpu_graph_pages": len(gpu_pages),
            "model_sample_counts": [len(o.get("samples", [])) for o in outputs],
            "all_errors_finite": all(
                __import__("math").isfinite(s["error_m"])
                for o in outputs
                for s in o.get("samples", [])
            ),
            "cross_thread_rejected": True,
            "restart_read_without_recompute": True,
            "gpu": config.pin(),
            "external_llm_requests": final["skill_job"]["budget"]["llm_calls"],
            "conservative_tokens": final["skill_job"]["budget"]["charged_tokens"],
            "raw_data_exported": False,
            "standard_gpu_container_migration": False,
        }
        (ROOT / "docs/validation" / ("20261027-production-gpu-" + args.case + ".json")).write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        report.update(status="passed", summary=summary, stage="verified")
        print(json.dumps(summary, ensure_ascii=False))
    except BaseException as exc:
        report.update(status="failed", error_type=type(exc).__name__)
        raise
    finally:
        (directory / "result.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
        )
        # These two unique, explicitly owned collections contain only probe results.
        if db is not None and report["status"] == "passed":
            for name in collection_names:
                assert name.startswith(
                    ("analysis_runs_gpu_", "analysis_artifacts_gpu_")
                ) and name.endswith(identity)
                await db.drop_collection(name)
        await flight_database.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", required=True)
    parser.add_argument("--real", action="store_true")
    parser.add_argument("--native", action="store_true")
    parser.add_argument("--single", action="store_true")
    asyncio.run(probe(parser.parse_args()))
