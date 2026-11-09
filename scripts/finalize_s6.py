"""Freeze accepted backend evidence and rollback baseline; never publish candidate skills."""

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main():
    from application.skill_jobs import SOURCES
    from hermes_runtime.skills import SkillRelease, checked_path

    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("native", "backend", "cutover", "authoring", "audit", "maintenance", "readonly"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    reports = {
        name: json.loads(checked_path(ROOT, value).read_text())
        for name, value in vars(args).items()
    }
    for name in ("native", "backend", "cutover", "audit", "maintenance", "readonly"):
        assert reports[name]["status"] == "passed", name + "_gate_failed"
    native = reports["native"]
    assert native["summary"]["status"] == "passed" and len(native["summary"]["domains"]) == 11
    assert all(r["gate_passed"] and r["total"] >= 3 for r in native["summary"]["domains"].values())
    assert (
        native["temporary_home_removed"]
        and native["isolated_collections_removed"]
        and native["owned_processes_remaining"] == 0
    )
    coverage = json.loads((ROOT / "evaluations/skills-coverage-s6-v1.json").read_text())
    baseline = json.loads((ROOT / "evaluations/hermes_h2_v1.json").read_text())
    assert len(baseline["cases"]) >= 20 and len(coverage["rows"]) == 11
    release = SkillRelease(ROOT, ROOT / "services/hermes/skills.lock.json")
    assert native["bundle_sha256"] == release.bundle_sha256
    assert reports["authoring"]["published"] is False
    sources = {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in SOURCES}
    record = {
        "schema_version": 1,
        "status": "passed",
        "stage": "S6",
        "scope": "bounded CPU backend only",
        "frontend_changed": False,
        "production_gpu": False,
        "new_dependencies": False,
        "reports": {
            name: {"path": value, "sha256": hashlib.sha256((ROOT / value).read_bytes()).hexdigest()}
            for name, value in vars(args).items()
        },
        "real_model": native["model"],
        "real_model_summary": native["summary"],
        "legacy_baseline_questions": len(baseline["cases"]),
        "legacy_real_data_rerun": False,
        "legacy_scope": "historical H2 corpus/scorer regression retained; no internal data sent to a model",
        "candidate_comparison": {
            "scores": reports["authoring"]["scores"],
            "published": False,
            "interpretation": "Two published-group responses could not be parsed; no superiority or runtime publication claim.",
        },
        "first_invalid_validator_run_retained": "docs/validation/20261023-S6-native-72b2b16c75.json",
        "source_sha256": sources,
    }
    target = ROOT / "docs/validation/20261023-S6-acceptance.json"
    with target.open("x") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    release_record = {
        "schema_version": 1,
        "release": "s6-backend-v1",
        "skill_bundle_version": release.manifest["bundle_version"],
        "skill_bundle_sha256": release.bundle_sha256,
        "skill_files": {p: hashlib.sha256(data).hexdigest() for p, data in release.files.items()},
        "source_sha256": sources,
        "acceptance_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "environment_aliases": {"application": "scat-agent", "hermes": "scat-hermes"},
        "contracts": {
            p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest()
            for p in [
                "services/hermes/model-operations.v1.json",
                "services/hermes/workflow-operations.v1.json",
                "services/hermes/legacy-tool-mapping.v1.json",
                "config/skills-policy.yaml",
            ]
        },
        "new_request_defaults": {
            "HERMES_SKILL_JOBS_ENABLED": True,
            "HERMES_WORKFLOWS_ENABLED": True,
        },
        "rollback": {
            "native_args": ["--legacy-h5", "--with-mcp", "--with-model-tasks"],
            "platform_environment": {
                "HERMES_SKILL_JOBS_ENABLED": False,
                "HERMES_WORKFLOWS_ENABLED": False,
            },
            "procedure": "Stop new admission, reconcile in-flight/unknown effects, use a distinct legacy home; never let a legacy worker claim a Skills snapshot. Historical read APIs stay available.",
        },
        "candidate_published": False,
    }
    folder = ROOT / "services/hermes/releases"
    folder.mkdir(exist_ok=True)
    with (folder / "s6-backend-v1.json").open("x") as stream:
        stream.write(json.dumps(release_record, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "status": "passed",
                "domains": len(native["summary"]["domains"]),
                "independent_attempts": native["summary"]["independent_attempts"],
                "candidate_published": False,
            }
        )
    )


if __name__ == "__main__":
    if not (Path(sys.prefix) / "conda-meta").is_dir():
        raise RuntimeError("Existing Anaconda Python required")
    main()
