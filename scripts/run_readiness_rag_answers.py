"""Four frozen new RAG questions, real production answer code, shared finite QA budget."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261108-release-readiness"
sys.path.insert(0, str(ROOT / "src"))
from hermes_runtime.skill_budget import RunBudget
from hermes_runtime.skill_policy import Budget


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def main():
    os.umask(0o077)
    os.environ["SCAT_VALIDATION_CAMPAIGN_FILE"] = "var/deploy/validation-campaign/active.json"
    target = OUT / "rag-answers-new.json"
    assert not target.exists(), "already_admitted_no_replay"
    questions = json.loads((OUT / "rag-retrieval-new.json").read_text())["cases"]
    threads = json.loads((OUT / "private/native-campaign-threads.json").read_text())
    base = ROOT / "var/deploy/h7b/rag-operations/readiness-answers"
    base.mkdir(exist_ok=False)
    config = json.loads((ROOT / "services/rag/real-agent-production-v1.json").read_text())
    report = {
        "status": "running",
        "kind": "direct_production_rag_not_web_chat",
        "human_approval": "pending",
        "cases": [],
    }
    save(target, report)
    budget_path = ROOT / "var/deploy/h7b/shared/state/readiness-rag-budget.sqlite"
    for index, question in enumerate(questions):
        scope = next(t["scope"] for t in threads if t["user"] == "ZYJ" and t["index"] == index + 2)
        folder = base / question["id"]
        folder.mkdir()
        container = ROOT / "var/rag-operations/readiness-answers" / question["id"]
        local_config = {**config, "scope": scope}
        save(folder / "config.json", local_config)
        save(folder / "request.json", {"question": question["question"], "mode": "auto"})
        run = "run_readiness_rag_" + uuid4().hex
        limits = Budget(max_turns=12, max_llm_tokens=750000, max_tool_calls=40, wall_seconds=360)
        sha = hashlib.sha256(json.dumps(local_config, sort_keys=True).encode()).hexdigest()
        budget = RunBudget(budget_path, scope, run, limits, sha)
        save(
            folder / "parent-budget.json",
            {
                "path": str(budget_path),
                "scope": scope,
                "run_id": run,
                "limits": limits.model_dump(),
                "policy_sha256": sha,
            },
        )
        entry = {
            "id": question["id"],
            "question": question["question"],
            "scope": scope,
            "parent_run": run,
            "state": "admission_recorded",
        }
        report["cases"].append(entry)
        save(target, report)
        output = ROOT / "var/validation/readiness-rag-answers" / question["id"]
        host_output = (
            ROOT / "var/deploy/h7b/runtime-validation/readiness-rag-answers" / question["id"]
        )
        command = [
            "docker",
            "exec",
            "scat-hermes-h7b-hermes-main-1",
            "/opt/conda/envs/scat-hermes/bin/python",
            "-B",
            "scripts/run_real_agent_rag.py",
            "--config",
            str(container / "config.json"),
            "--request",
            str(container / "request.json"),
            "--shared-budget",
            str(container / "parent-budget.json"),
            "--output",
            str(output),
        ]
        try:
            p = subprocess.run(command, capture_output=True, text=True, check=False, timeout=410)
            (folder / "process.log").write_text(p.stdout + p.stderr)
            if (host_output / "result.json").exists():
                value = json.loads((host_output / "result.json").read_text())
                save(OUT / "private" / ("rag-answer-" + question["id"] + ".json"), value)
                result = value.get("result") or {}
                entry.update(
                    state=value.get("status", "failed"),
                    answer_status=result.get("status"),
                    checks=value.get("checks", {}),
                    answer_sha256=hashlib.sha256(result.get("answer", "").encode()).hexdigest(),
                    claim_count=len(result.get("claims", [])),
                    parent_budget=budget.snapshot(),
                )
            else:
                entry.update(
                    state="unknown_no_replay",
                    exit_code=p.returncode,
                    parent_budget=budget.snapshot(),
                )
        finally:
            budget.finish()
        save(target, report)
        print(
            json.dumps(
                {
                    "id": entry["id"],
                    "state": entry["state"],
                    "answer_status": entry.get("answer_status"),
                    "budget": entry.get("parent_budget"),
                }
            ),
            flush=True,
        )
        if entry["state"] != "passed":
            report["status"] = "stopped_failure_preserved"
            save(target, report)
            return
    report["status"] = "execution_passed_human_quality_pending"
    save(target, report)


if __name__ == "__main__":
    main()
