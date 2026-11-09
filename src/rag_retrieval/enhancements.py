"""Deterministic query-side enhancements for M8 retrieval experiments."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from rag_retrieval.baseline import Chunk

_IDENTIFIER = re.compile(
    r"[A-Za-z][A-Za-z0-9_]*\[\](?:\.[A-Za-z0-9_]+)?"
    r"|[A-Za-z][A-Za-z0-9]*(?:[._/:-][A-Za-z0-9*]+)+"
)

_SOURCE_RULES: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = (
    (
        re.compile(
            r"i062/|centre_ctrl|fpl_plan|icao_route|rfl_string|tas_string|"
            r"route\[\]|is_ato|scat_flights",
            re.IGNORECASE,
        ),
        (
            "scat-dataset-curated",
            "scat-research-guide",
            "aviation-terms-curated",
        ),
    ),
    (
        re.compile(
            r"cmp_|ground_speed_mps|\buav\b|固定翼|组合导航|卡尔曼|异常样本",
            re.IGNORECASE,
        ),
        (
            "uav-research-guide",
            "aviation-terms-curated",
        ),
    ),
    (
        re.compile(
            r"特征.{0,8}顺序|顺序.{0,8}(?:改变|变更)|feature\s+order|"
            r"order.{0,12}(?:change|contract)",
            re.IGNORECASE,
        ),
        (
            "gbar-method-curated",
        ),
    ),
    (
        re.compile(r"\bgbar\b|\bcgdf\b|差分步|绝对状态", re.IGNORECASE),
        (
            "gbar-method-curated",
        ),
    ),
    (
        re.compile(
            r"\bcfpl\b|沿航向|横向|legacy-nearest|插值|裁剪|基线结果",
            re.IGNORECASE,
        ),
        (
            "trajectory-evaluation-curated",
            "scat-research-guide",
        ),
    ),
    (
        re.compile(
            r"prompt[- ]?injection|提示注入|embedded\s+(?:java)?script|内嵌脚本|"
            r"附件|attachment|表单动作|form\s+action|不可信指令|untrusted\s+instruction",
            re.IGNORECASE,
        ),
        (
            "agent-usage-curated",
        ),
    ),
    (
        re.compile(r"\bade\b|\bfde\b|\bmde\b|沿航向|横向|轨迹.{0,4}评估", re.IGNORECASE),
        ("trajectory-evaluation-curated",),
    ),
)


@dataclass(frozen=True, slots=True)
class QueryVariant:
    """One deterministic retrieval query and its RRF contribution weight."""

    text: str
    weight: float
    reason: str


_QUERY_EXPANSION_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"prompt[- ]?injection|提示注入|ignore\s+(?:all|previous)|"
            r"embedded\s+(?:java)?script|内嵌脚本|附件|attachment|表单动作|form\s+action|"
            r"扩大.{0,6}(?:访问|权限)|broaden.{0,12}(?:access|permission)|"
            r"绕过来源登记|status\s*=\s*active",
            re.IGNORECASE,
        ),
        (
            "prompt injection untrusted document instructions embedded JavaScript attachment "
            "form action do not execute do not broaden access scope "
            "提示注入 文档内指令不可信 内嵌脚本 附件 表单动作 不得执行 不得扩大访问范围"
        ),
    ),
    (
        re.compile(
            r"chunk_id.{0,24}(?:稳定|错配|hash|哈希)|"
            r"(?:stable|mismatch).{0,24}chunk[_ -]?id",
            re.IGNORECASE,
        ),
        (
            "stable chunk_id source_id source_version canonical locator JSON text SHA-256 "
            "immutable evidence chunk 稳定块标识 来源版本 规范定位器 正文哈希"
        ),
    ),
    (
        re.compile(
            r"(?:绝对状态|absolute\s+states?).{0,24}(?:差分步|差分|difference)|"
            r"(?:差分步|adjacent\s+(?:interval|difference)).{0,24}(?:绝对状态|states?)",
            re.IGNORECASE,
        ),
        (
            "T absolute states produce T-1 adjacent intervals signed interval differences "
            "D_1:T-1 相邻状态 相邻区间 有符号距离 差分序列"
        ),
    ),
    (
        re.compile(r"沿航向|横向|along[- ]track|cross[- ]track|符号约定|sign convention", re.I),
        (
            "prediction evaluation coordinate sign convention along-track cross-track "
            "预测评测 坐标系 符号约定"
        ),
    ),
)

_CLAUSE_SEPARATOR = re.compile(
    r"(?:\s+(?:and|plus)\s+|并且|以及|同时|并说明|；|;)", re.IGNORECASE
)


def build_query_variants(query: str) -> tuple[QueryVariant, ...]:
    """Build bounded bilingual and composite-query variants.

    The original query always remains the strongest signal.  Expansion phrases are fixed domain
    aliases rather than generated text, so a run is reproducible and cannot execute instructions
    found in a document or query.  Clause variants are only emitted for genuinely composite
    questions and remain lower-weight than the original.
    """

    normalized = " ".join(unicodedata.normalize("NFKC", query).split())
    if not normalized:
        return ()
    variants = [QueryVariant(normalized, 1.0, "original")]
    aliases = [expansion for pattern, expansion in _QUERY_EXPANSION_RULES if pattern.search(normalized)]
    if aliases:
        variants.append(QueryVariant(f"{normalized} {' '.join(aliases)}", 0.45, "controlled-alias"))

    clauses = [part.strip(" ,，。?？") for part in _CLAUSE_SEPARATOR.split(normalized)]
    usable = [part for part in clauses if len(part) >= 8 or len(part.split()) >= 4]
    if len(usable) >= 2:
        variants.extend(QueryVariant(part, 0.30, "composite-clause") for part in usable[:3])

    deduplicated: list[QueryVariant] = []
    seen: set[str] = set()
    for variant in variants:
        key = variant.text.casefold()
        if key not in seen:
            deduplicated.append(variant)
            seen.add(key)
    return tuple(deduplicated)


def extract_identifiers(query: str) -> tuple[str, ...]:
    """Extract stable field/model identifiers without natural-language words."""

    normalized = unicodedata.normalize("NFKC", query)
    identifiers: list[str] = []
    seen: set[str] = set()
    for match in _IDENTIFIER.finditer(normalized):
        value = match.group(0).casefold()
        if value not in seen:
            identifiers.append(value)
            seen.add(value)
    return tuple(identifiers)


def infer_source_ids(query: str) -> tuple[str, ...]:
    """Infer broad source families from domain identifiers in a query."""

    sources: list[str] = []
    seen: set[str] = set()
    for pattern, candidates in _SOURCE_RULES:
        if pattern.search(query):
            for source_id in candidates:
                if source_id not in seen:
                    sources.append(source_id)
                    seen.add(source_id)
    return tuple(sources)


def rank_exact_identifier_matches(
    query: str, chunks: Sequence[Chunk], *, limit: int
) -> list[tuple[str, float]]:
    """Rank chunks by exact identifier coverage for field-card-style lookup."""

    identifiers = extract_identifiers(query)
    if not identifiers or limit <= 0:
        return []
    ranked: list[tuple[str, float]] = []
    for chunk in chunks:
        text = chunk.retrieval_text().casefold()
        exact_terms = {term.casefold() for term in chunk.exact_terms}
        score = 0.0
        for identifier in identifiers:
            if identifier in exact_terms:
                score += 4.0
            if identifier in text:
                score += 1.0
        if score:
            ranked.append((chunk.chunk_id, score))
    ranked.sort(key=lambda item: (-item[1], item[0]))
    return ranked[:limit]


def apply_source_boost(
    ranking: Sequence[tuple[str, float]],
    chunks_by_id: Mapping[str, Chunk],
    source_ids: Sequence[str],
    multiplier: float,
) -> list[tuple[str, float]]:
    """Apply a soft, deterministic boost to inferred sources."""

    if multiplier < 1:
        raise ValueError("source multiplier must be at least 1")
    sources = set(source_ids)
    boosted = [
        (
            chunk_id,
            score * multiplier if chunks_by_id[chunk_id].source_id in sources else score,
        )
        for chunk_id, score in ranking
    ]
    return sorted(boosted, key=lambda item: (-item[1], item[0]))


def source_authority_multipliers(
    query: str, *, levels: Sequence[float] = (2.2, 1.7, 1.35, 1.15)
) -> dict[str, float]:
    """Assign decreasing boosts to the query's ordered authoritative sources."""

    if not levels or any(level < 1 for level in levels):
        raise ValueError("authority multipliers must all be at least 1")
    sources = infer_source_ids(query)
    return {
        source_id: levels[min(index, len(levels) - 1)]
        for index, source_id in enumerate(sources)
    }


