from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from hermes_runtime.real_rag import (
    EvidenceStore,
    load_config,
    load_corpus,
    readonly_provider_options,
    select_mode,
)

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def config():
    return load_config(ROOT / "services/rag/real-agent-v1.json")


@pytest.fixture
def rows():
    text = "有效点至少20个，缺失率不超过2%。"
    return {
        "chunk": {
            "chunk_id": "chunk",
            "source_id": "policy",
            "source_version": "v2",
            "text": text,
            "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
        }
    }


def populated(config, rows, run="one"):
    store = EvidenceStore(rows, config, run, lambda q: list(rows.values()))
    identity = store.search("researcher", "准入规则")["results"][0]["evidence_id"]
    return store, identity


def candidate(identity):
    return {
        "answer": "有效点至少20个。",
        "status": "answerable",
        "claims": [
            {
                "claim": "有效点至少20个。",
                "evidence": [{"evidence_id": identity, "quote": "有效点至少20个"}],
            }
        ],
    }


def test_verifier_must_personally_read_and_receipt_is_immutable(config, rows):
    store, identity = populated(config, rows)
    store.read("researcher", [identity])
    with pytest.raises(ValueError, match="reviewer_must_read"):
        store.submit("verifier", candidate(identity))
    store.read("verifier", [identity])
    receipt = store.submit("verifier", candidate(identity))
    final = {"receipt_id": receipt["receipt_id"]}
    receipt["answer"] = "篡改答案"
    assert store.resolve(final)["answer"] == "有效点至少20个。"
    with pytest.raises(ValueError, match="invalid_final"):
        store.resolve(final | {"answer": "篡改答案"})
    citation = store.resolve(final)["claims"][0]["evidence"][0]
    assert rows["chunk"]["text"][citation["start_char"] : citation["end_char"]] == citation["quote"]


def test_unclaimed_extra_assertions_never_enter_the_rendered_answer(config, rows):
    store, identity = populated(config, rows)
    store.read("verifier", [identity])
    value = candidate(identity)
    value["answer"] += "文档不存在任何其他规则。"
    result = store.submit("verifier", value)
    assert result["answer"] == "有效点至少20个。"
    assert result["render_policy"] == "claims_only_v1"
    assert result["submitted_answer_sha256"] != result["answer_sha256"]


def test_cross_run_and_unissued_handles_rejected(config, rows):
    first, old = populated(config, rows)
    second, current = populated(config, rows, "two")
    assert old != current
    for identity in (old, "forged"):
        with pytest.raises(ValueError, match="cross_run"):
            second.read("verifier", [identity])
    assert first.read_counts == {}


@pytest.mark.parametrize(
    "field,value",
    [("text", "伪造"), ("source_version", "v1"), ("source_id", "other"), ("chunk_id", "unknown")],
)
def test_retriever_source_tampering_rejected(config, rows, field, value):
    row = copy.deepcopy(rows["chunk"]) | {field: value}
    store = EvidenceStore(rows, config, "run", lambda q: [row])
    with pytest.raises(ValueError, match="identity_mismatch"):
        store.search("researcher", "规则")


@pytest.mark.parametrize("change", ["quote", "claim", "no_evidence", "status", "empty_claims"])
def test_invalid_claim_or_quote_rejected(config, rows, change):
    store, identity = populated(config, rows)
    store.read("verifier", [identity])
    value = candidate(identity)
    if change == "quote":
        value["claims"][0]["evidence"][0]["quote"] = "有效点至少10个"
    elif change == "claim":
        value["claims"][0]["claim"] = "另一个结论"
    elif change == "no_evidence":
        value["claims"][0]["evidence"] = []
    elif change == "status":
        value["status"] = "success"
    else:
        value["claims"] = []
    with pytest.raises(ValueError):
        store.submit("verifier", value)


def test_insufficient_is_explicit_and_does_not_prove_absence(config, rows):
    store = EvidenceStore(rows, config, "run", lambda q: [])
    assert store.search("agent", "未知问题")["results"] == []
    result = store.submit(
        "agent", {"answer": "当前证据不足，无法确认。", "status": "insufficient", "claims": []}
    )
    assert result["semantic_support"] == "model_assessment_requires_external_evaluation"
    assert "不代表全库不存在答案" in result["answer"]


