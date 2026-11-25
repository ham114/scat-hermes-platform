import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
from evidence_retrieval_metrics import coverage,loss_stage

def test_two_required_chunks_and_depth_boundaries():
 ids=['a']+[f'n{i}' for i in range(98)]+['b']
 r=coverage(ids,['a','b'],[['a'],['b']])
 assert r['5']['evidence_recall']==.5 and not r['50']['all_evidence'] and r['100']['all_evidence']
 assert r['100']['claim_group_coverage']==1 and r['complete_evidence_rank']==100

def test_late_rerank_is_not_a_dense_miss():
 r={'dense':150,'bm25':3,'exact':None,'rrf_union':4,'boosted':4,'reranked':90}
 assert loss_stage(r)=='reranker_below_delivery_5'

def test_fusion_and_source_boost_have_separate_losses():
 r={'dense':10,'bm25':None,'exact':None,'rrf_union':250,'boosted':None,'reranked':None}
 assert loss_stage(r)=='rrf_cut_200'
 r.update(rrf_union=70,boosted=130)
 assert loss_stage(r)=='source_boost_pushes_out_100'
 r.update(rrf_union=130,boosted=140)
 assert loss_stage(r)=='candidate_cut_100'

def test_all_leaf_branches_must_miss_for_leaf_loss():
 r={'dense':300,'bm25':None,'exact':None,'rrf_union':None,'boosted':None,'reranked':None}
 assert loss_stage(r)=='leaf_recall_miss_200'
 r.update(bm25=12,rrf_union=3,boosted=3,reranked=2)
 assert loss_stage(r)=='delivered_5'
