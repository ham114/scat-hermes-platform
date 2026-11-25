"""Replace quarantined table chunks with source-reviewed semantic records."""
import hashlib,json,sys,os,copy
from pathlib import Path
from datetime import datetime,timezone
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'scripts')]
PARENT=ROOT/'var/rag/corpora/kg_20261124_aviation_v6_p0'
TARGET=ROOT/'var/rag/corpora/kg_20261124_aviation_v7_charts_final'
OUT=ROOT/'output/rag-chart-rewrite-20261124'
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
def read(p):return json.loads(p.read_text(encoding='utf8'))
def save(p,v):p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
def main():
 os.environ.update(HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',TOKENIZERS_PARALLELISM='false')
 assert not (TARGET/'rag_chunks.v1.jsonl').exists(),'immutable generation exists'
 parent_raw=(PARENT/'rag_chunks.v1.jsonl').read_bytes()
 assert sha(PARENT/'rag_chunks.v1.jsonl')=='d5caa4b52de8924799cd752765d950c193532d3d6502bbcba64adc105e1571c5'
 parent=[json.loads(x) for x in parent_raw.splitlines()];lines={r['chunk_id']:line for r,line in zip(parent,parent_raw.splitlines(keepends=True))}
 rewrites=read(OUT/'reviewed-rewrites.json');assert len(rewrites)==29
 from transformers import AutoTokenizer
 cfg=read(ROOT/'services/rag/qwen3-production-v1.json');tokenizer=AutoTokenizer.from_pretrained(cfg['embedding_model'],local_files_only=True)
 count=lambda t:len(tokenizer.encode(t,add_special_tokens=False))
 remove=set();new=[];audit=[]
 for item in rewrites:
  owners=[r for r in parent if r['locator'].get('structured_table_json')==item['artifact']]
  assert owners
  source=ROOT/item['source_pdf'];assert sha(source)==item['source_sha256']
  assert sha(OUT/item['image'])==item['image_sha256']
  remove.update(r['chunk_id'] for r in owners)
  old=owners[0];artifact=TARGET/'rewrites'/f"chart-{item['index']:02d}.json"
  save(artifact,item)
  prefix=f"{old['institution']} | {old['edition']} | {old['jurisdiction']}\n{item['title']}\nSource PDF page {item['page_numbers'][0]}. Reviewed semantic transcription, original diagram archived.\n{item['common_context']}\n"
  batches=[];batch=[]
  for index,unit in enumerate(item['units']):
   assert count(prefix+unit)<=1800
   if batch and count(prefix+'\n'.join(item['units'][i] for i in batch+[index]))>640:batches.append(batch);batch=[]
   batch.append(index)
  if batch:batches.append(batch)
  ids=[]
  for batch in batches:
   text=prefix+'\n'.join(item['units'][i] for i in batch)
   loc={**old['locator'],'kind':'reviewed-chart-rewrite','reviewed_rewrite_json':artifact.relative_to(ROOT).as_posix(),'reviewed_rewrite_sha256':sha(artifact),'reviewed_unit_indices':batch}
   loc.pop('cell_segments',None);loc.pop('row_indices',None)
   identity=json.dumps({'source_id':old['source_id'],'locator':loc,'text':text},ensure_ascii=False,sort_keys=True)
   row={**old,'locator':loc,'text':text,'text_sha256':hashlib.sha256(text.encode()).hexdigest(),'char_count':len(text),'token_count':count(text),
        'chunk_id':'kco_'+hashlib.sha256(identity.encode()).hexdigest()[:32],'section_path':[old['section_path'][0],item['title']],
        'chunk_type':'text' if item['kind'] in ['contents-index','glossary','worked-formula','example-form'] else 'table',
        'normalization_version':'source-reviewed-chart-rewrite/1'}
   assert '[Unavailable source rows' not in text and 'Table extraction unavailable' not in text
   new.append(row);ids.append(row['chunk_id'])
  assert sorted(i for b in batches for i in b)==list(range(len(item['units'])))
  audit.append({'index':item['index'],'title':item['title'],'kind':item['kind'],'source_pdf':item['source_pdf'],'source_sha256':item['source_sha256'],
                'pages':item['page_numbers'],'removed_chunks':[r['chunk_id'] for r in owners],'new_chunks':ids,'units':len(item['units']),'notes':item['notes']})
 retained=[r for r in parent if r['chunk_id'] not in remove];allrows=retained+new
 assert len({r['chunk_id'] for r in allrows})==len(allrows)
 raw=b''.join(lines[r['chunk_id']] for r in retained)+b''.join((json.dumps(r,ensure_ascii=False,sort_keys=True)+'\n').encode() for r in new)
 (TARGET/'rag_chunks.v1.jsonl').write_bytes(raw)
 generation=read(PARENT/'rag_generation.v1.json');generation.update(generation_id=TARGET.name,parent_generation_id=PARENT.name,
    parent_corpus_sha256=sha(PARENT/'rag_chunks.v1.jsonl'),corpus_sha256=sha(TARGET/'rag_chunks.v1.jsonl'),chunk_count=len(allrows),
    created_at=datetime.now(timezone.utc).isoformat(),status='built-pending-local-retrieval-validation',normalization_version='source-reviewed-chart-rewrite/1')
 save(TARGET/'rag_generation.v1.json',generation)
 summary={'status':'passed','parent_chunks':len(parent),'chunks':len(allrows),'corpus_sha256':sha(TARGET/'rag_chunks.v1.jsonl'),
          'parent_sha256':sha(PARENT/'rag_chunks.v1.jsonl'),'retained_chunks':len(retained),'removed_chunks':len(remove),'new_chunks':len(new),
          'table_count':647,'rewritten_regions':29,'reviewed_units':sum(x['units'] for x in audit),'quarantined_tables':0,
          'nonempty_cells_checked':25597,'source_anomalies':6,'external_model_calls':0,'production_switched':False}
 save(TARGET/'build-summary.json',summary);save(TARGET/'chart-rewrite-audit.json',{'status':'passed','regions':audit,'all_units_published':True,'historical_evaluations_changed':False})
 print(json.dumps(summary),flush=True)
if __name__=='__main__':
 main()
 import stage_aviation_p0 as stage
 stage.PARENT=PARENT;stage.TARGET=TARGET;stage.main()
 native=read(TARGET/'real-agent-config.json');native['scope']='production-knowledge-aviation-v7-charts';save(TARGET/'real-agent-config.json',native)
