"""Freeze v7 focused evaluation inputs before any paid calibration or answers."""
import collections,hashlib,json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];DEST=ROOT/'output/ragas-multihop120-evaluation-20261124'
SOURCE=ROOT/'evaluations/rag_multihop120_candidates_20261124_v1/evaluation.json'
GEN=ROOT/'output/ragas-multihop120-20261124'
SHA='bd2010dab0c5b4ad3586b85c53497847d0a896a9ca09e638fca3a14219ff6aa3'
def read(p):return json.loads(p.read_text(encoding='utf8'))
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def save(p,v):p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
def main():
 assert not (DEST/'dataset.json').exists(),'frozen inputs preserved'
 data=read(SOURCE);assert len(data['cases'])==120 and data['corpus_sha256']==SHA
 cfg=read(ROOT/'services/rag/real-agent-production-v1.json');assert cfg['corpus_sha256']==SHA
 corpus=ROOT/'var/rag/corpora/kg_20261124_aviation_v7_charts_final/rag_chunks.v1.jsonl';assert sha(corpus)==SHA
 rows={r['chunk_id']:r for r in map(json.loads,corpus.read_text(encoding='utf8').splitlines())}
 original={c['qid']:c for c in map(json.loads,(GEN/'candidates.jsonl').read_text(encoding='utf8').splitlines())}
 units=[];quotes=0
 for c in data['cases']:
  assert c==original[c['qid']] and 0<len(c['question'])<=1000
  evidence=[]
  for cl in c['claims']:
   for e in cl['evidence']:
    r=rows[e['chunk_id']];assert all(r[k]==e[k] for k in ['source_id','source_version','text_sha256'])
    assert r['text'][e['start_char']:e['end_char']]==e['quote'];quotes+=1
    evidence.append({'chunk_id':e['chunk_id'],'quote':e['quote'],'source_id':e['source_id']})
  ids=list(dict.fromkeys(e['chunk_id'] for e in evidence));assert len(ids)==2
  notes=['Optional examples, background exposition, numerical values not asked, and repeated explanations are not mandatory answer points. Keep correct prerequisites and requested numerical results.']
  optional=[]
  points=list(c['required_claims'])
  if c['slot']==58:
   optional=['Approximately 300 ft ground indication error is an optional explanation because the question asks the qualitative relation; missing it alone must not fail the answer.']
   notes+=optional;points=points[1:]
  units.append({'unit_id':c['qid']+'-T1','case_id':c['qid'],'turn':1,'question':c['question'],'reference':c['reference'],
   'required_points':points,'optional_explanations':optional,'gold_claims':c['claims'],'gold_evidence':list({(e['chunk_id'],e['quote']):e for e in evidence}.values()),
   'category':c['category'],'primary_source':c['primary_source'],'language':c['language'],'require_cross_source':c['require_cross_source'],
   'gold_chunk_ids':ids,'supporting_evidence_groups':c['supporting_evidence_groups'],'expected_status':c['expected_status'],
   'scoring_notes':notes,'comparison':'new focused development set; no same-question baseline'})
 assert len(units)==120 and sum(u['require_cross_source'] for u in units)==75
 frozen={'generation_id':cfg['generation_id'],'corpus_sha256':SHA,'cases':120,'turns':120,'units':units,
  'protocol':'Question-only isolated production-source fast RAG; pre-answer minimum rubric freeze; same-provider criterion review; five native Ragas standard metrics; no historical answers or business operations',
  'user_statement':'可以开始评测','individual_human_review_not_inferred':True,'whole_set_authorized_for_evaluation':True}
 save(DEST/'dataset.json',frozen)
 protected=[SOURCE,GEN/'candidates.jsonl',GEN/'audit.json',GEN/'assistant-label-revisions.json',GEN/'evidence-manifest.json']
 for folder in ['rag_aviation300_reviewed_20261124_v1','rag_aviation300_reviewed_20261124_v2','rag_p0_reviewed_20261123_v1','rag_p0_reviewed_20261124_v2']:
  protected.append(ROOT/'evaluations'/folder/'evaluation.json')
 save(DEST/'plan.json',{'status':'prepared_no_model_calls','cases':120,'qa_turns':120,'category_counts':dict(collections.Counter(u['category'] for u in units)),
  'cross_document_total':75,'dataset_sha256':sha(DEST/'dataset.json'),'source_candidates_sha256':sha(SOURCE),'exact_quotes_checked':quotes,
  'protected_files':{p.relative_to(ROOT).as_posix():sha(p) for p in protected},
  'native_metrics':['Faithfulness','AnswerRelevancy (strictness 3; pinned local BGE-M3)','ContextPrecision (ranked AP)','ContextRecall','FactualCorrectness (F1; low atomicity/coverage)'],
  'gold_protocol':'Trim minimum points from fixed candidate labels before any actual answer; optional detail not mandatory. Annotation issues flagged and reference-based primary metrics stratified; originals preserved.',
  'coverage_protocol':'Strict frozen chunk IDs for both required chunks, per-group and per-source, initial top5/top10 and actual full-read contexts. Alternative valid chunks can be missed by exact-ID coverage.',
  'proposed_limits':{'max_requests':4000,'max_provider_input_plus_output_tokens':24000000,'max_conservative_units':160000000,'wall_seconds':7200,'max_output_tokens':4096,'workers':2},
  'destination':'https://api.deepseek.com','credentials':'Existing production profile key for QA; existing server Ragas dedicated key for calibration and scoring; keys never exported',
  'data_scope':'120 synthetic questions, frozen candidate labels and necessary fixed-v7 excerpts (including existing project background if retrieved), actual answers and their evidence for scoring; no real user sessions, database trajectories or full stored run-trace files',
  'external_model_calls':0,'production_changes':0,'training_runs':0,'not_independent_human_accuracy':True})
 print(json.dumps({'cases':120,'quotes_checked':quotes,'cross_document':75,'dataset_sha256':sha(DEST/'dataset.json'),'new_model_calls':0}))
if __name__=='__main__':main()
