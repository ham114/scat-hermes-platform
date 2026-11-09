from __future__ import annotations

import pytest

from hermes_runtime.multi_rag_demo import DemoEvidence, bound_session


@pytest.mark.parametrize("parent,dispatch", [("known", None), (None, "known")])
def test_native_parent_or_dispatch_session_is_accepted(parent, dispatch):
    assert bound_session(parent, dispatch, {"known"}) == "known"


@pytest.mark.parametrize("parent,dispatch", [(None, None), ("known", "other"), (None, "unknown")])
def test_missing_unknown_or_conflicting_native_sessions_are_rejected(parent, dispatch):
    with pytest.raises(ValueError):
        bound_session(parent, dispatch, {"known"})


def test_search_excludes_retired_policy_and_requires_original_reads():
    store = DemoEvidence("scope", "run")
    result = store.search("researcher", "合成预测规则观测点缺失率")
    assert result["results"]
    assert all(x["reference"]["source_id"] != "DEMO-POLICY-OLD" for x in result["results"])
    refs = [store.reference(key) for key in ("DEMO-POLICY", "DEMO-QUALITY")]
    for ref in refs:
        store.read("researcher", ref)
    with pytest.raises(ValueError, match="verifier_must_read"):
        store.verify("verifier", refs, {})


@pytest.mark.parametrize(
    "field,value",
    [("scope", "other"), ("run_id", "other"), ("source_version", "old"), ("sha256", "0" * 64)],
)
def test_cross_run_or_tampered_reference_rejected(field, value):
    store = DemoEvidence("scope", "run")
    ref = store.reference("DEMO-POLICY") | {field: value}
    with pytest.raises(ValueError, match="mismatch"):
        store.read("verifier", ref)


def test_unsupported_facts_and_forged_final_receipts_rejected():
    store = DemoEvidence("scope", "run")
    refs = [store.reference(key) for key in ("DEMO-POLICY", "DEMO-QUALITY")]
    for ref in refs:
        store.read("verifier", ref)
    facts = {
        "effective_points": 18,
        "missing_rate_percent": 5,
        "minimum_points": 20,
        "maximum_missing_percent": 2,
    }
    with pytest.raises(ValueError, match="not_supported"):
        store.verify("verifier", refs, facts | {"effective_points": 28})
    receipt = store.verify("verifier", refs, facts)
    assert store.check_answer(receipt)
    assert not store.check_answer(receipt | {"eligible": True})
    assert not store.check_answer(receipt | {"verification_id": "forged"})
