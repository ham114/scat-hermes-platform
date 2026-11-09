"""Synthetic evidence store for an isolated native Hermes delegation demo."""

from __future__ import annotations

import hashlib
import json
import threading
from uuid import uuid4

DOCUMENTS = {
    "DEMO-POLICY": {
        "version": "2",
        "status": "active",
        "title": "合成预测规则",
        "text": "演示模型SYNTH-CV的准入规则：有效观测点至少20个，缺失率不得超过2%。两项必须同时满足，否则不能执行预测。此资料仅为合成技术演示。",
    },
    "DEMO-QUALITY": {
        "version": "1",
        "status": "active",
        "title": "合成批次质量",
        "text": "合成批次DEMO-A、轨迹SYNTH-001：当前有效观测点18个，缺失率5%。这是固定的合成质量报告，不是真实航班数据。",
    },
    "DEMO-POLICY-OLD": {
        "version": "1",
        "status": "retired",
        "title": "已停用的合成规则",
        "text": "旧演示规则只要求10个点；该版本已停用，不得作为当前依据。",
    },
}


def bound_session(parent_session: str | None, dispatch_session: str | None, known: set[str]) -> str:
    """Use native dispatch metadata, never a session named in model arguments."""
    if parent_session and dispatch_session and parent_session != dispatch_session:
        raise ValueError("conflicting_demo_session")
    session = parent_session or dispatch_session
    if not session or session not in known:
        raise ValueError("unbound_demo_session")
    return session


class DemoEvidence:
    def __init__(self, scope: str, run_id: str):
        self.scope, self.run_id = scope, run_id
        self.lock = threading.RLock()
        self.reads: dict[str, set[str]] = {}
        self.events: list[dict] = []
        self.verifications: dict[str, dict] = {}

    def reference(self, source_id: str) -> dict:
        doc = DOCUMENTS[source_id]
        return {
            "source_id": source_id,
            "source_version": doc["version"],
            "sha256": hashlib.sha256(doc["text"].encode()).hexdigest(),
            "scope": self.scope,
            "run_id": self.run_id,
        }

    def validate(self, ref: dict) -> str:
        source_id = ref.get("source_id")
        if source_id not in DOCUMENTS or DOCUMENTS[source_id]["status"] != "active":
            raise ValueError("source_not_active")
        if ref != self.reference(source_id):
            raise ValueError("evidence_scope_version_or_digest_mismatch")
        return source_id

    def search(self, session: str, query: str) -> dict:
        from rag_retrieval.baseline import tokenize_unicode

        # This tiny closed synthetic corpus uses transparent term overlap, not
        # the frozen production BM25 implementation or its admission metrics.
        terms = set(tokenize_unicode(query))
        ranked = sorted(
            [
                (key, len(terms & set(tokenize_unicode(doc["title"] + doc["text"]))))
                for key, doc in DOCUMENTS.items()
                if doc["status"] == "active"
            ],
            key=lambda item: (-item[1], item[0]),
        )[:2]
        results = [
            {
                "reference": self.reference(key),
                "title": DOCUMENTS[key]["title"],
                "snippet": DOCUMENTS[key]["text"][:32],
            }
            for key, score in ranked
            if score > 0
        ]
        with self.lock:
            self.events.append(
                {
                    "session": session,
                    "tool": "demo_search",
                    "source_ids": [x["reference"]["source_id"] for x in results],
                }
            )
        return {"synthetic": True, "results": results}

    def read(self, session: str, ref: dict) -> dict:
        source_id = self.validate(ref)
        with self.lock:
            self.reads.setdefault(session, set()).add(source_id)
            self.events.append({"session": session, "tool": "demo_read", "source_ids": [source_id]})
        return {
            "reference": self.reference(source_id),
            "text": DOCUMENTS[source_id]["text"],
            "synthetic": True,
        }

    def verify(self, session: str, references: list[dict], facts: dict) -> dict:
        ids = {self.validate(ref) for ref in references}
        expected = {
            "effective_points": 18,
            "missing_rate_percent": 5,
            "minimum_points": 20,
            "maximum_missing_percent": 2,
        }
        with self.lock:
            if ids != {"DEMO-POLICY", "DEMO-QUALITY"} or not ids.issubset(
                self.reads.get(session, set())
            ):
                raise ValueError("verifier_must_read_both_original_sources")
            if facts != expected or any(type(v) is not int for v in facts.values()):
                raise ValueError("facts_not_supported_by_original_sources")
            identity = "verification-" + uuid4().hex[:12]
            result = {
                "verification_id": identity,
                "verified": True,
                "eligible": False,
                "missing_points": 2,
                "facts": expected,
                "evidence_ids": sorted(ids),
                "references": [self.reference(key) for key in sorted(ids)],
                "synthetic": True,
            }
            self.verifications[identity] = result
            self.events.append(
                {"session": session, "tool": "demo_verify", "verification_id": identity}
            )
            return result

    def check_answer(self, answer: dict) -> bool:
        receipt = self.verifications.get(answer.get("verification_id"))
        if not receipt:
            return False
        return (
            all(
                answer.get(key) == receipt[key]
                for key in ("eligible", "missing_points", "evidence_ids")
            )
            and answer.get("eligible") is False
        )


def schemas() -> list[dict]:
    ref = {
        "type": "object",
        "properties": {
            key: {"type": "string"}
            for key in ("source_id", "source_version", "sha256", "scope", "run_id")
        },
        "required": ["source_id", "source_version", "sha256", "scope", "run_id"],
        "additionalProperties": False,
    }
    facts = {
        "type": "object",
        "properties": {
            key: {"type": "integer"}
            for key in (
                "effective_points",
                "missing_rate_percent",
                "minimum_points",
                "maximum_missing_percent",
            )
        },
        "required": [
            "effective_points",
            "missing_rate_percent",
            "minimum_points",
            "maximum_missing_percent",
        ],
        "additionalProperties": False,
    }
    return [
        {
            "name": "demo_search",
            "description": "检索本轮固定合成资料，返回当前版本引用。不能读取真实文件或网络。",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
        },
        {
            "name": "demo_read",
            "description": "按搜索返回的完整reference回读合成原文；保留版本、摘要、scope/run。",
            "parameters": {
                "type": "object",
                "properties": {"reference": ref},
                "required": ["reference"],
                "additionalProperties": False,
            },
        },
        {
            "name": "demo_verify",
            "description": "仅独立核验子Agent使用。必须亲自回读两份原文，再提交四项数字事实及原引用；返回核验回执。",
            "parameters": {
                "type": "object",
                "properties": {
                    "references": {"type": "array", "items": ref, "minItems": 2, "maxItems": 2},
                    "facts": facts,
                },
                "required": ["references", "facts"],
                "additionalProperties": False,
            },
        },
    ]


def encode(result: dict) -> str:
    return json.dumps(result, ensure_ascii=False)
