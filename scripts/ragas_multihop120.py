"""Focused v7 multi-evidence test generation using native installed Ragas."""
import asyncio,collections,copy,hashlib,json,os,random,re,socket,ssl,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
import ragas_multihop120_core as core
OUT=core.OUT;DEST=ROOT/'output/ragas-multihop120-20261124'
QUOTAS={'faa-aim':36,'faa-instrument-flying-handbook':22,'faa-phak':18,'faa-aviation-weather-handbook':16,'wmo-no8-volume1':12,'faa-pilot-controller-glossary':6,'eurocontrol-asterix-cat062':6,'ecmwf-era5-documentation':4}
TYPES={'cross_document':60,'cross_chapter':30,'numeric_conditions':20,'insufficient':10}
LIMITS={'max_requests':1000,'max_provider_tokens':8000000,'max_conservative_units':40000000,'wall_seconds':7200,'max_output_tokens':2400}
HINT='''Create ONE coherent two-evidence aviation interpretation or decision problem. BOTH contexts must be indispensable in a genuine dependency: applying a definition to a different rule, joining an operating condition to an exception, or combining an encoding/calculation with its datum or applicability. Do NOT glue two independent lookups together. Do NOT merely ask why one should read a glossary plus a term definition. No operational conclusions beyond the document's jurisdiction. State source names/editions when their identity is needed; never say just "this glossary". Preserve prerequisites and row/column meanings. Ask only the minimum necessary points; do not make optional examples mandatory.'''
def setup():
 core.QUOTAS=QUOTAS;core.TYPES=TYPES
 core.TYPE_HINTS={
  'cross_document':HINT+' Use two different document families to solve the same concrete interpretation problem.',
  'cross_chapter':HINT+' Require two distinct sections of the same named document. Naturally include explicit section identifiers where useful for precise navigation.',
  'numeric_conditions':HINT+' Require an actual grounded quantitative result, encoding conversion, bound or table value AND the applicable conditions/datum from the other excerpt. Provide every calculation input in the question. Never transplant a worked example with different temperature/height/weight. Do not infer missing printed numbers.',
  'insufficient':HINT+' Provide the known rule/measurement from both excerpts but deliberately omit one specific instance input. Ask what can and cannot be concluded and name the missing input. Do not claim real-world nonexistence or manufacture conflicts.'}
 core.REVIEW+='''
All four requested categories in THIS batch are multi-evidence questions. Reject if the two parts could be answered independently as unrelated lookups; reject if either context can be removed without losing an indispensable step of the SAME problem. Add dependency_test: {bridge_relation: a specific explanation of the logical connection, without_first_unanswerable:bool, without_second_unanswerable:bool, parallel_independent_questions:bool}. This is a reasoned model review, NOT an empirical ablation experiment. Show the distinct necessary contribution of each context. For numerical questions check inputs, formula, arithmetic, units, datum, rounding and prerequisites. Reject ambiguous references to unnamed books, fake contradictions and unsupported aircraft capability conclusions.'''
 core.select_rows=select_rows

def eligible(r):
 title=' '.join(r['section_path']).lower();text=r['text']
 return not any(s in title for s in ['contents','list of figures','list of tables','acknowledg','bibliograph','references cited','acronyms, abbreviations']) and not any(s in text for s in ['SOURCE ANOMALY','Table extraction unavailable','Unavailable source rows','contents navigation only']) and len(text)>230 and r['token_count']>=65

def select_rows(slot,attempt,pools,byid,vectors,used):
 import numpy as np
 pool=pools[slot['primary_source']];offset=(slot['slot']*17+attempt*23)%len(pool);ordered=pool[offset:]+pool[:offset]
 if slot['category']=='numeric_conditions':
  numeric=[r for r in ordered if re.search(r'\d',byid[r['chunk_id']]['text']) and re.search(r'\b(ft|feet|FL|LSB|hPa|knots?|kg|Kelvin|mm|m/s|percent|W/m|temperature|altitude|pressure)\b',byid[r['chunk_id']]['text'],re.I)]
  if numeric:ordered=numeric
 first=next(r for r in ordered if r['chunk_id'] not in used)
 candidates=[r for group in pools.values() for r in group if r['chunk_id'] not in used and r['chunk_id']!=first['chunk_id'] and
    (r['source_id']!=first['source_id'] if slot['require_cross_source'] else r['source_id']==first['source_id']) and r['section']!=first['section']]
 indices=np.array([r['index'] for r in candidates]);scores=vectors[indices]@vectors[first['index']]
 order=np.argsort(-scores,kind='stable');choices=[candidates[j] for j in order if 0.35<float(scores[j])<0.88][:24]
 if not choices:raise ValueError('No genuinely related distinct-context candidate')
 second=choices[(slot['slot']+attempt)%min(10,len(choices))]
 used.update([first['chunk_id'],second['chunk_id']]);return [byid[first['chunk_id']],byid[second['chunk_id']]]

