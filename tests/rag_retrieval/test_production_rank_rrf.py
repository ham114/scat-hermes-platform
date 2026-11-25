"""Production ordering must reproduce the frozen experiment, including ties."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from fusion_policy_experiments import POLICIES as FUSION, select
from rerank_postprocess_policies import POLICIES as POST, rerank
from rag_retrieval.baseline import Chunk
from rag_retrieval.candidates import WEIGHTED_POLICY, select_candidates
from rag_retrieval.ranking import RRF_RANKING, rank_candidates
from hermes_runtime.entrypoint import apply_rag_switch
from rag_service_enabled import enabled
from run_container_service import rag_arguments


@pytest.mark.parametrize("query", ["aviation", "CAT062", "I062/136"])
def test_production_candidates_match_frozen_weighted2(query):
    dense = [f"d{i}" for i in range(200)]
    lexical = dense[40:80] + [f"l{i}" for i in range(160)]
    exact = lexical[:30]
    chunks = {cid: Chunk(cid, "scat-dataset-curated" if cid.startswith("l") else "official", "v7", (), cid)
              for cid in set(dense + lexical)}
    profile = {"candidate_k": 200, "rrf_k": 40, "exact_weight": 1., "source_multiplier": 2.2}
    expected = select(next(p for p in FUSION if p['id'] == 'weighted2'),
                      dense, lexical, exact, chunks, query, profile)
    result = select_candidates(query=query, dense=dense, lexical=lexical, exact=exact,
                               chunks_by_id=chunks, profile=profile, policy=WEIGHTED_POLICY)
    assert list(result.ids) == expected
    assert len(result.ids) == len(set(result.ids)) == 100


@pytest.mark.parametrize("scores", [[.5] * 100, [i / 100 for i in range(100)], [((i * 37) % 100) / 100 for i in range(100)]])
def test_production_final_ranking_matches_frozen_formula(scores):
    pool = [f"c{i}" for i in range(100)]
    expected, _ = rerank(next(p for p in POST if p['id'] == 'rank_rrf'), query='question',
                         pool=pool, scores=dict(zip(pool, scores)), records={c: {} for c in pool})
    assert rank_candidates(pool, scores, RRF_RANKING) == expected


@pytest.mark.parametrize("value", [False, True])
def test_config_switch_controls_native_opt_in_and_service_condition(tmp_path, value):
    path = tmp_path / 'rag.json'
    path.write_text(json.dumps({'enabled': value}))
    args = SimpleNamespace(rag_config='rag.json')
    assert apply_rag_switch(args, tmp_path).rag_config == ('rag.json' if value else None)
    assert enabled(tmp_path, 'rag.json') is value


@pytest.mark.parametrize("value", ['false', 0, None])
def test_invalid_switch_cannot_accidentally_enable_rag(tmp_path, value):
    (tmp_path / 'rag.json').write_text(json.dumps({'enabled': value}))
    with pytest.raises(ValueError):
        apply_rag_switch(SimpleNamespace(rag_config='rag.json'), tmp_path)
    with pytest.raises(ValueError):
        enabled(tmp_path, 'rag.json')


def test_saved_configs_disable_both_rag_paths():
    root = Path(__file__).resolve().parents[2]
    for name in ['qwen3-production-v1.json', 'real-agent-production-v1.json']:
        assert not enabled(root, 'services/rag/' + name)


@pytest.mark.parametrize("value", [True, False, 'false'])
def test_container_entrypoint_honors_the_same_native_switch(tmp_path, value):
    path = tmp_path / 'services/rag/real-agent-production-v1.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'enabled': value}))
    if type(value) is not bool:
        with pytest.raises(ValueError):rag_arguments(tmp_path)
    else:
        assert rag_arguments(tmp_path) == (['--rag-config', 'services/rag/real-agent-production-v1.json'] if value else [])
