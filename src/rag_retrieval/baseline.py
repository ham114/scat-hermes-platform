"""Deterministic building blocks for M8 retrieval baselines.

The module deliberately has no model or vector-database dependency.  BM25 and
metric tests can run in the normal project environment; the CLI loads optional
Sentence Transformers only when dense retrieval or reranking is requested in
the isolated ``scat-rag-train`` environment.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass

_TOKEN_PATTERN = re.compile(
    r"[a-z][a-z0-9]*(?:[._/:-][a-z0-9]+)*|\d+(?:\.\d+)?|[\u3400-\u4dbf\u4e00-\u9fff]+",
    re.IGNORECASE,
)
_CJK_PATTERN = re.compile(r"^[\u3400-\u4dbf\u4e00-\u9fff]+$")


@dataclass(frozen=True, slots=True)
class Chunk:
    """The subset of a normalized knowledge chunk used by retrieval."""

    chunk_id: str
    source_id: str
    source_version: str
    section_path: tuple[str, ...]
    text: str
    exact_terms: tuple[str, ...] = ()

    def retrieval_text(self) -> str:
        """Return stable text used by lexical and dense indexes."""

        parts = [self.source_id, *self.section_path, *self.exact_terms, self.text]
        return "\n".join(part for part in parts if part)


@dataclass(frozen=True, slots=True)
class RetrievalMetrics:
    """Per-query retrieval metrics for one ranked list."""

    recall_at_5: float
    recall_at_10: float
    hit_at_5: float
    hit_at_10: float
    reciprocal_rank: float
    ndcg_at_10: float


def tokenize_unicode(text: str) -> list[str]:
    """Tokenize mixed Chinese/English text without external dependencies.

    Latin identifiers are preserved, which is important for ASTERIX item names,
    API paths and model IDs.  Each Chinese run contributes single characters and
    overlapping bigrams.  This deterministic tokenizer is the portable baseline;
    the evaluation CLI also supports a pinned Jieba search-mode tokenizer.
    """

    normalized = unicodedata.normalize("NFKC", text).casefold()
    tokens: list[str] = []
    for match in _TOKEN_PATTERN.finditer(normalized):
        token = match.group(0)
        if _CJK_PATTERN.fullmatch(token):
            tokens.extend(token)
            tokens.extend(token[index : index + 2] for index in range(len(token) - 1))
        else:
            tokens.append(token)
    return tokens


class BM25Index:
    """Small deterministic Okapi BM25 index suitable for the frozen corpus."""

    def __init__(
        self,
        chunks: Sequence[Chunk],
        *,
        tokenizer: Callable[[str], list[str]] = tokenize_unicode,
        k1: float = 1.2,
        b: float = 0.75,
    ) -> None:
        if not chunks:
            raise ValueError("BM25 requires at least one chunk")
        if k1 <= 0:
            raise ValueError("k1 must be positive")
        if not 0 <= b <= 1:
            raise ValueError("b must be between 0 and 1")

        self.chunks = tuple(chunks)
        self.tokenizer = tokenizer
        self.k1 = k1
        self.b = b
        self._term_frequencies: list[Counter[str]] = []
        self._document_lengths: list[int] = []
        document_frequency: Counter[str] = Counter()

        for chunk in self.chunks:
            frequencies = Counter(tokenizer(chunk.retrieval_text()))
            self._term_frequencies.append(frequencies)
            self._document_lengths.append(sum(frequencies.values()))
            document_frequency.update(frequencies)

        self.average_document_length = sum(self._document_lengths) / len(self.chunks)
        document_count = len(self.chunks)
        self._idf = {
            term: math.log(1.0 + (document_count - frequency + 0.5) / (frequency + 0.5))
            for term, frequency in document_frequency.items()
        }

    def rank(self, query: str, *, limit: int = 30) -> list[tuple[str, float]]:
        """Return positive-scoring chunk IDs ordered by score then stable ID."""

        if limit <= 0:
            return []
        query_terms = Counter(self.tokenizer(query))
        scores: list[tuple[str, float]] = []
        average_length = self.average_document_length or 1.0
        for chunk, frequencies, document_length in zip(
            self.chunks,
            self._term_frequencies,
            self._document_lengths,
            strict=True,
        ):
            score = 0.0
            length_ratio = document_length / average_length
            for term, query_frequency in query_terms.items():
                term_frequency = frequencies.get(term, 0)
                if term_frequency == 0:
                    continue
                denominator = term_frequency + self.k1 * (1 - self.b + self.b * length_ratio)
                score += (
                    self._idf.get(term, 0.0)
                    * term_frequency
                    * (self.k1 + 1)
                    / denominator
                    * query_frequency
                )
            if score > 0:
                scores.append((chunk.chunk_id, score))
        scores.sort(key=lambda item: (-item[1], item[0]))
        return scores[:limit]


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[str]],
    *,
    rank_constant: int = 60,
    weights: Sequence[float] | None = None,
    limit: int | None = None,
) -> list[tuple[str, float]]:
    """Fuse ranked chunk IDs using deterministic reciprocal-rank fusion."""

    if rank_constant <= 0:
        raise ValueError("rank_constant must be positive")
    if weights is None:
        weights = [1.0] * len(rankings)
    if len(weights) != len(rankings):
        raise ValueError("RRF weights must match the number of rankings")
    if any(weight <= 0 for weight in weights):
        raise ValueError("RRF weights must be positive")
    scores: defaultdict[str, float] = defaultdict(float)
    best_rank: dict[str, int] = {}
    for ranking, weight in zip(rankings, weights, strict=True):
        seen: set[str] = set()
        for chunk_id in ranking:
            if chunk_id in seen:
                continue
            seen.add(chunk_id)
            rank = len(seen)
            scores[chunk_id] += weight / (rank_constant + rank)
            best_rank[chunk_id] = min(rank, best_rank.get(chunk_id, rank))
    fused = sorted(scores.items(), key=lambda item: (-item[1], best_rank[item[0]], item[0]))
    return fused if limit is None else fused[:limit]


def evaluate_ranking(relevant_ids: Iterable[str], ranked_ids: Sequence[str]) -> RetrievalMetrics:
    """Score a ranked list against binary chunk relevance judgments."""

    relevant = set(relevant_ids)
    if not relevant:
        raise ValueError("At least one relevant chunk ID is required")

    def recall_at(k: int) -> float:
        return len(relevant.intersection(ranked_ids[:k])) / len(relevant)

    def hit_at(k: int) -> float:
        return float(bool(relevant.intersection(ranked_ids[:k])))

    reciprocal_rank = 0.0
    for rank, chunk_id in enumerate(ranked_ids, start=1):
        if chunk_id in relevant:
            reciprocal_rank = 1.0 / rank
            break

    dcg = sum(
        1.0 / math.log2(rank + 1)
        for rank, chunk_id in enumerate(ranked_ids[:10], start=1)
        if chunk_id in relevant
    )
    ideal_count = min(len(relevant), 10)
    ideal_dcg = sum(1.0 / math.log2(rank + 1) for rank in range(1, ideal_count + 1))
    return RetrievalMetrics(
        recall_at_5=recall_at(5),
        recall_at_10=recall_at(10),
        hit_at_5=hit_at(5),
        hit_at_10=hit_at(10),
        reciprocal_rank=reciprocal_rank,
        ndcg_at_10=dcg / ideal_dcg,
    )


def coverage_at_depths(
    relevant_ids: Iterable[str], ranked_ids: Sequence[str], depths: Iterable[int]
) -> dict[str, float]:
    """Return recall/hit coverage at caller-selected candidate depths."""

    relevant = set(relevant_ids)
    if not relevant:
        raise ValueError("At least one relevant chunk ID is required")
    normalized_depths = sorted(set(depths))
    if not normalized_depths or any(depth <= 0 for depth in normalized_depths):
        raise ValueError("Candidate depths must contain positive integers")

    metrics: dict[str, float] = {}
    for depth in normalized_depths:
        matched = relevant.intersection(ranked_ids[:depth])
        metrics[f"recall_at_{depth}"] = len(matched) / len(relevant)
        metrics[f"hit_at_{depth}"] = float(bool(matched))
    return metrics


def normalize_scores_minmax(values: Sequence[float]) -> list[float]:
    """Normalize one query's scores to [0, 1] while preserving ties."""

    if not values:
        return []
    lower = min(values)
    upper = max(values)
    if upper == lower:
        return [1.0] * len(values)
    return [(value - lower) / (upper - lower) for value in values]


def aggregate_metrics(metrics: Sequence[RetrievalMetrics]) -> Mapping[str, float]:
    """Return macro means for a non-empty metric collection."""

    if not metrics:
        raise ValueError("At least one metric row is required")
    fields = RetrievalMetrics.__dataclass_fields__
    return {
        name: sum(getattr(metric, name) for metric in metrics) / len(metrics) for name in fields
    }
