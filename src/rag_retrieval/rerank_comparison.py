"""Offline comparison helpers; not registered with the production retriever."""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import asdict
from typing import Any

import numpy as np

from rag_retrieval.baseline import evaluate_ranking, reciprocal_rank_fusion
from rag_retrieval.claim_groups import accepted_evidence_ids, group_completion_rank, score_case
from rag_retrieval.enhancements import (
    apply_source_boost,
    infer_source_ids,
    rank_exact_identifier_matches,
)
from rag_retrieval.runtime import M8_RRF_PROFILE, M8RRFIndex


def frozen_candidates(index: M8RRFIndex, query: str, limit: int = 100) -> list[str]:
    """Mirror the unchanged runtime pipeline, retaining its full candidate order."""
    normalized = " ".join(query.split())
    if not normalized or not 1 <= limit <= int(M8_RRF_PROFILE["candidate_k"]):
        raise ValueError("invalid_candidate_query_or_limit")
    depth = int(M8_RRF_PROFILE["candidate_k"])
    lexical = [chunk_id for chunk_id, _ in index.bm25.rank(normalized, limit=depth)]
    vector = np.asarray(
        index.model.encode([normalized], normalize_embeddings=True, show_progress_bar=False)[0],
        dtype=np.float32,
    )
    dense = [
        index.chunk_ids[i]
        for i in np.argsort(-(index.corpus_vectors @ vector), kind="stable")[:depth]
    ]
    exact = [
        chunk_id
        for chunk_id, _ in rank_exact_identifier_matches(normalized, index.chunks, limit=depth)
    ]
    rankings, weights = [lexical, dense], [1.0, 1.0]
    if exact:
        rankings.append(exact)
        weights.append(float(M8_RRF_PROFILE["exact_weight"]))
    fused = reciprocal_rank_fusion(
        rankings, rank_constant=int(M8_RRF_PROFILE["rrf_k"]), weights=weights, limit=depth
    )
    boosted = apply_source_boost(
        fused,
        index.chunks_by_id,
        infer_source_ids(normalized),
        float(M8_RRF_PROFILE["source_multiplier"]),
    )
    return [chunk_id for chunk_id, _ in boosted[:limit]]


def order_by_scores(ids: Sequence[str], scores: Sequence[float]) -> list[str]:
    if (
        len(ids) != len(scores)
        or len(set(ids)) != len(ids)
        or not all(math.isfinite(x) for x in scores)
    ):
        raise ValueError("invalid_rerank_scores")
    return [ids[i] for i in sorted(range(len(ids)), key=lambda i: (-scores[i], i))]


def pack_ids(
    content: Sequence[int], prefix: Sequence[int], suffix: Sequence[int], maximum: int
) -> list[int]:
    budget = maximum - len(prefix) - len(suffix)
    if budget < 1:
        raise ValueError("prompt_exceeds_token_budget")
    return [*prefix, *content[:budget], *suffix]


def ranking_metrics(case: dict[str, Any], ids: Sequence[str]) -> dict[str, float]:
    groups = case["document_claim_groups"]
    if not groups:
        raise ValueError("comparison_requires_document_groups")
    result = score_case(case, ids)
    chunk = asdict(evaluate_ranking(accepted_evidence_ids(case), ids))
    return {
        "group_recall_at_5": result["document_group_recall_at_5"],
        "group_recall_at_10": result["document_group_recall_at_10"],
        "complete_at_5": result["document_complete_at_5"],
        "complete_at_10": result["document_complete_at_10"],
        "group_mrr": result["reciprocal_rank"],
        "accepted_evidence_ndcg_at_10": chunk["ndcg_at_10"],
        "candidate_group_recall_at_100": statistics.fmean(
            float((rank := group_completion_rank(group, ids)) is not None and rank <= 100)
            for group in groups
        ),
    }


class Qwen3Reranker:
    """FP16/SDPA local-only scoring with the official yes/no prompt contract."""

    INSTRUCTION = "Given a web search query, retrieve relevant passages that answer the query"
    PREFIX = (
        "<|im_start|>system\n Judge whether the Document meets the requirements based on the "
        'Query and the Instruct provided. Note that the answer can only be "yes" or "no".'
        "<|im_end|>\n<|im_start|>user\n"
    )
    SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"

    def __init__(self, path: str, *, batch_size: int = 4, max_length: int = 2048):
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        if not 1 <= batch_size <= 4 or not 128 <= max_length <= 2048:
            raise ValueError("comparison_resource_limit")
        self.torch = torch
        self.batch_size = batch_size
        self.max_length = max_length
        self.tokenizer = AutoTokenizer.from_pretrained(
            path, padding_side="left", local_files_only=True, trust_remote_code=False
        )
        self.model = AutoModelForCausalLM.from_pretrained(
            path,
            torch_dtype=torch.float16,
            attn_implementation="sdpa",
            device_map={"": "cuda:0"},
            local_files_only=True,
            trust_remote_code=False,
        ).eval()
        self.prefix = self.tokenizer.encode(self.PREFIX, add_special_tokens=False)
        self.suffix = self.tokenizer.encode(self.SUFFIX, add_special_tokens=False)
        self.no, self.yes = [self.tokenizer.convert_tokens_to_ids(word) for word in ("no", "yes")]
        if self.no == self.yes or self.no is None or self.yes is None:
            raise ValueError("yes_no_token_contract")

    def score(
        self, query: str, documents: Sequence[str], *, optimized: bool = True
    ) -> tuple[list[float], dict]:
        if not query.strip() or not documents or any(not text.strip() for text in documents):
            raise ValueError("empty_reranking_input")
        texts = [
            f"<Instruct>: {self.INSTRUCTION}\n<Query>: {query}\n<Document>: {doc}"
            for doc in documents
        ]
        content = self.tokenizer(
            texts,
            add_special_tokens=False,
            padding=False,
            truncation=False,
            return_attention_mask=False,
        )["input_ids"]
        packed = [pack_ids(ids, self.prefix, self.suffix, self.max_length) for ids in content]
        order = sorted(range(len(packed)), key=lambda i: (len(packed[i]), i))
        scores = [0.0] * len(documents)
        with self.torch.inference_mode():
            for offset in range(0, len(order), self.batch_size):
                indices = order[offset : offset + self.batch_size]
                batch = self.tokenizer.pad(
                    {"input_ids": [packed[i] for i in indices]},
                    padding=True,
                    return_attention_mask=True,
                    return_tensors="pt",
                ).to("cuda:0")
                options = {"logits_to_keep": 1} if optimized else {}
                output = self.model(**batch, use_cache=False, **options)
                values = output.logits[:, -1, [self.no, self.yes]].float().softmax(dim=-1)[:, 1]
                if not self.torch.isfinite(values).all():
                    raise ValueError("nonfinite_model_scores")
                for i, value in zip(indices, values.cpu().tolist(), strict=True):
                    scores[i] = value
                del output, batch, values
        budget = self.max_length - len(self.prefix) - len(self.suffix)
        return scores, {
            "pairs": len(documents),
            "truncated_pairs": sum(len(ids) > budget for ids in content),
            "max_packed_tokens": max(map(len, packed)),
            "max_untruncated_tokens": max(map(len, content)) + len(self.prefix) + len(self.suffix),
        }