def test_search_and_read_limits(config, rows):
    store, identity = populated(config, rows)
    store.search("researcher", "第二次查询")
    with pytest.raises(ValueError, match="search_budget"):
        store.search("researcher", "第三次查询")
    for _ in range(2):
        store.read("reader", [identity] * 5)
    assert store.read_counts["reader"] == 1
    second = rows["chunk"] | {"chunk_id": "second"}
    expanded = rows | {"second": second}
    bounded = EvidenceStore(
        expanded,
        config | {"max_reads_per_session": 1},
        "bounded",
        lambda q: list(expanded.values()),
    )
    identities = [r["evidence_id"] for r in bounded.search("researcher", "规则")["results"]]
    bounded.read("reader", [identities[0]])
    bounded.read("reader", [identities[0]])
    with pytest.raises(ValueError, match="read_budget"):
        bounded.read("reader", [identities[1]])


@pytest.mark.parametrize(
    "question,mode,expected",
    [
        ("rocd单位", "auto", "fast"),
        ("存在冲突如何处理", "auto", "deep"),
        ("短问题", "deep", "deep"),
        ("很长" * 100, "fast", "fast"),
    ],
)
def test_public_route_policy(config, question, mode, expected):
    assert select_mode(question, mode, config) == expected


@pytest.mark.parametrize(
    "question,mode", [("", "auto"), ("x" * 1001, "auto"), ("question", "other")]
)
def test_route_input_bounds(config, question, mode):
    with pytest.raises(ValueError):
        select_mode(question, mode, config)


def test_corpus_digest_and_per_chunk_validation(tmp_path, config, rows):
    path = tmp_path / "corpus.jsonl"

    def write(values):
        path.write_text(
            "\n".join(json.dumps(v, ensure_ascii=False) for v in values), encoding="utf-8"
        )
        return config | {
            "corpus": str(path),
            "corpus_count": 1,
            "corpus_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }

    valid = write(list(rows.values()))
    assert load_corpus(valid) == rows
    path.write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="digest_mismatch"):
        load_corpus(valid)
    for values in ([rows["chunk"], rows["chunk"]], [rows["chunk"] | {"text": "changed"}]):
        with pytest.raises(ValueError, match="invalid_corpus"):
            load_corpus(write(values))


@pytest.mark.parametrize(
    "field,value",
    [
        ("retrieval_url", "https://external.invalid"),
        ("auto_min_characters", 0),
        ("max_output_tokens", 5000),
        ("max_searches_per_session", 10),
    ],
)
def test_configuration_refuses_unbounded_or_external_settings(tmp_path, config, field, value):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config | {field: value}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(path)


def test_model_token_budget_cannot_be_raised_by_config(tmp_path, config):
    config["batch_budget"]["max_turns"] = 321
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(ValueError, match="exceeds"):
        load_config(path)


def test_early_initialization_failure_removes_temporary_credentials(tmp_path, config, monkeypatch):
    from hermes_runtime import real_rag_native

    def fail(root, config, question, mode, output, budget, stage, identity, **kwargs):
        stage.mkdir(parents=True)
        (stage / ".env").write_text("synthetic-test-only")
        raise RuntimeError("initialization_failed")

    monkeypatch.setattr(real_rag_native, "_run_question", fail)
    with pytest.raises(RuntimeError, match="initialization_failed"):
        real_rag_native.run_question(tmp_path, config, "问题", "fast", tmp_path / "out", None)
    assert not list((tmp_path / ".tmp/real-rag").iterdir())


def test_all_native_clients_get_explicit_disabled_thinking_without_losing_other_options():
    original = {
        "model": "deepseek-flash",
        "reasoning_effort": "high",
        "extra_body": {"thinking": {"type": "enabled"}, "other": 1},
        "max_tokens": 2400,
    }
    actual = readonly_provider_options(original)
    assert actual["extra_body"] == {"thinking": {"type": "disabled"}, "other": 1}
    assert "reasoning_effort" not in actual
    assert actual["max_tokens"] == 2400
    assert original["extra_body"]["thinking"]["type"] == "enabled"
