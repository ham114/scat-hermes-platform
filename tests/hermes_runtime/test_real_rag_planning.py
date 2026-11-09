from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from hermes_runtime.real_rag import EvidenceStore, load_config, role_tool_names, tool_schemas

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def planned():
    config = load_config(ROOT / "services/rag/real-agent-v1.json") | {"planning_enabled": True}
    text = "样本均值不证明因果，应控制设备配置等混杂因素。"
    row = {
        "chunk_id": "one",
        "source_id": "rules",
        "source_version": "v1",
        "text": text,
        "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
    }
    store = EvidenceStore({"one": row}, config, "run", lambda q: [row])
    identity = store.search("research", "统计解释")["results"][0]["evidence_id"]
    store.read("verifier", [identity])
    value = {
        "status": "answerable",
        "claims": [{"claim": text, "evidence": [{"evidence_id": identity, "quote": text}]}],
        "coverage": [
            {"target_id": "T1", "status": "covered", "claim_indices": [0], "reason": "原文说明"}
        ],
    }
    return store, value


def test_fixed_plan_and_complete_coverage_are_required(planned):
    store, value = planned
    with pytest.raises(ValueError, match="all_planned"):
        store.submit("verifier", value)
    targets = [{"id": "T1", "question": "该差异是否证明因果，需控制什么？"}]
    store.plan("main", targets)
    targets[0]["question"] = "tampered"
    result = store.submit("verifier", value)
    assert result["targets"][0]["question"] != "tampered"
    assert result["coverage"][0]["claim_indices"] == [0]
    with pytest.raises(ValueError, match="already_fixed"):
        store.plan("main", targets)


@pytest.mark.parametrize(
    "targets",
    [
        [],
        [{"id": "T2", "question": "q"}],
        [{"id": "T1", "question": ""}],
        [{"id": "T1", "question": "q", "gold": "answer"}],
    ],
)
def test_invalid_plan_or_gold_fields_rejected(planned, targets):
    store, _ = planned
    with pytest.raises(ValueError):
        store.plan("main", targets)


@pytest.mark.parametrize(
    "change", ["missing", "unknown_target", "no_claims", "bad_index", "unresolved"]
)
def test_incomplete_or_fabricated_coverage_cannot_be_answerable(planned, change):
    store, value = planned
    store.plan("main", [{"id": "T1", "question": "边界？"}])
    if change == "missing":
        value["coverage"] = []
    elif change == "unknown_target":
        value["coverage"][0]["target_id"] = "other"
    elif change == "no_claims":
        value["coverage"][0]["claim_indices"] = []
    elif change == "bad_index":
        value["coverage"][0]["claim_indices"] = [99]
    else:
        value["coverage"][0]["status"] = "insufficient"
    with pytest.raises(ValueError):
        store.submit("verifier", value)


def test_partial_answer_exposes_the_unresolved_user_target(planned):
    store, value = planned
    store.plan("main", [{"id": "T1", "question": "还缺什么数据？"}])
    value["coverage"][0].update(status="insufficient", reason="未找到直接证据")
    value["status"] = "insufficient"
    result = store.submit("verifier", value)
    assert result["answer"].startswith("部分证据不足")
    assert "还缺什么数据" in result["answer"]


def test_planning_schema_is_opt_in_and_contains_the_checklist():
    normal = {s["name"]: s for s in tool_schemas()}
    planned = {s["name"]: s for s in tool_schemas(True)}
    assert "rag_plan" not in normal and "rag_plan" in planned
    candidate = planned["rag_submit"]["parameters"]["properties"]["candidate"]
    assert "coverage" in candidate["required"]
    assert "answer" not in candidate["properties"]


def test_candidate_endpoint_requires_the_matching_backend(tmp_path):
    config = json.loads((ROOT / "services/rag/real-agent-v1.json").read_text())
    config["retrieval_url"] = "http://127.0.0.1:18133"
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="endpoint"):
        load_config(path)
    config["retrieval_backend"] = "qwen06b"
    path.write_text(json.dumps(config))
    assert load_config(path)["retrieval_backend"] == "qwen06b"


def test_deep_role_tool_groups_do_not_expose_submit_or_plan_to_researchers():
    assert role_tool_names("deep", 0, planning=True, scoped=True) == {"rag_plan", "delegate_task"}
    for ordinal in (1, 2):
        assert role_tool_names("deep", ordinal, planning=True, scoped=True) == {
            "rag_read",
            "rag_search",
        }
    assert role_tool_names("deep", 3, planning=True, scoped=True) == {
        "rag_read",
        "rag_search",
        "rag_submit",
    }


def test_delegation_cannot_select_another_model_or_force_receipt_placeholder():
    from hermes_runtime.real_rag import readonly_delegation_task

    task = {
        "goal": "read and submit",
        "context": "evidence clues",
        "model": "other-provider",
        "toolsets": ["terminal"],
        "output_schema": {"const": {"receipt_id": "pending"}},
    }
    normalized = readonly_delegation_task(task)
    assert normalized == {"goal": "read and submit", "context": "evidence clues"}
    assert "output_schema" in task


def test_quote_rejection_locates_the_bad_citation_without_fabricating_a_repair(planned):
    from hermes_runtime.real_rag import QuoteValidationError

    store, value = planned
    store.plan("main", [{"id": "T1", "question": "边界？"}])
    value["claims"][0]["evidence"][0]["quote"] = "not in source"
    with pytest.raises(QuoteValidationError) as caught:
        store.submit("verifier", value)
    assert caught.value.details["claim_index"] == 0
    assert caught.value.details["citation_index"] == 0
    assert caught.value.details["evidence_id"] == value["claims"][0]["evidence"][0]["evidence_id"]
    assert not store.receipts
