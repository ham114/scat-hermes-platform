"""Freeze a small v7 problem-case retest before any model calls."""
import json,hashlib,copy
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'output/rag-problem-retest-20261124'
def read(p):return json.loads(p.read_text(encoding='utf8'))
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def save(p,v):p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
def main():
 assert not (OUT/'dataset.json').exists(),'frozen retest preserved'
 old=[json.loads(x) for x in (ROOT/'output/ragas-standard-rescore-20261124/inputs.jsonl').read_text(encoding='utf8').splitlines()]
 selected={r['case_id'] for r in old if r['prior_review'].get('verdict')!='pass'}
 selected|={'AV300-20261124-104','AV300-20261124-297'}
 assert len(selected)==14
 aviation=read(ROOT/'evaluations/rag_aviation300_reviewed_20261124_v2/evaluation.json')['cases']
 p0=read(ROOT/'evaluations/rag_p0_reviewed_20261124_v2/evaluation.json')['cases']
 current={c['case_id']:c for c in aviation+p0}
 config=read(ROOT/'services/rag/real-agent-production-v1.json')
 corpus=ROOT/'var/rag/corpora/kg_20261124_aviation_v7_charts_final/rag_chunks.v1.jsonl'
 assert sha(corpus)==config['corpus_sha256']=='bd2010dab0c5b4ad3586b85c53497847d0a896a9ca09e638fca3a14219ff6aa3'
 records={r['chunk_id']:r for r in map(json.loads,corpus.read_text(encoding='utf8').splitlines())}
 units=[]
 for cid in sorted(selected):
  c=current[cid];prior=sorted([r for r in old if r['case_id']==cid],key=lambda r:r['turn'])
  for i,b in enumerate(prior):
   question=c['turns'][i]['question'] if c.get('turns') else c['question']
   revised=bool(c.get('p0_revision'))
   reference=c['turns'][i]['reference'] if c.get('turns') else '\n'.join(c['required_claims']) if revised else b['reference']
   evidence=[]
   for claim in c['claims']:
    for e in claim['evidence']:
     if e['chunk_id'] in records:
      row=records[e['chunk_id']];assert row['text'][e['start_char']:e['end_char']]==e['quote'];evidence.append({'chunk_id':row['chunk_id'],'quote':e['quote']})
     else:
      loc=e['locator'];matches=[r for r in records.values() if r['locator'].get('structured_table_json')==loc.get('structured_table_json') and loc.get('structured_table_json')]
      assert matches,(cid,e['chunk_id'])
      evidence.extend({'chunk_id':r['chunk_id'],'quote':r['text']} for r in matches)
   evidence=list({(e['chunk_id'],e['quote']):e for e in evidence}.values())
   units.append({'unit_id':b['unit_id'],'case_id':cid,'turn':i+1,'question':question,'reference':reference,
      'required_points':[s for s in reference.split('\n') if s.strip()],'gold_evidence':evidence,
      'question_changed':question!=b['question_raw'],'reference_revised':revised,
      'comparison':'revised-question acceptance; not direct before/after' if question!=b['question_raw'] else 'same-question comparison',
      'baseline_question':b['question_raw'],'baseline_answer':b['response'],'baseline_reference':b['reference'],
      'baseline_contexts_seen':b['contexts_seen'],
      'baseline_review':b['prior_review'],'baseline_execution_status':b['execution_status'],
      'inclusion_reason':'prior failure or unavailable review' if b['prior_review'].get('verdict')!='pass' else 'unavailable Ragas faithfulness' if cid.endswith('-104') else 'restored chart evidence regression'})
 assert len(units)==15
 save(OUT/'dataset.json',{'generation_id':config['generation_id'],'corpus_sha256':config['corpus_sha256'],'cases':14,'turns':15,'units':units,
      'protocol':'native production-source fast QA, question only; actual current synthetic turn history; frozen v2 corrections; separate execution, criterion review and native Ragas metrics'})
 protected=[ROOT/'evaluations/rag_aviation300_reviewed_20261124_v1/evaluation.json',ROOT/'evaluations/rag_aviation300_reviewed_20261124_v2/evaluation.json',ROOT/'evaluations/rag_p0_reviewed_20261123_v1/evaluation.json',ROOT/'evaluations/rag_p0_reviewed_20261124_v2/evaluation.json',ROOT/'output/ragas-standard-rescore-20261124/final.json',ROOT/'output/ragas-aviation300-evaluation-20261124/qa-final.json']
 save(OUT/'plan.json',{'status':'prepared_no_model_calls','cases':14,'qa_turns':15,'question_changed_turns':sum(u['question_changed'] for u in units),
      'dataset_sha256':sha(OUT/'dataset.json'),'protected_files':{p.relative_to(ROOT).as_posix():sha(p) for p in protected},
      'baseline_label_rescore':'Rejudge saved answers for P0-024 and AV261 with corrected rubrics to separate label repair from answer changes.',
      'native_metrics':['Faithfulness','AnswerRelevancy (strictness 3, pinned local BGE-M3)','ContextPrecision (ranked AP)','ContextRecall','FactualCorrectness (F1, low atomicity/coverage)'],
      'proposed_limits':{'max_requests':650,'max_provider_input_plus_output_tokens':6000000,'max_conservative_units':40000000,'wall_seconds':3600,'max_output_tokens':4096,'workers':2},
      'destination':'https://api.deepseek.com','credentials':'existing production profile key for QA; existing server Ragas dedicated key for scoring; never exported',
      'external_model_calls':0,'production_change':False,'no_business_operations':True})
 print(json.dumps({'cases':14,'turns':15,'changed_questions':sum(u['question_changed'] for u in units),'model_calls':0}))
if __name__=='__main__':main()
