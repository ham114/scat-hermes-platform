import json
from pathlib import Path

from hermes_runtime.stability import summarize
from workflow_runtime.contracts import WorkflowProposal

ROOT = Path(__file__).resolve().parents[2]


def test_frozen_holdout_covers_eleven_domains_with_valid_proposals():
    suite = json.loads((ROOT / "evaluations/hermes_s6_holdout_v1.json").read_text(encoding="utf-8"))
    assert len({d for c in suite["cases"] for d in c["domains"]}) == 11
    assert suite["min_repetitions"] == 3 and suite["max_repetitions"] == 10
    for case in suite["cases"]:
        if "proposal" in case:
            WorkflowProposal.model_validate(case["proposal"])


def test_failed_attempts_cannot_be_hidden_by_shared_domain_totals():
    rows = [
        {
            "domains": [str(d) for d in range(11)],
            "passed": i != 0,
            "selection_parameters_passed": True,
            "elapsed_seconds": i + 1,
        }
        for i in range(3)
    ]
    result = summarize(rows)
    assert result["status"] == "failed" and result["independent_attempts"] == 3
    assert result["domains"]["0"]["passed"] == 2
    assert result["domains"]["0"]["p95_seconds"] == 3
    assert result["domains"]["0"]["small_sample_no_sla"]


def test_selection_and_task_completion_are_separate_gates():
    rows = [
        {
            "domains": [str(d) for d in range(11)],
            "passed": True,
            "selection_parameters_passed": i != 0,
            "elapsed_seconds": 1,
        }
        for i in range(3)
    ]
    assert summarize(rows)["status"] == "failed"
    rows[0]["selection_parameters_passed"] = True
    assert summarize(rows)["status"] == "passed"
