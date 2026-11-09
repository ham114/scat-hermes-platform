from __future__ import annotations

import copy
import hashlib

import pytest

from rag_retrieval.claim_groups import group_completion_rank
from rag_retrieval.reviewed_dataset import convert_reviewed, minimal_bundles


def fixture():
    rows = []
    for cid, text, section in [
        ("a", "必须同时满足条件甲。", "rules"),
        ("b", "必须同时满足条件乙。", "rules"),
        ("c", "必须同时满足条件甲。必须同时满足条件乙。", "rules"),
        ("d", "必须同时满足条件甲。", "different-table"),
    ]:
        rows.append(
            {
                "chunk_id": cid,
                "source_id": "source",
                "source_version": "v1",
                "section_path": [section],
                "text": text,
                "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            }
        )
    refs = []
    for row in rows[:2]:
        refs.append(
            {k: row[k] for k in ["chunk_id", "source_id", "source_version", "text_sha256"]}
            | {"quote": row["text"], "start_char": 0, "end_char": len(row["text"])}
        )
    case = {
        "qid": "synthetic",
        "corpus_sha256": "corpus",
        "split": "development",
        "human_review": "approved",
        "type": "multi_source",
        "difficulty": "medium",
        "question": "甲乙规则是什么？",
        "reference": "甲乙必须满足。",
        "claims": [{"text": "条件甲乙都必须满足", "evidence": refs}],
    }
    return case, rows


def test_joint_evidence_and_complete_alternative_scored_correctly():
    case, rows = fixture()
    _, labels, audit = convert_reviewed([case], rows, {}, "corpus")
    group = labels["cases"][0]["document_claim_groups"][0]
    assert group["alternatives"] == [["c"], ["a", "b"]]
    assert group_completion_rank(group, ["a"]) is None
    assert group_completion_rank(group, ["a", "b"]) == 2
    assert group_completion_rank(group, ["c"]) == 1
    assert audit["cases"][0]["single_chunk_can_cover_all"] == ["c"]


def test_alternative_witnesses_do_not_require_both():
    case, rows = fixture()
    rules = {"cases": {"synthetic": {"claim_modes": {"1": "any-witness"}}}}
    group = convert_reviewed([case], rows, rules, "corpus")[1]["cases"][0]["document_claim_groups"][
        0
    ]
    assert group["alternatives"] == [["a"], ["b"], ["c"]]
    assert group_completion_rank(group, ["b"]) == 1


def test_same_words_in_different_section_not_automatically_accepted():
    case, rows = fixture()
    group = convert_reviewed([case], rows, {}, "corpus")[1]["cases"][0]["document_claim_groups"][0]
    assert group_completion_rank(group, ["d", "b"]) is None


def test_explicit_exclusion_preserves_user_question_and_answer():
    case, rows = fixture()
    case["claims"].append(copy.deepcopy(case["claims"][0]))
    evaluation, labels, audit = convert_reviewed(
        [case],
        rows,
        {"cases": {"synthetic": {"exclude_claims": {"2": "duplicate annotation"}}}},
        "corpus",
    )
    assert len(labels["cases"][0]["document_claim_groups"]) == 1
    assert audit["excluded_claims"] == 1
    assert evaluation["cases"][0]["question"] == case["question"]
    assert evaluation["cases"][0]["reference"] == case["reference"]


@pytest.mark.parametrize(
    "field,value",
    [("start_char", 1), ("quote", "编造规则"), ("source_version", "v2"), ("chunk_id", "unknown")],
)
def test_changed_or_fabricated_evidence_rejected(field, value):
    case, rows = fixture()
    case["claims"][0]["evidence"][0][field] = value
    with pytest.raises(ValueError):
        convert_reviewed([case], rows, {}, "corpus")


def test_unapproved_revision_does_not_silently_become_approved():
    case, rows = fixture()
    case["human_review"] = "revised_after_user_review"
    with pytest.raises(ValueError, match="revision_use_not_authorized"):
        convert_reviewed([case], rows, {}, "corpus")
    result = convert_reviewed([case], rows, {"authorize_use_of_presented_revision": True}, "corpus")
    assert result[0]["cases"][0]["human_review"] == "revised_after_user_review"


def test_empty_support_and_misspelled_rule_fail():
    with pytest.raises(ValueError, match="empty_support_unit"):
        minimal_bundles([set()])
    case, rows = fixture()
    with pytest.raises(ValueError, match="rule_claim_missing"):
        convert_reviewed(
            [case], rows, {"cases": {"synthetic": {"exclude_claims": {"9": "bad"}}}}, "corpus"
        )


def test_manual_alternative_requires_real_quote():
    case, rows = fixture()
    rules = {
        "cases": {
            "synthetic": {
                "extra_alternatives": {
                    "1": [
                        {
                            "chunk_ids": ["d"],
                            "rationale": "a separately reviewed alternative",
                            "quotes": [{"chunk_id": "d", "quote": "不存在的内容"}],
                        }
                    ]
                }
            }
        }
    }
    with pytest.raises(ValueError, match="reviewed_alternative_quote_invalid"):
        convert_reviewed([case], rows, rules, "corpus")
