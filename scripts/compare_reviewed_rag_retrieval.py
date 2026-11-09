"""Run one pinned, offline research-v3 diagnostic comparison in existing Conda."""

from __future__ import annotations

import hashlib
import json
import os
import statistics
import sys
import time
import traceback
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "services/rag/qwen3-reviewed-dev40-v1.json"
PUBLIC = ROOT / "docs/validation/20261026-reviewed-dev40-comparison.json"
LOCK = ROOT / "var/validation/20261026-reviewed-dev40-comparison.lock"
PYTHON = "/home/h3c/anaconda3/envs/scat-rag-train/bin/python"


def sha(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def save(path: Path, value: dict | list) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    if sys.executable != PYTHON:
        raise ValueError("use_existing_scat_rag_train")
    if PUBLIC.exists():
        raise ValueError("comparison_already_completed_do_not_overwrite")
    stage = ROOT / ".tmp/reviewed-rag/compare"
    stage.mkdir(parents=True, exist_ok=True)
    if not stage.resolve().is_relative_to(ROOT.resolve()) or stage.is_symlink():
        raise ValueError("stage_path_invalid")
    for key, folder in {
        "TMPDIR": "tmp",
        "TMP": "tmp",
        "TEMP": "tmp",
        "HF_HOME": "hf",
        "XDG_CACHE_HOME": "cache",
        "TORCH_HOME": "torch",
        "CUDA_CACHE_PATH": "cuda",
        "PYTORCH_KERNEL_CACHE_PATH": "kernels",
    }.items():
        path = stage / folder
        path.mkdir(parents=True, exist_ok=True)
        os.environ[key] = str(path)
    os.environ.update(
        HF_HUB_OFFLINE="1",
        TRANSFORMERS_OFFLINE="1",
        TOKENIZERS_PARALLELISM="false",
        PYTHONNOUSERSITE="1",
        PYTHONDONTWRITEBYTECODE="1",
        OMP_NUM_THREADS="4",
        OPENBLAS_NUM_THREADS="1",
        MKL_NUM_THREADS="1",
    )
    sys.path.insert(0, str(ROOT / "src"))
    import numpy as np
    import torch
    from sentence_transformers import SentenceTransformer

    from rag_retrieval.rerank_comparison import (
        Qwen3Reranker,
        frozen_candidates,
        order_by_scores,
        ranking_metrics,
    )
    from rag_retrieval.runtime import (
        M8_RRF_PROFILE,
        M8RRFIndex,
        build_jieba_tokenizer,
        load_chunk_records,
    )

    config = json.loads(CONFIG.read_text())
    run = ROOT / "var/validation" / ("20261026-reviewed-dev40-compare-" + uuid.uuid4().hex[:10])
    run.mkdir(parents=True)
    with LOCK.open("x") as stream:
        stream.write(str(os.getpid()))
    started = time.monotonic()
    progress = {
        "status": "preflight",
        "completed_queries": 0,
        "total_queries": config["case_count"],
    }
    save(run / "progress.json", progress)
    try:
        paths = {key: Path(value["path"]) for key, value in config["inputs"].items()}
        if any(sha(paths[key]) != value["sha256"] for key, value in config["inputs"].items()):
            raise ValueError("input_sha256_changed")
        if any(sha(ROOT / key) != value for key, value in config["retriever_sources"].items()):
            raise ValueError("original_retriever_source_changed")
        evaluation = json.loads(paths["evaluation"].read_text())
        claims = json.loads(paths["claim_groups"].read_text())
        if (
            evaluation["dataset_version"] != config["dataset_version"]
            or claims["dataset_version"] != config["claim_group_version"]
        ):
            raise ValueError("wrong_development_dataset")
        groups = {c["case_id"]: c["document_claim_groups"] for c in claims["cases"]}
        cases = sorted(evaluation["cases"], key=lambda c: c["case_id"])
        if len(cases) != config["case_count"] or set(groups) != {c["case_id"] for c in cases}:
            raise ValueError("case_set_changed")
        if any(c["split"] != "dev" or c["expected_route"] != "rag" for c in cases):
            raise ValueError("non_development_or_non_document_case")
        for case in cases:
            case["document_claim_groups"] = groups[case["case_id"]]
        chunks, records = load_chunk_records(paths["chunks"])
        if len(chunks) != config["chunk_count"]:
            raise ValueError("corpus_count_changed")
        for case in cases:
            for group in case["document_claim_groups"]:
                for alternative in group["alternatives"]:
                    if not alternative or not set(alternative).issubset(records):
                        raise ValueError("invalid_evidence_reference")
        metadata = json.loads(paths["embedding_cache_metadata"].read_text())
        dense_path = (
            "/mnt/h3c/scat-rag/models/embedding/bge-m3-5617a9f61b028005a4858fdac845db406aefb181"
        )
        expected_cache = {
            "schema_version": "scat-rag-online-dense-cache/1.0",
            "corpus_sha256": config["inputs"]["chunks"]["sha256"],
            "model_id": dense_path,
            "normalized": True,
            "row_count": len(chunks),
        }
        if metadata != expected_cache:
            raise ValueError("original_embedding_cache_identity_mismatch")
        vectors = np.load(paths["embedding_cache"], mmap_mode="r")
        if vectors.shape != (len(chunks), 1024) or not np.isfinite(vectors).all():
            raise ValueError("invalid_original_vectors")
        manifest_path = ROOT / "services/rag/qwen3-4b-download.json"
        manifest = json.loads(manifest_path.read_text())
        model_row = next(row for row in manifest["models"] if row["kind"] == "reranker")
        for row in model_row["files"]:
            path = Path(model_row["destination"]) / row["path"]
            if path.stat().st_size != row["size"] or sha(path) != row["sha256"]:
                raise ValueError("reranker_integrity_failure")
        torch.set_num_threads(4)
        torch.set_num_interop_threads(1)
        if not torch.cuda.is_available() or torch.cuda.mem_get_info(0)[0] < 14 * 1024**3:
            raise ValueError("insufficient_cuda_headroom")
        gpu_before = list(torch.cuda.mem_get_info(0))
        dense = SentenceTransformer(dense_path, device="cuda", local_files_only=True)
        index = M8RRFIndex(
            chunks=chunks,
            records=records,
            model=dense,
            corpus_vectors=vectors,
            tokenizer=build_jieba_tokenizer(),
        )
        reranker = Qwen3Reranker(
            model_row["destination"],
            batch_size=config["batch_size"],
            max_length=config["max_input_tokens"],
        )
        query, docs = (
            "What is the capital of China?",
            ["The capital of China is Beijing.", "Water freezes at zero degrees Celsius."],
        )
        reference, _ = reranker.score(query, docs, optimized=False)
        optimized, _ = reranker.score(query, docs)
        difference = max(abs(a - b) for a, b in zip(reference, optimized, strict=True))
        if difference > 1e-4:
            raise ValueError("optimized_logits_differ")
        # Test the fixed maximum batch/context before reading any model rankings.
        reranker.score(
            "Synthetic resource warmup", ["public synthetic text " * 2500] * config["batch_size"]
        )
        dense.encode(
            ["Synthetic resource warmup"], normalize_embeddings=True, show_progress_bar=False
        )
        warmup_peak = torch.cuda.max_memory_reserved(0)
        torch.cuda.empty_cache()
        torch.cuda.reset_peak_memory_stats(0)
        rows = []
        for number, case in enumerate(cases, start=1):
            if time.monotonic() - started > config["max_wall_seconds"]:
                raise ValueError("comparison_wall_budget_exceeded")
            question = " ".join(case["question"].split())
            torch.cuda.synchronize()
            begin = time.perf_counter()
            ids = frozen_candidates(index, question, config["candidate_limit"])
            torch.cuda.synchronize()
            retrieval_ms = (time.perf_counter() - begin) * 1000
            expected = [item["chunk_id"] for item in index.search(question, limit=10)]
            if ids[:10] != expected or len(ids) != config["candidate_limit"]:
                raise ValueError("original_retrieval_parity_failure")
            documents = [index.chunks_by_id[chunk_id].retrieval_text() for chunk_id in ids]
            torch.cuda.synchronize()
            begin = time.perf_counter()
            scores, info = reranker.score(question, documents)
            ranked = order_by_scores(ids, scores)
            torch.cuda.synchronize()
            rerank_ms = (time.perf_counter() - begin) * 1000
            if set(ranked) != set(ids):
                raise ValueError("candidate_pool_changed")
            baseline, candidate = ranking_metrics(case, ids), ranking_metrics(case, ranked)
            rows.append(
                {
                    "case_id": case["case_id"],
                    "category": case["category"],
                    "language": case["language"],
                    "query_sha256": hashlib.sha256(question.encode()).hexdigest(),
                    "baseline_ranking": ids,
                    "candidate_ranking": ranked,
                    "scores_in_original_order": scores,
                    "baseline": baseline,
                    "candidate": candidate,
                    "retrieval_ms": retrieval_ms,
                    "rerank_ms": rerank_ms,
                    "total_candidate_ms": retrieval_ms + rerank_ms,
                    "tokens": info,
                }
            )
            save(run / "per-query-private.json", rows)
            progress.update(
                status="running",
                completed_queries=number,
                last_query_seconds=round((retrieval_ms + rerank_ms) / 1000, 3),
            )
            save(run / "progress.json", progress)
            print(json.dumps(progress), flush=True)

        def mean_metrics(selected, arm):
            return {
                key: statistics.fmean(row[arm][key] for row in selected) for key in selected[0][arm]
            }

        def latency(values):
            return {
                "samples": len(values),
                "p50": float(np.percentile(values, 50)),
                "p95": float(np.percentile(values, 95)),
                "mean": statistics.fmean(values),
            }

        baseline, candidate = mean_metrics(rows, "baseline"), mean_metrics(rows, "candidate")
        regressions = sum(
            row["candidate"]["group_recall_at_5"] < row["baseline"]["group_recall_at_5"] - 1e-12
            for row in rows
        )
        improvements = sum(
            row["candidate"]["group_recall_at_5"] > row["baseline"]["group_recall_at_5"] + 1e-12
            for row in rows
        )
        categories = {
            name: {
                "count": sum(row["category"] == name for row in rows),
                **{
                    arm: mean_metrics([row for row in rows if row["category"] == name], arm)
                    for arm in ("baseline", "candidate")
                },
            }
            for name in sorted({row["category"] for row in rows})
        }
        times = {
            "baseline_retrieval_ms": latency([row["retrieval_ms"] for row in rows]),
            "rerank_only_ms": latency([row["rerank_ms"] for row in rows]),
            "candidate_total_ms": latency([row["total_candidate_ms"] for row in rows]),
        }
        gates = config["gate_reference_only"]
        diagnostic = {
            "r5": candidate["group_recall_at_5"] >= gates["group_recall_at_5"],
            "r10": candidate["group_recall_at_10"] >= gates["group_recall_at_10"],
            "candidate_recall": candidate["candidate_group_recall_at_100"]
            >= gates["candidate_group_recall_at_100"],
            "r5_gain": candidate["group_recall_at_5"] - baseline["group_recall_at_5"]
            >= gates["minimum_r5_gain"],
            "r5_regressions": regressions <= gates["maximum_r5_regressions"],
            "category_floor": min(
                value["candidate"]["group_recall_at_5"] for value in categories.values()
            )
            >= gates["minimum_category_r5"],
            "p50": times["candidate_total_ms"]["p50"] <= gates["p50_ms"],
            "p95": times["candidate_total_ms"]["p95"] <= gates["p95_ms"],
            "p50_increase": times["candidate_total_ms"]["p50"]
            - times["baseline_retrieval_ms"]["p50"]
            <= gates["maximum_p50_increase_ms"],
        }
        report = {
            "schema_version": 1,
            "status": "comparison_completed",
            "scope": config["scope"],
            "run_id": run.name,
            "config_sha256": sha(CONFIG),
            "inputs": config["inputs"],
            "parameters": {
                key: config[key]
                for key in (
                    "candidate_limit",
                    "batch_size",
                    "max_input_tokens",
                    "dtype",
                    "attention",
                    "timing_policy",
                )
            },
            "dataset_version": config["dataset_version"],
            "claim_group_version": config["claim_group_version"],
            "qa_started": False,
            "case_count": len(rows),
            "chunk_count": len(chunks),
            "languages": sorted({row["language"] for row in rows}),
            "retriever_profile": M8_RRF_PROFILE,
            "reranker_repo": model_row["repo"],
            "reranker_revision": model_row["modelscope_revision"],
            "baseline": baseline,
            "candidate": candidate,
            "r5_delta": candidate["group_recall_at_5"] - baseline["group_recall_at_5"],
            "r5_improved_queries": improvements,
            "r5_regressed_queries": regressions,
            "r5_unchanged_queries": len(rows) - improvements - regressions,
            "by_category": categories,
            "latency": times,
            "diagnostic_gate_reference_only": diagnostic,
            "candidate_pool_parity_queries": len(rows),
            "original_runtime_top10_parity_queries": len(rows),
            "scored_pairs": sum(row["tokens"]["pairs"] for row in rows),
            "truncated_pairs": sum(row["tokens"]["truncated_pairs"] for row in rows),
            "max_untruncated_input_tokens": max(
                row["tokens"]["max_untruncated_tokens"] for row in rows
            ),
            "gpu": torch.cuda.get_device_name(0),
            "gpu_free_total_before": gpu_before,
            "peak_allocated_bytes": torch.cuda.max_memory_allocated(0),
            "peak_reserved_bytes": torch.cuda.max_memory_reserved(0),
            "warmup_peak_reserved_bytes": warmup_peak,
            "optimized_vs_full_score_max_difference": difference,
            "elapsed_seconds": time.monotonic() - started,
            "retrieval_quality_admitted": False,
            "latency_sla_validated": False,
            "independent_holdout_used": False,
            "api_calls": 0,
            "production_service_changed": False,
            "labels_modified": False,
            "source_sha256": {
                name: sha(ROOT / name)
                for name in [
                    "scripts/compare_reviewed_rag_retrieval.py",
                    "src/rag_retrieval/rerank_comparison.py",
                    "services/rag/qwen3-reviewed-dev40-v1.json",
                ]
            },
        }
        for key, value in config["inputs"].items():
            if sha(paths[key]) != value["sha256"]:
                raise ValueError("original_input_changed_during_comparison")
        save(run / "summary.json", report)
        save(PUBLIC, report)
        progress.update(status="completed")
        save(run / "progress.json", progress)
        print(
            json.dumps(
                {"status": "comparison_completed", "public_report": str(PUBLIC), "run_id": run.name}
            ),
            flush=True,
        )
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        traceback.print_exc()
        progress.update(
            status="failed",
            error=type(exc).__name__,
            reason=str(exc) if isinstance(exc, ValueError) else "runtime_or_io",
        )
        save(run / "progress.json", progress)
        print(json.dumps(progress), flush=True)
        return 1
    finally:
        LOCK.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
