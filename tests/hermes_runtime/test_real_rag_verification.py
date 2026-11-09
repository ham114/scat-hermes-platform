from __future__ import annotations

import copy
import hashlib
import importlib.util
from pathlib import Path

import pytest

from hermes_runtime.real_rag import EvidenceStore, load_config

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "verify_real_agent_rag", ROOT / "scripts/verify_real_agent_rag.py"
)
VERIFY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFY)


@pytest.fixture
def receipt_report():
    config = load_config(ROOT / "services/rag/real-agent-v1.json")
    text = "标准气压为1013.2hPa。"
    row = {
        "chunk_id": "chunk",
        "source_id": "terms",
        "source_version": "v1",
        "text": text,
        "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
    }
    records = {"chunk": row}
    store = EvidenceStore(records, config, "run", lambda q: [row])
    identity = store.search("single", "气压")["results"][0]["evidence_id"]
    store.read("single", [identity])
    receipt = store.submit(
        "single",
        {
            "answer": text,
            "status": "answerable",
            "claims": [{"claim": text, "evidence": [{"evidence_id": identity, "quote": text}]}],
        },
    )
    report = {
        "status": "passed",
        "mode": "fast",
        "run_id": "run",
        "result": receipt,
        "agents": {"single": {"ordinal": 0, "tools": ["rag_search", "rag_read", "rag_submit"]}},
        "events": [{"event": "llm_request", "session": "single"}],
        "evidence_operations": store.events,
    }
    return report, records, config


def test_independent_verifier_recomputes_offsets_and_original_read(receipt_report):
    report, records, config = receipt_report
    assert VERIFY.verify_result(report, records, config)["citations_verified"] == 1


@pytest.mark.parametrize(
    "change", ["answer", "offset", "version", "read", "tool", "request", "receipt"]
)
def test_independent_verifier_rejects_corrupted_execution_artifacts(receipt_report, change):
    report, records, config = copy.deepcopy(receipt_report)
    if change == "answer":
        report["result"]["answer"] = "被修改"
    elif change == "offset":
        report["result"]["claims"][0]["evidence"][0]["start_char"] = 1
    elif change == "version":
        report["result"]["claims"][0]["evidence"][0]["reference"]["source_version"] = "old"
    elif change == "read":
        report["evidence_operations"] = [
            e for e in report["evidence_operations"] if e["tool"] != "rag_read"
        ]
    elif change == "tool":
        report["agents"]["single"]["tools"].append("terminal")
    elif change == "request":
        report["events"] = []
    else:
        report["result"]["receipt_id"] = "forged"
    with pytest.raises(ValueError):
        VERIFY.verify_result(report, records, config)


def test_failed_execution_is_not_promoted_by_the_offline_verifier():
    assert VERIFY.verify_result({"status": "failed", "error_type": "TimeoutError"}, {}, {}) == {
        "execution_passed": False,
        "error_type": "TimeoutError",
    }
