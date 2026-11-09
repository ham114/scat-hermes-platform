"""Read-only final deployment, frozen inputs, environment and credential audit."""

from __future__ import annotations

import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GEN = ROOT.parent / "ragas-evaluation"
OUT = ROOT / "var/validation/20261026-rag-live"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def main():
    if sys.executable != "/home/h3c/anaconda3/envs/scat-agent/bin/python":
        raise ValueError("use_existing_scat_agent")
    sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]
    from audit_rag_06b import open_port, sha
    from dotenv import dotenv_values
    from manage_rag_production import health, status

    from hermes_runtime.native_budget import verify_budget_source
    from hermes_runtime.native_profiles import verify_native

    verify_native(ROOT)
    verify_budget_source(ROOT, ROOT / "vendor/hermes-agent")
    history = read(ROOT / "var/validation/20261026-rag-06b/preflight.json")["history"]
    histories = {
        name: sha(Path("/home/h3c/anaconda3/envs") / name / "conda-meta/history")
        for name in history
    }
    old = read(ROOT / "services/rag/qwen3-06b-dev40-v1.json")
    config = read(ROOT / "services/rag/qwen3-production-v1.json")
    manifest = read(ROOT / "services/rag/qwen3-06b-download.json")
    models_ok = all(
        (Path(manifest["destination"]) / row["path"]).stat().st_size == row["size"]
        and sha(Path(manifest["destination"]) / row["path"]) == row["sha256"]
        for row in manifest["files"]
    )
    generation = GEN / "reports/batch-20261026-live40"
    generated = read(generation / "summary.json")
    started = read(generation / "started.json")
    prereg = read(OUT / "test40/preregistration.json")
    final = read(ROOT / "docs/validation/20261026-rag-live-test40-final.json")
    proof = read(ROOT / "docs/validation/20261026-rag-live-test40-verification.json")
    rollback = read(ROOT / "docs/validation/20261026-rag-live-rollback.json")
    suites = list(ET.parse(OUT / "full-tests.xml").getroot().iter("testsuite"))
    tests = {
        k: sum(int(s.get(k, 0)) for s in suites) for k in ("tests", "failures", "errors", "skipped")
    }
    deployment = status()
    ports = {str(p): open_port(p) for p in (80, 443, 8080, 8501, 8091, 27017, 18081, 18133, 18134)}
    linger = subprocess.run(
        ["loginctl", "show-user", "h3c", "-p", "Linger", "--value"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    secrets = set()
    envs = [ROOT / ".env", GEN / ".env", ROOT / "var/hermes/rag-production/.env"]
    envs.extend((ROOT / "var/hermes/managed/profiles").glob("*/.env"))
    for path in envs:
        if path.is_file():
            for name, value in dotenv_values(path).items():
                if (
                    value
                    and len(value) >= 12
                    and any(term in name.upper() for term in ("KEY", "TOKEN", "PASSWORD", "SECRET"))
                ):
                    secrets.add(value.encode())
    secrets.add((ROOT / "var/secrets/hermes-api.key").read_bytes().strip())
    files = set()
    for folder in [
        OUT,
        generation,
        ROOT / "src/rag_retrieval",
        ROOT / "scripts",
        ROOT / "services/rag",
        ROOT / "docs",
        GEN / "scripts",
        GEN / "config",
        GEN / "logs",
    ]:
        files.update(
            p
            for p in folder.rglob("*")
            if p.is_file()
            and p.suffix in {".py", ".json", ".jsonl", ".md", ".log", ".xml", ".txt", ".yaml"}
            and p.name != ".env"
        )
    files.update([ROOT / "SCAT_HERMES_PLATFORM_DEVELOPMENT_AND_TEST_PLAN.md", GEN / "README.md"])
    exposures = [str(p) for p in sorted(files) if any(v in p.read_bytes() for v in secrets)]
    scratch = {
        str(base / ".tmp/rag-live"): not (base / ".tmp/rag-live").exists() for base in (ROOT, GEN)
    }
    checks = {
        "native_and_budget_pins": True,
        "original_conda_histories_unchanged": histories == history,
        "original_frozen_corpus_vectors_labels_unchanged": all(
            sha(Path(v["path"])) == v["sha256"] for v in old["inputs"].values()
        ),
        "original_retriever_sources_unchanged": all(
            sha(ROOT / p) == digest for p, digest in old["retriever_sources"].items()
        ),
        "eight_model_files_verified": models_ok and len(manifest["files"]) == 8,
        "production_corpus_and_vectors_pinned": sha(Path(config["corpus"]))
        == config["corpus_sha256"]
        and sha(Path(config["embedding_cache"])) == config["embedding_cache_sha256"]
        and sha(ROOT / config["model_manifest"]) == config["model_manifest_sha256"],
        "evaluation_sources_still_frozen": all(
            sha(ROOT / p) == digest for p, digest in prereg["sources"].items()
        ),
        "original_ragas_workbench_unchanged": sha(GEN / "scripts/ragas_workbench.py")
        == started["workbench_sha256"],
        "native_forty_complete_and_frozen": generated["status"] == "completed"
        and generated["count"] == 40
        and sha(generation / "candidates.jsonl") == generated["candidates_sha256"],
        "all_generation_attempts_within_caps": generated["requests"] <= 600
        and generated["total_tokens"] <= 900000,
        "all_qa_attempts_within_caps": final["total_model_requests"] <= 500
        and final["total_conservative_tokens"] <= 20000000,
        "independent_evidence_verification": proof["status"] == "passed",
        "rollback_and_persistent_recovery": rollback["status"] == "passed",
        "full_backend_tests": tests["failures"] == tests["errors"] == 0
        and tests["tests"] - tests["skipped"] == 1263,
        "units_active_enabled_and_ready": deployment["rag_ready"]
        and bool(deployment["hermes_health"])
        and all(
            x == {"active": "active", "enabled": "enabled"} for x in deployment["units"].values()
        ),
        "user_linger_enabled": linger == "yes",
        "original_six_ports_and_old_rag_healthy": all(
            ports[str(p)] for p in (80, 443, 8080, 8501, 8091, 27017)
        )
        and bool(health(8091)),
        "new_only_backend_ports_ready_test_port_closed": ports["18081"]
        and ports["18133"]
        and not ports["18134"],
        "credential_actual_value_scan_clean": not exposures,
        "private_key_permissions": all(
            (p.stat().st_mode & 0o777) == 0o600
            for p in [ROOT / ".env", GEN / ".env", ROOT / "var/secrets/hermes-api.key"]
        ),
        "task_scratch_removed": all(scratch.values()),
    }
    report = {
        "status": "passed" if all(checks.values()) else "incomplete",
        "checks": checks,
        "deployment": deployment,
        "ports": ports,
        "conda_histories": histories,
        "credential_scan": {"files": len(files), "exposure_files": exposures},
        "test_summary": tests,
        "scratch": scratch,
        "new_native_questions": 40,
        "human_approved_questions": 0,
        "all_campaign_model_requests": generated["requests"] + final["total_model_requests"],
        "remaining_content_gap": "LIVE-20261026-008: answer omitted requested SCAT_DATASET.md filename",
        "whole_platform_or_frontend_released": False,
        "independent_human_admission": False,
    }
    path = ROOT / "docs/validation/20261026-rag-live-final-audit.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report))
    if report["status"] != "passed":
        raise ValueError("final_audit_incomplete")


if __name__ == "__main__":
    main()
