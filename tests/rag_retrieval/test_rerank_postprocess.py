import sys
from pathlib import Path
import numpy as np
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
from rerank_postprocess_policies import POLICIES,rerank,explicit_sources


def apply(policy, pool=None, scores=None, sources=None, query='question', sim=None):
    pool=pool or ['a','b','c','d']
    scores=scores or {'a':.99,'b':.9,'c':.89,'d':.05}
    records={cid:{'source_id':sid} for cid,sid in zip(pool,sources or ['one','one','two','three'])}
    return rerank(policy,query=query,pool=pool,scores=scores,records=records,
                  similarities=sim if sim is not None else np.eye(len(pool)))


@pytest.mark.parametrize('policy',POLICIES)
def test_every_rule_returns_deterministic_permutation(policy):
    a,_=apply(policy);b,_=apply(policy)
    assert a==b and set(a)=={'a','b','c','d'} and len(a)==4
    if policy.get('protect_top1'):assert a[0]=='a'


def test_baseline_and_ties_keep_original_pool_order():
    assert apply(POLICIES[0],pool=['c','b','a'],scores={'a':.8,'b':.8,'c':.8},sources=['x']*3)[0]==['c','b','a']


def test_mmr_reduces_duplicate_before_complementary_evidence():
    sim=np.eye(4);sim[0,1]=sim[1,0]=.99
    assert apply(POLICIES[2],sim=sim)[0][:2]==['a','c']


def test_irrelevant_diversity_does_not_override_score_floor():
    result,_=apply(POLICIES[2],scores={'a':.99,'b':.9,'c':.01,'d':.001})
    assert result[:2]==['a','b']


def test_soft_cap_backfills_when_only_one_source_is_available():
    assert apply(POLICIES[3],sources=['one']*4)[0]==['a','b','c','d']


def test_explicit_source_aliases_use_query_not_labels():
    assert explicit_sources('Compare FAA AIM with PHAK and ERA5')==['faa-aim','faa-phak','ecmwf-era5-documentation']
    result,info=apply(POLICIES[4],query='PHAK and AIM',sources=['faa-phak','faa-phak','faa-aim','other'])
    assert result[:2]==['a','c'] and info['reserved_source_candidates']==['c']
    assert apply(POLICIES[4],query='PHAK only')[0]==apply(POLICIES[2])[0]


def test_zeros_fall_back_and_invalid_data_is_rejected():
    assert apply(POLICIES[2],scores={k:0. for k in 'abcd'})[0]==list('abcd')
    with pytest.raises(ValueError):apply(POLICIES[0],scores={'a':float('nan'),'b':.2,'c':.1,'d':0})
    with pytest.raises(ValueError):apply({'id':'label_tuned'})
