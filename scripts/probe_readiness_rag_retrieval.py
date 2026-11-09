"""Four new technical questions sent only to the existing offline local retriever."""

import hashlib
import json
import os
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261108-release-readiness"
QUESTIONS = [
    {
        "id": "protocol_boundary",
        "kind": "cross_source",
        "question": "SCAT的CFPL计划与实飞误差，和GBAR任务模型的历史回测误差，能否直接混成一个指标？依据哪些协议和样本覆盖？",
    },
    {
        "id": "vertical_sign",
        "kind": "conflict",
        "question": "平台的SCAT预测垂直误差正负号到底是实飞减预测，还是预测减实飞？若旧说明不一致，应采用哪个版本？",
    },
    {
        "id": "unit_alignment",
        "kind": "multi_hop",
        "question": "SCAT原始飞行高度FL、航迹展示的高度米和预测水平误差米，分别怎样对齐单位与时间？",
    },
    {
        "id": "unsupported_class_training",
        "kind": "insufficient",
        "question": "GBAR辅助分类头类别索引7代表什么飞行阶段？给出其分类准确率以及验收航班未出现在训练集中的证明。",
    },
]


def main():
    os.umask(0o077)
    target = OUT / "rag-retrieval-new.json"
    assert not target.exists(), "already_frozen"
    private = OUT / "private"
    private.mkdir(exist_ok=True)
    rows = []
    with httpx.Client(base_url="http://127.0.0.1:18133", trust_env=False, timeout=30) as client:
        health = client.get("/health")
        health.raise_for_status()
        for question in QUESTIONS:
            start = time.monotonic()
            response = client.post("/search", json={"query": question["question"], "limit": 5})
            response.raise_for_status()
            value = response.json()
            raw = json.dumps(value, ensure_ascii=False, sort_keys=True)
            sha = hashlib.sha256(raw.encode()).hexdigest()
            (private / ("rag-retrieval-" + question["id"] + ".json")).write_text(raw)
            results = value.get("results", [])
            rows.append(
                {
                    **question,
                    "http": response.status_code,
                    "seconds": round(time.monotonic() - start, 4),
                    "returned": len(results),
                    "payload_sha256": sha,
                    "result_keys": sorted(value),
                    "candidates_have_text": all(
                        bool(r.get("text") or r.get("content")) for r in results
                    ),
                }
            )
    report = {
        "kind": "new_local_retrieval_only_not_answer_quality",
        "new_external_llm_calls": 0,
        "human_approval": "pending",
        "cases": rows,
    }
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
