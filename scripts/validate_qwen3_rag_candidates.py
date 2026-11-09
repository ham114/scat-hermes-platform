"""Offline, synthetic FP16 smoke checks; never a retrieval admission benchmark."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = "/home/h3c/anaconda3/envs/scat-rag-train/bin/python"
CASES = [
    (
        "What is the capital of China?",
        "The capital of China is Beijing.",
        "Water freezes at zero degrees Celsius.",
    ),
    ("水在标准大气压下的沸点是多少？", "标准大气压下，水的沸点是100摄氏度。", "地球围绕太阳公转。"),
    ("地球绕什么天体公转？", "地球绕太阳公转。", "标准大气压下水在100摄氏度沸腾。"),
]
INSTRUCTION = "Given a web search query, retrieve relevant passages that answer the query"


def check(kind: str) -> dict:
    import torch
    from torch.nn import functional
    from transformers import AutoModel, AutoModelForCausalLM, AutoTokenizer

    manifest = json.loads((ROOT / "services/rag/qwen3-4b-download.json").read_text())
    row = next(model for model in manifest["models"] if model["kind"] == kind)
    if not torch.cuda.is_available() or torch.cuda.mem_get_info(0)[0] < 12 * 1024**3:
        raise ValueError("cuda_headroom_unavailable")
    torch.set_num_threads(4)
    torch.cuda.reset_peak_memory_stats(0)
    started = time.monotonic()
    tokenizer = AutoTokenizer.from_pretrained(
        row["destination"], padding_side="left", local_files_only=True, trust_remote_code=False
    )
    factory = AutoModelForCausalLM if kind == "reranker" else AutoModel
    model = factory.from_pretrained(
        row["destination"],
        torch_dtype=torch.float16,
        device_map={"": "cuda:0"},
        attn_implementation="sdpa",
        local_files_only=True,
        trust_remote_code=False,
    ).eval()
    torch.cuda.synchronize()
    loaded = time.monotonic() - started
    scores = []
    timings = []
    with torch.inference_mode():
        for query, positive, negative in CASES:
            if kind == "reranker":
                prefix = (
                    "<|im_start|>system\nJudge whether the Document meets the requirements "
                    "based on the Query and the Instruct provided. Note that the answer "
                    'can only be "yes" or "no".<|im_end|>\n<|im_start|>user\n'
                )
                suffix = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
                texts = [
                    prefix
                    + f"<Instruct>: {INSTRUCTION}\n<Query>: {query}\n<Document>: {doc}"
                    + suffix
                    for doc in (positive, negative)
                ]
            else:
                texts = [f"Instruct: {INSTRUCTION}\nQuery: {query}", positive, negative]
            batch = tokenizer(
                texts,
                padding=True,
                truncation=True,
                max_length=512,
                add_special_tokens=False,
                return_tensors="pt",
            ).to("cuda:0")
            torch.cuda.synchronize()
            start = time.monotonic()
            result = model(**batch, use_cache=False)
            if kind == "reranker":
                yes, no = [tokenizer.convert_tokens_to_ids(word) for word in ("yes", "no")]
                values = result.logits[:, -1, [no, yes]].float().softmax(dim=-1)[:, 1]
            else:
                vector = functional.normalize(result.last_hidden_state[:, -1].float(), dim=-1)
                if vector.shape[1] != 2560:
                    raise ValueError("unexpected_embedding_dimension")
                values = vector[1:] @ vector[0]
            torch.cuda.synchronize()
            timings.append(round((time.monotonic() - start) * 1000, 3))
            if not torch.isfinite(values).all():
                raise ValueError("nonfinite_model_output")
            pair = values.cpu().tolist()
            scores.append(pair)
            del result, batch, values
    passed = all(pair[0] > pair[1] for pair in scores)
    return {
        "status": "passed" if passed else "failed",
        "kind": kind,
        "repo": row["repo"],
        "revision": row["modelscope_revision"],
        "gpu": torch.cuda.get_device_name(0),
        "dtype": "float16",
        "attention": "sdpa",
        "max_input_tokens": 512,
        "synthetic_queries": len(CASES),
        "positive_negative_scores": scores,
        "load_seconds": round(loaded, 3),
        "small_batch_forward_ms_including_first": timings,
        "peak_allocated_bytes": torch.cuda.max_memory_allocated(0),
        "peak_reserved_bytes": torch.cuda.max_memory_reserved(0),
        "business_corpus_used": False,
        "retrieval_quality_admitted": False,
        "latency_sla_validated": False,
        "api_calls": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=["reranker", "embedding"])
    args = parser.parse_args()
    if sys.executable != PYTHON:
        raise ValueError("use_existing_scat_rag_train")
    if args.kind:
        row = check(args.kind)
        print(json.dumps(row))
        return 0 if row["status"] == "passed" else 1
    download = json.loads((ROOT / "var/validation/20261025-Qwen3-4B-download.json").read_text())
    if download["status"] != "verified":
        raise ValueError("download_not_verified")
    stage = ROOT / ".tmp/qwen3-download/smoke"
    stage.mkdir(parents=True, exist_ok=True)
    env = os.environ | {
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_HOME": str(stage / "hf"),
        "XDG_CACHE_HOME": str(stage / "cache"),
        "TORCH_HOME": str(stage / "torch"),
        "CUDA_CACHE_PATH": str(stage / "cuda"),
        "PYTORCH_KERNEL_CACHE_PATH": str(stage / "kernels"),
        "TMPDIR": str(stage),
        "TMP": str(stage),
        "TEMP": str(stage),
        "TOKENIZERS_PARALLELISM": "false",
    }
    for key in (
        "HF_HOME",
        "XDG_CACHE_HOME",
        "TORCH_HOME",
        "CUDA_CACHE_PATH",
        "PYTORCH_KERNEL_CACHE_PATH",
    ):
        Path(env[key]).mkdir(parents=True, exist_ok=True)
    rows = []
    logs = ROOT / "var/validation"
    for kind in ("reranker", "embedding"):
        result = subprocess.run(
            [PYTHON, "-B", str(Path(__file__)), "--kind", kind],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=300,
        )
        (logs / f"20261025-Qwen3-4B-smoke-{kind}.log").write_text(
            result.stdout + result.stderr, encoding="utf-8"
        )
        if result.returncode:
            rows.append({"kind": kind, "status": "failed", "exit": result.returncode})
        else:
            rows.append(json.loads(result.stdout.strip().splitlines()[-1]))
    report = {
        "status": "passed" if all(row["status"] == "passed" for row in rows) else "failed",
        "models": rows,
        "scope": "offline_synthetic_smoke_only",
        "original_rag_service_switched": False,
    }
    output = ROOT / "docs/validation/20261025-Qwen3-4B-smoke.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
