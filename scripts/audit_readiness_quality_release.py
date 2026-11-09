"""Inventory release licenses and real cutoff evidence; never infer human/scientific approval."""
import hashlib
import json
import os
import sqlite3
import sys
import zipfile
from datetime import timedelta
from email.parser import BytesParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261108-release-readiness"
sys.path.insert(0, str(ROOT / "src"))
from application.task_store import digest
from workflow_runtime.contracts import WorkflowProposal
from workflow_runtime.windows import prediction_windows


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    os.umask(0o077)
    target = OUT / "quality-release-review.json"
    assert not target.exists()
    context = ROOT / "var/deploy/gpu/conda-migration/context"
    lock = json.loads((context / "wheels.lock.json").read_text())
    packages = []
    for row in lock["wheels"]:
        with zipfile.ZipFile(context / "wheels" / row["file"]) as archive:
            roots = [n for n in archive.namelist() if n.count("/") == 1 and n.endswith(".dist-info/METADATA")]
            assert len(roots) == 1
            metadata = BytesParser().parsebytes(archive.read(roots[0]))
            terms = [n for n in archive.namelist() if any(word in Path(n).name.upper() for word in ["LICENSE", "LICENCE", "COPYING", "NOTICE"]) and not n.endswith("/")]
            packages.append({"name": row["name"], "version": row["version"], "wheel_sha256": row["sha256"], "license_expression": metadata.get("License-Expression"), "declared_license": (metadata.get("License") or "")[:1000], "license_classifiers": [v for v in metadata.get_all("Classifier", []) if v.startswith("License")], "declared_project_urls": metadata.get_all("Project-URL", []), "bundled_terms": [{"path": n, "sha256": hashlib.sha256(archive.read(n)).hexdigest()} for n in terms], "approval": "pending_human_distribution_review"})
    experiment = json.loads((OUT / "natural-experiment.json").read_text())
    assert experiment["status"] == "passed"
    with sqlite3.connect("file:" + str(ROOT / "var/deploy/h7b/shared/state/tasks.sqlite") + "?mode=ro", uri=True) as db:
        snapshot, frozen_sha = None, None
        row = db.execute("SELECT snapshot,sha256 FROM model_tasks WHERE task_id=?", (experiment["task_id"],)).fetchone()
        snapshot = json.loads(row[0])
        assert digest(snapshot) == row[1]
    import subprocess
    info = json.loads(subprocess.check_output(["docker", "inspect", "scat-hermes-h7b-platform-1"]))[0]
    inputs = Path(next(m["Source"] for m in info["Mounts"] if m["Destination"] == str(ROOT / "var/workflow-inputs")))
    frozen_sha = snapshot["workflow"]["input_sha256"]
    frozen = json.loads((inputs / (frozen_sha + ".json")).read_text())
    assert digest(frozen) == frozen_sha and frozen["synthetic"] is False
    proposal = WorkflowProposal.model_validate(snapshot["workflow"]["proposal"])
    windows = prediction_windows(frozen["flights"][0], proposal)
    assert len(windows) == 1
    observed, targets = windows[0]
    checks = {"exactly_200_observations": len(observed) == 200, "off_grid_cutoff": observed[-1].time < proposal.as_of, "no_future_observation_input": all(p.time <= proposal.as_of for p in observed), "requested_cutoff_horizon_anchor": targets == tuple(proposal.as_of + timedelta(seconds=h) for h in proposal.horizons_s), "real_frozen_source": frozen["synthetic"] is False, "native_job_report_succeeded": experiment["job_state"] == "succeeded", "repeated_artifact_hash_stable": all(a["reopen_identical"] for a in experiment["artifacts"])}
    assert all(checks.values())
    artifact_files = json.loads((context / "artifacts.lock.json").read_text())
    training_manifests = [p for p in artifact_files if any(s in p.lower() for s in ["training_ids", "train_split", "split_manifest", "training_membership"])]
    report = {"status": "audit_material_prepared_not_human_approved", "off_grid_asof": {"checks": checks, "requested_as_of": proposal.as_of.isoformat(), "last_observation_time": observed[-1].time.isoformat(), "lag_seconds": (proposal.as_of - observed[-1].time).total_seconds(), "target_times": [t.isoformat() for t in targets], "input_sha256": frozen_sha}, "migration_fidelity": json.loads((OUT / "gpu-conda-acceptance.json").read_text())["calls"], "scientific_quality": {"new_holdout_generalization_claim": False, "single_flight_acceptance_is_accuracy_benchmark": False, "training_membership_manifests_in_approved_artifacts": training_manifests, "training_overlap": "unknown_without_training_membership_manifest", "classification_semantics": "class indices only; no validated flight-phase or anomaly mapping", "human_model_review": "pending"}, "distribution": {"human_final_approval": "pending", "project_license_sha256": sha(ROOT / "LICENSE"), "notice_sha256": sha(ROOT / "NOTICE.md"), "conda_lock_sha256": sha(context / "conda.lock.json"), "wheel_lock_sha256": sha(context / "wheels.lock.json"), "internally_reconstructed_wheels": True, "vendor_original_wheel_claim": False, "review_requires": ["dependency and bundled third-party terms", "NVIDIA toolkit/driver user-space redistribution terms", "GBAR checkpoint/config/source permission", "SCAT dataset and knowledge corpus permission", "deployment identity and credential handling"], "packages": packages}, "rag": {"new_answer_cases": 4, "independent_human_review": "pending", "raw_evidence": "private/rag-answer-*.json", "protocol_success_is_semantic_approval": False}}
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "off_grid_checks": checks, "packaged_distributions_inventoried": len(packages), "training_overlap": report["scientific_quality"]["training_overlap"], "human_approval": "pending"}))


if __name__ == "__main__":
    main()
