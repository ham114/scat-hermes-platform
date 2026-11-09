"""Bounded server-only real SCAT read/numerical smoke test. No LLM, Mongo writes or GPU."""

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


async def main():
    from application.context import ExecutionContext
    from application.task_store import digest
    from scat.infrastructure.database import flight_database
    from scat.repositories.flight_repository import ScatFlightRepository
    from workflow_runtime.contracts import WorkflowProposal, trial_manifest
    from workflow_runtime.data import ScatSource, select_input
    from workflow_runtime.numerics import materialize, run_trial, summarize

    assert (Path(sys.prefix) / "conda-meta").is_dir()
    report = {
        "status": "running",
        "external_llm_requests": 0,
        "original_collections_written": False,
        "gpu": False,
    }
    try:
        await flight_database.connect()
        batch = "scat20161015_20161021"
        candidates = await ScatFlightRepository().search_flights(
            batch=batch, has_scat_prediction=True, min_track_points=100, limit=8
        )
        ids = [str(r["source"]["external_id"]) for r in candidates]
        plan = WorkflowProposal(
            kind="comparison",
            batch=batch,
            external_ids=ids,
            max_flights=1,
            models=[{"model": "constant-velocity-heading"}, {"model": "kalman-cv"}],
            materializations=["phase_segments"],
            missing_phase="build",
        )
        frozen = await select_input(
            ExecutionContext("scat-researcher", "s5-server-only-smoke"), plan, ScatSource()
        )
        outputs = [
            await asyncio.to_thread(run_trial, frozen, plan.model_dump(mode="json"), t)
            for t in trial_manifest(plan)
        ]
        assert all(o["status"] == "succeeded" for o in outputs)
        derived = []
        for kind in ("phase_segments", "flight_features", "prediction_metrics"):
            result = await asyncio.to_thread(
                materialize, frozen, kind, generated_at=datetime.now(UTC).isoformat()
            )
            assert result["status"] == "succeeded"
            derived.append(result)
        phases = {r["external_id"]: r["segments"] for r in derived[0]["records"]}
        evaluation = summarize(outputs, plan.model_dump(mode="json"), phases=phases)
        assert evaluation["common_sample_count"] > 0
        report.update(
            status="passed",
            selected_flights=len(frozen["flights"]),
            checked_candidates=frozen["coverage"]["checked_count"],
            input_sha256=digest(frozen),
            protocol=evaluation["protocol"],
            model_sample_counts=[len(o["samples"]) for o in outputs],
            common_sample_count=evaluation["common_sample_count"],
            ungrouped_samples=[t["ungrouped_samples"] for t in evaluation["trials"]],
            derived=[
                {"kind": r["kind"], "records": r["record_count"], "status": r["status"]}
                for r in derived
            ],
        )
    except BaseException as exc:
        report.update(status="failed", error_type=type(exc).__name__)
        raise
    finally:
        await flight_database.close()
        path = ROOT / "docs/validation/20261023-S5-real-data-smoke.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