def prepare():
 import numpy as np
 if (OUT/'plan.json').exists():raise ValueError('frozen plan exists')
 allrows,rows=core.load_rows();pools={s:[] for s in QUOTAS}
 for i,r in enumerate(allrows):
  if r['source_id'] in pools and eligible(r):pools[r['source_id']].append({'chunk_id':r['chunk_id'],'index':i,'source_id':r['source_id'],'section':r['section_path'][-3:]})
 rng=random.Random(20261124120)
 for pool in pools.values():rng.shuffle(pool)
 assert all(len(pool)>=30 for pool in pools.values())
 tasks=[]
 for category,n in TYPES.items():
  for j in range(n):tasks.append({'category':category,'require_cross_source':category=='cross_document' or category in ['numeric_conditions','insufficient'] and j<n//2})
 rng.shuffle(tasks);remaining=dict(QUOTAS)
 slots=[]
 for i,task in enumerate(tasks):
  source=max((s for s in QUOTAS if remaining[s]),key=lambda s:(remaining[s]/QUOTAS[s],remaining[s],s));remaining[source]-=1
  slots.append({'slot':i+1,**task,'primary_source':source,'language':'en' if i%5==4 else 'zh-CN'})
 old=[]
 for folder in ['rag_p0_reviewed_20261123_v1','rag_p0_reviewed_20261124_v2','rag_aviation300_reviewed_20261124_v1','rag_aviation300_reviewed_20261124_v2']:
  for name in ['evaluation.json','regression.json']:
   path=ROOT/'evaluations'/folder/name
   if not path.exists():continue
   for c in core.read(path)['cases']:
    old.append(c['question']);old.extend(t['question'] for t in c.get('turns',[]))
 old=list(dict.fromkeys(old))
 core.save(OUT/'old-question-fingerprints.json',[{'question':q,'normalized':core.norm(q)} for q in old]);core.save(OUT/'selection-pools.json',pools);core.save(OUT/'slots.json',slots)
 vectors=np.load(core.CORPUS/'bge-m3.npy',mmap_mode='r');assert vectors.shape==(8919,1024)
 used=set();byid={r['chunk_id']:r for r in rows};scenarios=[]
 for slot in slots:
  chosen=select_rows(slot,1,pools,byid,vectors,used)
  assert len(chosen)==2 and len({r['source_id'] for r in chosen})==(2 if slot['require_cross_source'] else 1)
  scenarios.append({'slot':slot['slot'],'chunks':[r['chunk_id'] for r in chosen]})
 core.save(OUT/'planned-scenarios.json',scenarios)
 plan={'target':120,'corpus_generation':core.CORPUS.name,'corpus_sha256':core.SHA,'source_quotas':QUOTAS,'category_targets':TYPES,
   'languages':{'zh-CN':96,'en':24},'minimum_cross_document':75,'all_require_two_indispensable_contexts':True,
   'native_generation':'Ragas 0.4.3 MultiHopSpecificQuerySynthesizer.generate_sample; 120 two-node scenarios',
   'old_question_dedup_count':len(old),'old_questions_local_only':True,'scope':'generation, source-grounded initial review and duplicate checks only; no QA scoring, training or production changes',
   'limits':LIMITS,'destination':'https://api.deepseek.com','key':'existing Ragas dedicated key remains on server',
   'source_exclusions':['TOCs and acronym lists','unavailable extraction notices','six flagged published takeoff values','private project documents'],
   'review_status':'model construction review and assistant source checks; not independent human benchmark admission','external_model_calls':0}
 core.save(OUT/'plan.json',plan);DEST.mkdir(parents=True,exist_ok=True);core.save(DEST/'plan.json',plan)
 print(json.dumps({'target':120,'eligible':sum(map(len,pools.values())),'planned_cross_source':sum(s['require_cross_source'] for s in slots),'prior_questions':len(old),'calls':0}),flush=True)

def preflight():
 from ragas.testset.graph import Node,NodeType
 from ragas.testset.persona import Persona
 from ragas.testset.synthesizers.multi_hop.base import MultiHopScenario
 from ragas.testset.synthesizers.base import QueryStyle,QueryLength
 import importlib.metadata
 key,model=core.credentials();allrows,rows=core.load_rows();byid={r['chunk_id']:r for r in rows}
 sample=core.read(OUT/'planned-scenarios.json')[0];nodes=[Node(type=NodeType.CHUNK,properties={'page_content':byid[cid]['text']}) for cid in sample['chunks']]
 scenario=MultiHopScenario(nodes=nodes,combinations=['rule','applicability'],persona=Persona(name='Aviation analyst',role_description='Combine official evidence with correct conditions'),style=QueryStyle.PERFECT_GRAMMAR,length=QueryLength.MEDIUM)
 assert len(scenario.nodes)==2
 address='42.177.83.94'
 try:address=socket.gethostbyname('api.deepseek.com')
 except OSError:pass
 with socket.create_connection((address,443),timeout=12) as raw:
  with ssl.create_default_context().wrap_socket(raw,server_hostname='api.deepseek.com') as tls:assert tls.getpeercert()
 files=[Path(__file__),ROOT/'scripts/ragas_multihop120_core.py',ROOT/'scripts/ragas_aviation_budget.py']
 result={'status':'passed','ragas_version':importlib.metadata.version('ragas'),'scenarios_checked':120,'key_present':bool(key),'model':model,'verified_dns_address':address,'tls_hostname_verified':True,'source_sha256':{p.name:core.sha(p) for p in files},'plan_sha256':core.sha(OUT/'plan.json'),'model_calls':0}
 core.save(OUT/'preflight.json',result);core.save(DEST/'preflight.json',result);print(json.dumps(result),flush=True)

def validate_campaign():
 assert core.read(DEST/'user-approval.json')['authorized'] is True
 pre=core.read(OUT/'preflight.json');assert pre['plan_sha256']==core.sha(OUT/'plan.json')
 for name,digest in pre['source_sha256'].items():assert core.sha(ROOT/'scripts'/name)==digest
 return pre

def generate():
 pre=validate_campaign()
 assert not (OUT/'policy.json').exists(),'existing campaign preserved'
 now=time.time();core.save(OUT/'policy.json',{'started_at':now,'expires_at':now+7200,**{k:v for k,v in LIMITS.items() if k!='wall_seconds'},
   'data_destination':'https://api.deepseek.com','model':'deepseek-flash','corpus_sha256':core.SHA,'verified_dns_address':pre['verified_dns_address'],
   'source_sha256':{p.name:core.sha(p) for p in [ROOT/'scripts/ragas_multihop120_core.py',ROOT/'scripts/ragas_aviation_budget.py']},
   'plan_sha256':core.sha(OUT/'plan.json'),'private_project_documents_sent':False,'qa_evaluation':False,'old_questions_sent':False})
 asyncio.run(core.run(int(sys.argv[2]) if len(sys.argv)>2 else 0))

def resume():
 validate_campaign();assert (OUT/'policy.json').exists()
 asyncio.run(core.run(int(sys.argv[2]) if len(sys.argv)>2 else 0))

def refine():
 validate_campaign();request=core.read(OUT/'refinement-request.json');state=core.read(OUT/'state.json')
 mapping={r['qid']:r['reason'] for r in request};assert mapping and all(len(r)>12 for r in mapping.values())
 removed=[c for c in state['accepted'] if c['qid'] in mapping];assert len(removed)==len(mapping)
 archive=OUT/'refinement-archive.json';history=core.read(archive) if archive.exists() else []
 for c in removed:history.append({'candidate':c,'reason':mapping[c['qid']],'removed_at':time.time()})
 core.save(archive,history);state['accepted']=[c for c in state['accepted'] if c['qid'] not in mapping]
 for c in removed:state['excluded'].append({'job':c['generation']['job'],'slot':c['slot'],'category':c['category'],'source':c['primary_source'],'error_type':'PostGenerationAudit','reason':mapping[c['qid']]})
 core.save(OUT/'state.json',state);asyncio.run(core.run(0))
if __name__=='__main__':
 setup();action=sys.argv[1]
 {'prepare':prepare,'preflight':preflight,'generate':generate,'resume':resume,'refine':refine}[action]()
