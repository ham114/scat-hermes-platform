"""Deterministic evidence-only retrieval measures; no model or API dependency."""
def rank_map(ids):
 if len(set(ids))!=len(ids):raise ValueError('duplicate ranked chunk')
 return {cid:i+1 for i,cid in enumerate(ids)}

def coverage(ids, gold, groups, depths=(5,10,20,50,100)):
 positions=rank_map(ids);gold=set(gold)
 if not gold:raise ValueError('empty gold')
 result={}
 for k in depths:
  found=set(ids[:k]);hits=len(found&gold)
  result[str(k)]={'evidence_hits':hits,'evidence_total':len(gold),'evidence_recall':hits/len(gold),'any_evidence':bool(hits),'all_evidence':gold<=found,
   'claim_group_coverage':sum(set(g)<=found for g in groups)/len(groups) if groups else None}
 result['first_evidence_rank']=min((positions[c] for c in gold if c in positions),default=None)
 result['complete_evidence_rank']=max(positions[c] for c in gold) if gold<=set(positions) else None
 return result

def loss_stage(ranks, *, leaf_depth=200, pool_depth=100, delivery_depth=5):
 """Assign a first retrieval loss, separately from the pure-dense diagnostic."""
 if not any(ranks.get(k) is not None and ranks[k]<=leaf_depth for k in ['dense','bm25','exact']):return 'leaf_recall_miss_200'
 if ranks.get('rrf_union') is None or ranks['rrf_union']>leaf_depth:return 'rrf_cut_200'
 if ranks.get('boosted') is None or ranks['boosted']>pool_depth:
  if ranks['rrf_union']<=pool_depth:return 'source_boost_pushes_out_100'
  return 'candidate_cut_100'
 if ranks.get('reranked') is None:raise ValueError('candidate lost by reranker')
 if ranks['reranked']>delivery_depth:return 'reranker_below_delivery_'+str(delivery_depth)
 return 'delivered_'+str(delivery_depth)