def apply_source_authority(
    ranking: Sequence[tuple[str, float]],
    chunks_by_id: Mapping[str, Chunk],
    multipliers: Mapping[str, float],
) -> list[tuple[str, float]]:
    """Apply source-specific authority multipliers without excluding lower-tier evidence."""

    if any(multiplier < 1 for multiplier in multipliers.values()):
        raise ValueError("authority multipliers must all be at least 1")
    reranked = [
        (
            chunk_id,
            score * multipliers.get(chunks_by_id[chunk_id].source_id, 1.0),
        )
        for chunk_id, score in ranking
    ]
    return sorted(reranked, key=lambda item: (-item[1], item[0]))


def expand_with_neighbors(
    ranked_ids: Sequence[str],
    neighbors_by_id: Mapping[str, Sequence[str]],
    *,
    seed_count: int,
) -> list[str]:
    """Interleave same-source adjacent chunks around the highest ranked seeds."""

    if seed_count < 0:
        raise ValueError("seed_count must be non-negative")
    expanded: list[str] = []
    seen: set[str] = set()

    def append(chunk_id: str) -> None:
        if chunk_id not in seen:
            expanded.append(chunk_id)
            seen.add(chunk_id)

    for chunk_id in ranked_ids[:seed_count]:
        append(chunk_id)
        for neighbor_id in neighbors_by_id.get(chunk_id, ()):
            append(neighbor_id)
    for chunk_id in ranked_ids:
        append(chunk_id)
    return expanded
