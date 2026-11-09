"""Read-only final integrity, usage, secrets and cleanup audit for the 0.6B campaign."""

from __future__ import annotations

import hashlib
import json
import socket
import sqlite3
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261026-rag-06b"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def open_port(port):
    with socket.socket() as connection:
        connection.settimeout(2)
        return connection.connect_ex(("127.0.0.1", port)) == 0


def ledger(path):
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        return [dict(r) for r in db.execute("SELECT * FROM skill_run_budgets")]


def main():
    if sys.executable != "/home/h3c/anaconda3/envs/scat-agent/bin/python":
        raise ValueError("use_existing_scat_agent")
    sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]
    from dotenv import dotenv_values
    from verify_real_agent_rag import verify_result

    from hermes_runtime.native_budget import verify_budget_source
    from hermes_runtime.native_profiles import verify_native
    from hermes_runtime.real_rag import load_corpus

    verify_native(ROOT)
    verify_budget_source(ROOT, ROOT / "vendor/hermes-agent")
    history = read(OUT / "preflight.json")["history"]
    histories = {
        name: sha(Path("/home/h3c/anaconda3/envs") / name / "conda-meta/history")
        for name in history
    }
    config = read(ROOT / "services/rag/qwen3-06b-dev40-v1.json")
    inputs_ok = all(sha(Path(v["path"])) == v["sha256"] for v in config["inputs"].values())
    retriever_ok = all(sha(ROOT / p) == digest for p, digest in config["retriever_sources"].items())
    model = read(ROOT / "services/rag/qwen3-06b-download.json")
    model_ok = all(
        (Path(model["destination"]) / row["path"]).stat().st_size == row["size"]
        and sha(Path(model["destination"]) / row["path"]) == row["sha256"]
        for row in model["files"]
    )
    budgets = {}
    batch_roots = sorted((ROOT / "var/validation").glob("20261026-rag06b-*"))
    for folder in batch_roots:
        candidates = [
            p
            for p in folder.glob("*.sqlite")
            if "skill_run_budgets"
            in {
                row[0]
                for row in sqlite3.connect(p).execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        ]
        if len(candidates) != 1:
            raise ValueError("ambiguous_batch_ledger:" + folder.name)
        budgets[folder.name] = ledger(candidates[0])
    budgets["negative-v3"] = ledger(OUT / "negatives/budget.sqlite")
    budgets["regressions-v4"] = ledger(OUT / "regressions-v4/budget.sqlite")
    for name in ("api-integration", "api-integration-v2"):
        budgets[name] = ledger(OUT / name / "home/skill-loads.sqlite")
    fields = (
        "llm_calls",
        "tool_calls",
        "charged_tokens",
        "provider_input_tokens",
        "provider_output_tokens",
        "provider_cached_tokens",
    )
    totals = {k: sum(row[k] for rows in budgets.values() for row in rows) for k in fields}
    negative_proof = []
    for name in ("injection", "conflict", "insufficient"):
        folder = OUT / "negatives" / name
        fixture_config = read(folder / "config.json")
        for mode in ("fast", "deep"):
            proof = verify_result(
                read(folder / mode / "result.json"), load_corpus(fixture_config), fixture_config
            )
            negative_proof.append({"case": name, "mode": mode, **proof})
    suites = list(ET.parse(OUT / "final-full-tests.xml").getroot().iter("testsuite"))
    tests = {
        k: sum(int(s.get(k, 0)) for s in suites) for k in ("tests", "failures", "errors", "skipped")
    }
    secrets = set()
    envs = [ROOT / ".env", *sorted((ROOT / "var/hermes/managed/profiles").glob("*/.env"))]
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
    scan = set()
    for folder in [OUT, *batch_roots, ROOT / "var/rag-operations"]:
        scan.update(
            p
            for p in folder.rglob("*")
            if p.is_file()
            and p.name != ".env"
            and p.suffix in {".py", ".json", ".jsonl", ".md", ".log", ".yaml", ".xml", ".txt"}
        )
    scan.update(
        p
        for folder in (ROOT / "src/hermes_runtime", ROOT / "scripts", ROOT / "services/rag")
        for p in folder.glob("*")
        if p.is_file() and p.suffix in {".py", ".json"}
    )
    scan.update((ROOT / "docs/validation").glob("20261026-*06b*.json"))
    scan.update(
        ROOT / name
        for name in (
            "docs/HERMES_RAG_06B_COMPLETION.md",
            "docs/DEVELOPMENT_LOG.md",
            "SCAT_HERMES_PLATFORM_DEVELOPMENT_AND_TEST_PLAN.md",
        )
    )
    exposures = [
        str(p.relative_to(ROOT))
        for p in sorted(scan)
        if any(secret and secret in p.read_bytes() for secret in secrets)
    ]
    ports = {
        str(p): open_port(p) for p in (80, 443, 8080, 8501, 8091, 27017, 18130, 18131, 18132, 18133)
    }
    cleanup = {p: not (ROOT / p).exists() for p in (".tmp/rag-06b", ".tmp/real-rag")}
    qa = read(ROOT / "docs/validation/20261026-rag-06b-qa.json")
    checks = {
        "native_and_budget_sources_pinned": True,
        "existing_conda_histories_unchanged": histories == history,
        "frozen_inputs_and_labels_unchanged": inputs_ok,
        "original_retriever_sources_unchanged": retriever_ok,
        "eight_model_files_verified": model_ok and len(model["files"]) == 8,
        "all_forty_pairs_recorded": qa["paired_cases"] == 40,
        "budget_within_request_and_corrected_conservative_ceiling": totals["llm_calls"] <= 1600
        and totals["charged_tokens"] <= 55000000,
        "guard_acceptance": read(ROOT / "docs/validation/20261026-rag-06b-guard.json")["status"]
        == "passed",
        "api_acceptance": read(ROOT / "docs/validation/20261026-rag-06b-api.json")["status"]
        == "passed",
        "restart_acceptance": read(ROOT / "docs/validation/20261026-rag-06b-recovery.json")[
            "status"
        ]
        == "passed",
        "targeted_v4_regressions": read(OUT / "regressions-v4/summary.json")["passed"],
        "synthetic_negative_provenance": all(x["execution_passed"] for x in negative_proof),
        "full_backend_tests": tests["failures"] == tests["errors"] == 0
        and tests["tests"] - tests["skipped"] == 1250,
        "actual_private_values_not_exposed": not exposures,
        "original_six_ports_listening": all(
            ports[str(p)] for p in (80, 443, 8080, 8501, 8091, 27017)
        ),
        "reserved_test_ports_closed": all(not ports[str(p)] for p in (18130, 18131, 18132, 18133)),
        "task_temp_removed": all(cleanup.values()),
    }
    report = {
        "status": "backend-development-completed-with-quality-limitations"
        if all(checks.values())
        else "audit_incomplete",
        "checks": checks,
        "model": {
            "repo": model["repo"],
            "revision": model["huggingface_revision"],
            "verified_files": len(model["files"]),
            "bytes": model["total_bytes"],
        },
        "all_phases_usage_including_failures_and_reviews": totals,
        "phase_usage": {
            name: {k: sum(r[k] for r in rows) for k in fields} for name, rows in budgets.items()
        },
        "usage_note": "provider_input includes cached tokens; conservative UTF-8 reservation is not billable-token usage or a price guarantee",
        "test_summary": tests,
        "ports": ports,
        "temporary_directories": cleanup,
        "credential_scan": {"files": len(scan), "exposure_files": exposures},
        "negative_provenance": negative_proof,
        "v3_negative_behavior_passes": 5,
        "v3_negative_runs": 6,
        "v4_injection_regression_passed": True,
        "v4_full_negative_matrix_rerun": False,
        "qa": qa["aggregate"],
        "source_reference_conflict": read(
            ROOT / "docs/validation/20261026-rag-06b-source-conflict.json"
        ),
        "user_retrieval_latency_accepted": True,
        "multi_agent_quality_advantage_demonstrated": False,
        "independent_production_admission": False,
        "default_service_or_skill_release_changed": False,
    }
    output = ROOT / "docs/validation/20261026-rag-06b-acceptance.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "status": report["status"],
                "checks": checks,
                "usage": totals,
                "tests": tests,
                "credential_scan_files": len(scan),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
