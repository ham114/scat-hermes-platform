"""Verify reviewed chart publication and pinned local retrieval regression."""
import json,hashlib,sys,time
from pathlib import Path
from urllib.request import Request,build_opener,ProxyHandler
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
OUT=ROOT/'output/rag-chart-rewrite-20261124'
TARGET=ROOT/'var/rag/corpora/kg_20261124_aviation_v7_charts_final'
PARENT=ROOT/'var/rag/corpora/kg_20261124_aviation_v6_p0'
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
def read(p):return json.loads(p.read_text(encoding='utf8'))
def save(p,v):p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
def main():
 summary=read(TARGET/'build-summary.json');audit=read(TARGET/'chart-rewrite-audit.json')
 rows={r['chunk_id']:r for r in map(json.loads,(TARGET/'rag_chunks.v1.jsonl').read_text().splitlines())}
 parent={r['chunk_id']:r for r in map(json.loads,(PARENT/'rag_chunks.v1.jsonl').read_text().splitlines())}
 assert sha(TARGET/'rag_chunks.v1.jsonl')==summary['corpus_sha256']
 removed={c for a in audit['regions'] for c in a['removed_chunks']}
 assert not removed.intersection(rows)
 for cid,row in parent.items():
  if cid not in removed:assert rows[cid]==row
 allids=[]
 for a in audit['regions']:
  chunks=[rows[c] for c in a['new_chunks']];allids+=a['new_chunks']
  item=read(ROOT/chunks[0]['locator']['reviewed_rewrite_json'])
  assert sha(ROOT/item['source_pdf'])==item['source_sha256']
  indices=[]
  for chunk in chunks:
   loc=chunk['locator'];assert sha(ROOT/loc['reviewed_rewrite_json'])==loc['reviewed_rewrite_sha256']
   assert chunk['token_count']<=1800
   assert item['common_context'] in chunk['text']
   for index in loc['reviewed_unit_indices']:
    assert item['units'][index] in chunk['text'];indices.append(index)
  assert sorted(indices)==list(range(a['units']))
 rewrites=read(OUT/'reviewed-rewrites.json')
 assert len(rewrites[8]['units'])==137 and len(rewrites[9]['units'])==20
 assert sum('SOURCE ANOMALY' in u for u in rewrites[8]['units'])==6
 assert any('140/8000' not in u and '65 lb' in u for u in rewrites[6]['units'])
 assert any('0.5 m' in u for u in rewrites[0]['units'])
 assert len(rewrites[28]['units'])==20
 structural={'status':'passed','rewritten_regions':29,'rewritten_units':summary['reviewed_units'],'retained_chunks_verified':len(parent)-len(removed),
             'new_chunks':len(allids),'all_units_published':True,'suspected_source_misprints_flagged':6,'external_model_calls':0}
 save(OUT/'structural-audit.json',structural);save(OUT/'rewrite-verification.json',structural)
 op=build_opener(ProxyHandler({}))
 for _ in range(90):
  try:
   with op.open('http://127.0.0.1:18134/health/ready',timeout=2) as response:h=json.load(response)
   if h['corpus_sha256']==summary['corpus_sha256']:break
  except OSError:pass
  time.sleep(1)
 else:raise RuntimeError('candidate not ready')
 def search(qid,q):
  path=OUT/'retrieval'/f'{qid}.json'
  if path.exists():
   v=read(path);assert v['corpus_sha256']==summary['corpus_sha256'] and v['query']==q;return v['results']
  req=Request('http://127.0.0.1:18134/search',data=json.dumps({'query':q,'limit':10}).encode(),headers={'Content-Type':'application/json'})
  with op.open(req,timeout=90) as response:v=json.load(response)
  assert v['corpus_sha256']==summary['corpus_sha256']
  for x in v['results']:assert x['text']==rows[x['chunk_id']]['text']
  save(path,{'query':q,**v});return v['results']
 from verify_aviation_retrieval import PROBES
 probes=[]
 for qid,source,q in PROBES:
  result=search(qid,q);probes.append({'id':qid,'passed':any(x['source_id']==source for x in result)})
 sourcechecks=[]
 for i,q,needle in [(0,'CAT062 I062/500 XY covariance component resolution two complement','resolution 0.5 m'),
  (6,'FAA PHAK weight shifting 7800 pounds 81.5 80.5 120 inches','65 lb'),
  (8,'FAA PHAK Figure 11-24 short field takeoff 2400 lb 3000 ft 20 C','ground roll=1230 ft'),
  (9,'FAA PHAK Figure 11-26 climb 3400 pounds 8000 feet time fuel distance','fuel 21 lb'),
  (12,'FAA PHAK DME/P acronym precision distance measuring equipment','DME/P'),
  (26,'WMO Beaufort 8 gale wind speed knots metres per second','34-40 kt'),
  (27,'WMO pyrheliometer High quality Good quality zero offset A B','Zero offset A'),
  (28,'WMO daytime radiosonde Meteolabor copper constantan temperature systematic error','Meteolabor')]:
  got=search(f'REWRITE-{i:02d}',q);rank=next((k+1 for k,x in enumerate(got) if needle in x['text']),None)
  sourcechecks.append({'region':i,'evidence_rank':rank,'passed':rank is not None})
 comparisons=[]
 baseline_dir=ROOT/'output/rag-p0-repair-20261124/retrieval/18134'
 save(OUT/'retrieval-preregistration.json',{'baseline':'frozen v6 results from unchanged pinned production profile','baseline_corpus_sha256':sha(PARENT/'rag_chunks.v1.jsonl'),'source_probes':PROBES,'fresh_qa':False})
 for filename in ['evaluation.json','regression.json']:
  for case in read(ROOT/'evaluations/rag_p0_reviewed_20261123_v1'/filename)['cases']:
   if case.get('turns'):continue
   bpath=baseline_dir/(case['case_id']+'.json');base=read(bpath)
   assert base['query']==case['question'] and base['corpus_sha256']==sha(PARENT/'rag_chunks.v1.jsonl')
   new=search(case['case_id'],case['question']);item={'id':case['case_id'],'baseline_file_sha256':sha(bpath)}
   for k in [5,10]:
    item[f'old_{k}']=all(set(g)<={x['chunk_id'] for x in base['results'][:k]} for g in case['supporting_evidence_groups'])
    item[f'new_{k}']=all(set(g)<={x['chunk_id'] for x in new[:k]} for g in case['supporting_evidence_groups'])
   comparisons.append(item);print(json.dumps({'regression':len(comparisons)}),flush=True)
 losses={str(k):[c['id'] for c in comparisons if c[f'old_{k}'] and not c[f'new_{k}']] for k in [5,10]}
 result={'status':'passed' if not any(losses.values()) and all(p['passed'] for p in probes) and all(p['passed'] for p in sourcechecks) else 'needs-review',
         'old_cases':len(comparisons),'losses':losses,'counts':{f'{label}_{k}':sum(c[f'{label}_{k}'] for c in comparisons) for label in ['old','new'] for k in [5,10]},
         'source_probes':probes,'rewrite_probes':sourcechecks,'comparisons':comparisons,'external_model_calls':0,'qa_rerun':False}
 save(OUT/'retrieval-summary.json',result);print(json.dumps({k:v for k,v in result.items() if k!='comparisons'}),flush=True)
if __name__=='__main__':main()
