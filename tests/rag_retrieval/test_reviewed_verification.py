from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "reviewed_verifier", Path(__file__).parents[2] / "scripts/verify_reviewed_rag_retrieval.py"
)
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)


def test_independent_scorer_requires_a_complete_bundle_for_each_claim():
    groups = [{"alternatives": [["a", "b"], ["c"]]}, {"alternatives": [["d"]]}]
    partial = verifier.metrics(groups, ["a", "d"])
    complete = verifier.metrics(groups, ["a", "b", "d"])
    alternative = verifier.metrics(groups, ["c", "d"])
    assert partial["group_recall_at_5"] == 0.5
    assert partial["complete_at_5"] == 0
    assert complete["complete_at_5"] == alternative["complete_at_5"] == 1


def test_first_claim_mrr_does_not_imply_complete_answer_evidence():
    result = verifier.metrics([{"alternatives": [["a"]]}, {"alternatives": [["b"]]}], ["a"])
    assert result["group_mrr"] == 1
    assert result["group_recall_at_5"] == 0.5
    assert result["complete_at_10"] == 0


def test_percentile_uses_linear_interpolation_and_mismatch_fails():
    assert verifier.percentile([40, 10, 30, 20], 0.5) == 25
    assert verifier.percentile([40, 10, 30, 20], 0.95) == pytest.approx(38.5)
    with pytest.raises(ValueError, match="independent_metric_mismatch"):
        verifier.close(0.5, 1)
