from rag_retrieval.baseline import Chunk
from rag_retrieval.enhancements import (
    apply_source_authority,
    apply_source_boost,
    build_query_variants,
    expand_with_neighbors,
    extract_identifiers,
    infer_source_ids,
    rank_exact_identifier_matches,
    source_authority_multipliers,
)


def test_extract_identifiers_preserves_domain_fields() -> None:
    assert extract_identifiers("I062/136.measured_flight_level 和 centre_ctrl[]") == (
        "i062/136.measured_flight_level",
        "centre_ctrl[]",
    )


def test_infer_source_ids_combines_matching_domains() -> None:
    sources = infer_source_ids("cmp_ground_speed 与 gbar-align-v1 的 ADE")

    assert "uav-research-guide" in sources
    assert "trajectory-evaluation-curated" in sources


def test_exact_match_ranking_and_source_boost_are_deterministic() -> None:
    chunks = [
        Chunk("a", "other", "v1", (), "cmp_ground_speed is speed", ()),
        Chunk("b", "uav-research-guide", "v1", (), "field", ("cmp_ground_speed",)),
    ]

    ranked = rank_exact_identifier_matches("cmp_ground_speed 的单位？", chunks, limit=10)
    boosted = apply_source_boost(ranked, {chunk.chunk_id: chunk for chunk in chunks}, ["other"], 6)

    assert [chunk_id for chunk_id, _ in ranked] == ["b", "a"]
    assert [chunk_id for chunk_id, _ in boosted] == ["a", "b"]


def test_neighbor_expansion_interleaves_unique_neighbors() -> None:
    expanded = expand_with_neighbors(
        ["a", "b", "c"],
        {"a": ["x", "b"], "b": ["a", "y"]},
        seed_count=2,
    )

    assert expanded == ["a", "x", "b", "y", "c"]


def test_query_variants_keep_original_and_add_bounded_security_aliases() -> None:
    variants = build_query_variants("Can embedded JavaScript broaden access scope?")

    assert variants[0].reason == "original"
    assert variants[0].weight == 1.0
    assert len(variants) == 2
    assert "提示注入" in variants[1].text
    assert variants[1].weight < variants[0].weight


def test_query_variants_split_composite_claims_at_low_weight() -> None:
    variants = build_query_variants(
        "Explain the source status contract and list the generation activation gate"
    )

    assert [variant.reason for variant in variants] == [
        "original",
        "composite-clause",
        "composite-clause",
    ]
    assert all(variant.weight == 0.30 for variant in variants[1:])


def test_source_authority_uses_ordered_tiers_instead_of_uniform_boost() -> None:
    chunks = {
        "primary": Chunk("primary", "scat-dataset-curated", "v1", (), "x", ()),
        "secondary": Chunk("secondary", "scat-research-guide", "v1", (), "x", ()),
        "other": Chunk("other", "other", "v1", (), "x", ()),
    }
    levels = source_authority_multipliers("I062/136 和 centre_ctrl[] 如何解释？")
    ranked = apply_source_authority(
        [("secondary", 1.0), ("primary", 0.8), ("other", 1.2)], chunks, levels
    )

    assert levels["scat-dataset-curated"] > levels["scat-research-guide"]
    assert [chunk_id for chunk_id, _ in ranked] == ["primary", "secondary", "other"]


def test_feature_order_contract_prefers_runtime_contract_source() -> None:
    levels = source_authority_multipliers("六个 GBAR 特征的顺序可以随意改变吗？")

    assert levels["gbar-method-curated"] >= 1.0
