"""Extract reviewed historical questions, shield tests, group splits, screen negatives."""
import collections,json,math,random,sys,time
from pathlib import Path
from reranker_ft_common import *

def main():
 offline();OUT.mkdir(parents=True,exist_ok=True)
 assert not (OUT/'plan.json').exists(),'final experiment already frozen'
 import numpy as np
 from rag_retrieval.production import Retriever,load_config
 from fusion_policy_experiments import POLICIES as FUSION,select
 from rag_retrieval.runtime import M8_RRF_PROFILE
 from rag_retrieval.enhancements import rank_exact_identifier_matches
 cfg,chunks,rs=corpus();ids=list(rs);idx={c:i for i,c in enumerate(ids)}
 files=[ROOT/'evaluations/rag_multihop120_candidates_20261124_v1/evaluation.json',ROOT/'evaluations/rag_aviation300_reviewed_20261124_v2/evaluation.json']
 tests=read(BASE_EVAL/'dataset.json')['cases'];test_ids={c for u in tests for c in u['gold']}
 for v in read(BASE_EVAL/'quote-compatible-alternatives.json').values():test_ids.update(v)
 test_hashes={rs[c]['text_sha256']for c in test_ids}
 excluded=[];candidates=[];seen=set();all_gold=set(test_ids)
 for file in files:
  for c in read(file)['cases']:
   es=[e for cl in c['claims']for e in cl['evidence']];gold=sorted({e['chunk_id']for e in es});all_gold.update(gold)
   why=None
   if c.get('retrieval_gold_status')=='broad_source_context_not_minimal_row_gold':why='broad_context_gold'
   elif __import__('re').search(r'页码标注|page number|which page',c['question'],__import__('re').I):why='page_location_only_query'
   elif not 1<=len(gold)<=4:why='positive_context_count_outside_1_to_4'
   elif any(e['chunk_id']not in rs or e['quote']not in rs[e['chunk_id']]['text']for e in es):why='evidence_missing_or_quote_drift'
   elif any('$$'in rs[x]['text']for x in gold):why='unverified_equation_in_positive'
   elif set(gold)&test_ids or any(rs[x]['text_sha256']in test_hashes for x in gold):why='test_evidence_overlap'
   elif norm(c['question'])in seen:why='duplicate_historical_question'
   if why:excluded.append({'id':c['qid'],'reason':why});continue
   seen.add(norm(c['question']));qs=collections.defaultdict(set)
   for e in es:qs[e['chunk_id']].add(e['quote'])
   equiv=set(gold)
   for cid,quotes in qs.items():
    a=rs[cid]
    equiv.update(b['chunk_id']for b in rs.values()if a['source_id']==b['source_id']and a['section_path'][-1:]==b['section_path'][-1:]and all(q in b['text']for q in quotes))
   if equiv&test_ids:excluded.append({'id':c['qid'],'reason':'test_quote_alias_overlap'});continue
   all_gold.update(equiv)
   candidates.append({'id':c['qid'],'question':c['question'],'category':c['category'],'language':c.get('language','zh-CN'),'gold':gold,'equivalent_ids':sorted(equiv),'groups':c['supporting_evidence_groups'],
    'evidence':[{'chunk_id':e['chunk_id'],'quote':e['quote'],'current_text_sha256':rs[e['chunk_id']]['text_sha256'],'old_source_version':e.get('source_version'),'current_source_version':rs[e['chunk_id']]['source_version']}for e in es]})
 engine=Retriever(ROOT,load_config(ROOT,'services/rag/qwen3-production-v1.json'));vectors=np.asarray(engine.index.corpus_vectors)
 all_queries=[c['question']for c in candidates]+[c['question']for c in tests]
 qvectors=engine.index.model.encode(all_queries,batch_size=8,normalize_embeddings=True,show_progress_bar=False,convert_to_numpy=True)
 n=len(candidates);test_sim=qvectors[:n]@qvectors[n:].T;kept=[];kv=[]
 for j,c in enumerate(candidates):
  k=int(test_sim[j].argmax());score=float(test_sim[j,k])
  if score>=.85 or norm(c['question'])in {norm(t['question'])for t in tests}:excluded.append({'id':c['id'],'reason':'test_query_similarity','cosine':score,'test_id':tests[k]['id']});continue
  kept.append(c);kv.append(qvectors[j])
 qvectors=np.asarray(kv);candidates=kept;n=len(kept)
 # Group connected gold aliases and near-identical questions before any split.
 parent=list(range(n))
 def find(a):
  while parent[a]!=a:parent[a]=parent[parent[a]];a=parent[a]
  return a
 def union(a,b):parent[find(a)]=find(b)
 owners={}
 for j,c in enumerate(candidates):
  for cid in c['equivalent_ids']:
   if cid in owners:union(j,owners[cid])
   owners[cid]=j
 sim=qvectors@qvectors.T
 for j in range(n):
  for k in range(j):
   if float(sim[j,k])>=.92:union(j,k)
 components=collections.defaultdict(list)
 for j,c in enumerate(candidates):components[find(j)].append(j)
 components=sorted(components.values(),key=lambda v:min(candidates[j]['id']for j in v));rng=random.Random(SETTINGS['seed']);rng.shuffle(components)
 dev=set();target=round(n*.2)
 for comp in components:
  if len(dev)<target:dev.update(comp)
 protected_hashes={rs[c]['text_sha256']for c in all_gold if c in rs}
 dtags={cid:tags(' '.join(r['section_path'])+' '+r['text'])for cid,r in rs.items()}
 filtered=[];dev_pools=[];negative_review=[];metadata=[]
 official_sources={rs[x]['source_id']for g in candidates for x in g['gold']}
 for j,c in enumerate(candidates):
  split='dev'if j in dev else 'train';q=c['question'];positive=c['gold'];qtags=tags(q)
  for cid in positive:qtags|=dtags[cid]
  similarity=vectors@qvectors[j];ranked=np.argsort(-similarity,kind='stable');rankmap={int(pos):k+1 for k,pos in enumerate(ranked)}
  pos_sections={(rs[x]['source_id'],tuple(rs[x]['section_path']))for x in positive};posv=vectors[[idx[x]for x in positive]]
  candidates_neg=[]
  for pos in ranked:
   pos=int(pos);cid=ids[pos];r=rs[cid]
   if rankmap[pos]<=20 or r['source_id']not in official_sources:continue # Highest unlabeled matches and private sources are shielded.
   if any('table of contents'in s.lower()for s in r['section_path']):continue
   identifiers=set(__import__('re').findall(r'\b[A-Z][A-Z0-9_]{1,}\b',q))-{'FAA','WMO','ICAO','PHAK','AIM','AWH','IFH','PCG','US','U','S'}
   if any(__import__('re').search(r'\b'+__import__('re').escape(x)+r'\b',r['text'])for x in identifiers):continue
   if any(abs(pos-idx[x])<=2 for x in positive):continue
   if negative_allowed(candidate=r,protected_ids=all_gold,protected_hashes=protected_hashes,positive_sections=pos_sections,query_tags=qtags,candidate_tags=dtags[cid],positive_similarity=float((posv@vectors[pos]).max())):
    candidates_neg.append(cid)
   if len(candidates_neg)>=200:break
  neg=[]
  same=next((x for x in candidates_neg if rs[x]['source_id']in {rs[y]['source_id']for y in positive}),None)
  if same:neg.append(same)
  neg += [x for x in candidates_neg if x not in neg][:2]
  easy=sorted([x for x in candidates_neg if x not in neg],key=lambda x:sha_text(c['id']+x))
  neg += easy[:4-len(neg)]
  # Verify production's exact text budget before exporting any record.
  from rag_retrieval.rerank_comparison import Qwen3Reranker
  prefix=engine.reranker.prefix;suffix=engine.reranker.suffix
  def fits(cid):
   t=f'<Instruct>: {Qwen3Reranker.INSTRUCTION}\n<Query>: {q}\n<Document>: {chunks[cid].retrieval_text()}'
   return len(engine.reranker.tokenizer.encode(t,add_special_tokens=False))+len(prefix)+len(suffix)<=2048
  if len(neg)<4 or not all(fits(x)for x in positive+neg):excluded.append({'id':c['id'],'reason':'insufficient_screened_negatives_or_length'});continue
  g={**c,'split':split,'negatives':[{'chunk_id':cid,'kind':'mechanically_screened_off_topic','weight':.5,'bge_rank':rankmap[idx[cid]],'query_cosine':float(similarity[idx[cid]]),'topics':sorted(dtags[cid]),'positive_topics':sorted(qtags)}for cid in neg],
     'negative_label_limit':'weak screened negatives; no LLM or expert relevance judgment; unlabeled top20, all known positives and same topics excluded','hard_negative_positions':[len(positive)+k for k,cid in enumerate(neg)if cid in candidates_neg[:3]]}
  if not g['hard_negative_positions']:g['hard_negative_positions']=[len(positive)]
  if split=='dev'and not all(fits(x)for x in select(next(p for p in FUSION if p['id']=='weighted2'),[ids[int(k)]for k in ranked[:200]],[x for x,_ in engine.index.bm25.rank(' '.join(q.split()),limit=200)],[x for x,_ in rank_exact_identifier_matches(q,engine.index.chunks,limit=200)],engine.index.chunks_by_id,' '.join(q.split()),M8_RRF_PROFILE)):
   excluded.append({'id':c['id'],'reason':'dev_candidate_would_truncate'});continue
  filtered.append(g);metadata.append({'id':c['id'],'component':find(j),'split':split})
  if split=='dev':
   dense=[ids[int(k)]for k in ranked[:200]];lex=[x for x,_ in engine.index.bm25.rank(' '.join(q.split()),limit=200)];exact=[x for x,_ in rank_exact_identifier_matches(q,engine.index.chunks,limit=200)]
   pool=select(next(p for p in FUSION if p['id']=='weighted2'),dense,lex,exact,engine.index.chunks_by_id,' '.join(q.split()),M8_RRF_PROFILE)
   dev_pools.append({'id':c['id'],'cohort':'dev','category':c['category'],'question':q,'gold':positive,'groups':c['groups'],'pool':pool})
  else:
   for item in g['negatives']:
    negative_review.append({'id':c['id'],'query':q,'positive_ids':positive,'negative':item,'negative_section':rs[item['chunk_id']]['section_path'],'negative_excerpt':rs[item['chunk_id']]['text'][:700]})
  if j%20==0:save(OUT/'progress.json',{'stage':'data_mining','completed':j,'total':n,'retained':len(filtered)})
 train=[g for g in filtered if g['split']=='train'];validation=[g for g in filtered if g['split']=='dev']
 assert len(train)>=60 and len(validation)>=15
 train_e={cid for c in train for cid in c['equivalent_ids']};dev_e={cid for c in validation for cid in c['equivalent_ids']}
 assert not train_e&dev_e and not train_e&test_ids and not dev_e&test_ids
 for split,groups in [('train',train),('dev',validation)]:
  (OUT/(split+'.jsonl')).write_text(''.join(json.dumps(g,ensure_ascii=False)+'\n'for g in groups),encoding='utf8')
 save(OUT/'dev-pools.json',dev_pools);save(OUT/'exclusions.json',excluded);save(OUT/'split-components.json',metadata)
 negative_review.sort(key=lambda r:(r['negative']['bge_rank'],r['id']));save(OUT/'negative-review-sample.json',negative_review[:32])
 # RAG-FiT's native processing operates only on query/document + yes/no, never QA answers.
 pairs=[]
 for g in train:
  for cid in g['gold']:pairs.append({'id':g['id'],'query':g['question'],'document':chunks[cid].retrieval_text(),'answer':'yes','kind':'annotated_partial_or_complete_evidence'})
  for item in g['negatives']:pairs.append({'id':g['id'],'query':g['question'],'document':chunks[item['chunk_id']].retrieval_text(),'answer':'no','kind':item['kind']})
 (OUT/'ragfit-pairs.jsonl').write_text(''.join(json.dumps(p,ensure_ascii=False)+'\n'for p in pairs),encoding='utf8')
 template=engine.reranker.PREFIX+'<Instruct>: '+engine.reranker.INSTRUCTION+'\n<Query>: {query}\n<Document>: {document}'+engine.reranker.SUFFIX
 (OUT/'reranker-prompt.txt').write_text(template,encoding='utf8')
 config=f'''name: reranker_relevance_pairs
cache: false
output_path: {OUT}/ragfit-processed
steps:
  - _target_: ragfit.processing.dataset_loaders.loaders.LocalLoader
    inputs: main
    filename: {OUT}/ragfit-pairs.jsonl
  - _target_: ragfit.processing.local_steps.prompter.TextPrompter
    inputs: main
    prompt_file: {OUT}/reranker-prompt.txt
    output_key: prompt
    mapping:
      query: query
      document: document
  - _target_: ragfit.processing.global_steps.output.OutputData
    inputs: main
    prefix: relevance
    directory: ${{output_path}}
'''
 (OUT/'processing-reranker.yaml').write_text(config,encoding='utf8')
 protected=[*files,ROOT/'services/rag/qwen3-production-v1.json',ROOT/'services/rag/real-agent-production-v1.json',Path(cfg['corpus']),Path(cfg['embedding_cache']),ROOT/cfg['model_manifest'],
  BASE_EVAL/'dataset.json',BASE_EVAL/'summary.json',BASE_EVAL/'preregistration.json',ROOT/'var/validation/20261125-ragas-holdout120/candidates.jsonl',ROOT/'var/validation/20261125-ragas-holdout120/calls.sqlite',
  ROOT/'src/rag_retrieval/baseline.py',ROOT/'src/rag_retrieval/production.py',ROOT/'src/rag_retrieval/runtime.py',ROOT/'scripts/rerank_postprocess_policies.py']
 draft={'status':'data_prepared_not_training','schemes':SCHEMES,'settings':SETTINGS,'train_queries':len(train),'dev_queries':len(validation),'train_positive_pairs':sum(len(g['gold'])for g in train),'train_negative_pairs':4*len(train),'ragfit_rows':len(pairs),
  'excluded_by_reason':dict(collections.Counter(x['reason']for x in excluded)),'source_inputs':{f.relative_to(ROOT).as_posix():sha(f)for f in files},
  'protected_sha256':{p.relative_to(ROOT).as_posix():sha(p)for p in protected},'data_sha256':{n:sha(OUT/n)for n in ['train.jsonl','dev.jsonl','dev-pools.json','ragfit-pairs.jsonl','processing-reranker.yaml','reranker-prompt.txt']},
  'split_guard':{'train_dev_positive_alias_overlap':0,'test_positive_alias_overlap':0,'heldout_queries':233,'query_test_cosine_reject_at':.85,'query_component_join_at':.92},
  'historical_repurposing_authorized':True,'historical_not_for_training_flag_not_modified':True,'test_labels_used_only_for_exclusion_and_later_evaluation':True,
  'checkpoint_rule':'fixed one epoch adapter-last for all three objectives; dev decides eligibility, no test-based checkpoint/parameter selection',
  'negative_policy':'All known positive IDs/text hashes and test aliases excluded; top20, same topics, adjacent positives, same sections and positive cosine>=.8 excluded. Screened negatives remain weak labels(weight .5); group objectives use same corpus and split.',
  'ragfit_reuse':'native LocalLoader/TextPrompter/OutputData, legacy training environment untouched; dedicated scorer objectives use existing scat-rag-train PEFT environment',
  'external_model_calls':0,'generation_calls':0,'production_changes':0}
 save(OUT/'draft-plan.json',draft);print(json.dumps({k:draft[k]for k in ['train_queries','dev_queries','train_positive_pairs','train_negative_pairs','excluded_by_reason']},ensure_ascii=False),flush=True)

def sha_text(s):return __import__('hashlib').sha256(s.encode()).hexdigest()
if __name__=='__main__':main()
