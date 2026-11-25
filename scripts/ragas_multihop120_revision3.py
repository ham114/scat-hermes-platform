"""Improve evidence pairing without changing the approved campaign limits."""
import asyncio,json,re,sys,time
from pathlib import Path
import ragas_multihop120 as batch
import ragas_multihop120_revision as revision2
core=batch.core
GEN=revision2.GEN+'''
Also valid is a single explicit comparison on the SAME technical dimension, where the final difference, compatibility conclusion, relative bound or choice can only be derived after retrieving BOTH nonredundant source values or applicability rules. This is a genuine two-evidence synthesis. Do not confuse it with two definitions merely listed side by side. Prefer a short case question asking ONE decision or contrast rather than multiple questions. The source excerpts need not have identical terminology.'''
REVIEW=revision2.REVIEW+'''
CLARIFICATION: A same-dimension comparison IS valid multi-hop synthesis when its final contrast, compatibility, choice, difference or relative bound requires BOTH nonredundant source values/rules. Comparing the SAME technical phenomenon under two different conditions or source specifications qualifies; merely listing two facts does not. Definition-plus-rule application also qualifies even if a knowledgeable expert could recall the definition from memory: judge necessity within the supplied sources, not from an expert's background knowledge. Do not invent a stronger condition than the source: distinguish 'can be identified' from 'already identified', 'may' from 'must', and sufficiency from necessity. Numerical-type questions must explicitly request a numerical value or quantitative comparison and cannot be qualitative only.'''

def pair(slot,attempt,pools,byid,vectors,used):
 import numpy as np
 def useful(row):
  r=byid[row['chunk_id']];heading=' '.join(r['section_path']).lower();t=r['text']
  if any(x in heading for x in ['contents','index','abbreviation','acronym','list of','appendix a']):return False
  if len(re.findall(r'\bSee\b',t))>=4 or 'SOURCE ANOMALY' in t:return False
  return r['token_count']>=80
 pool=[r for r in pools[slot['primary_source']] if r['chunk_id'] not in used and useful(r)]
 if slot['category']=='numeric_conditions':
  numeric=[r for r in pool if re.search(r'\d',byid[r['chunk_id']]['text']) and re.search(r'\b(ft|feet|FL|LSB|hPa|knots?|kg|Kelvin|mm|m/s|percent|temperature|altitude|pressure|uncertainty|resolution)\b',byid[r['chunk_id']]['text'],re.I)]
  if numeric:pool=numeric
 assert pool,'No unused useful primary evidence remains'
 offset=(slot['slot']*17+attempt*23)%len(pool);pool=pool[offset:]+pool[:offset]
 other=[r for group in pools.values() for r in group if r['chunk_id'] not in used and useful(r) and (r['source_id']!=slot['primary_source'] if slot['require_cross_source'] else r['source_id']==slot['primary_source'])]
 idx=np.array([r['index'] for r in other]);firsts=pool[:120];matrix=vectors[[r['index'] for r in firsts]]@vectors[idx].T
 options=[]
 for i,first in enumerate(firsts):
  order=np.argsort(-matrix[i],kind='stable')[:25]
  for j in order:
   second=other[j];score=float(matrix[i,j])
   if .52<score<.86 and first['section']!=second['section'] and first['chunk_id']!=second['chunk_id']:
    options.append((score,i,first,second));break
 assert options,'No coherent distinct-section evidence pair found'
 # Prefer semantic relation but rotate among high-quality pairs to preserve variety.
 options.sort(key=lambda x:(-x[0],x[1]));score,_,first,second=options[(slot['slot']+attempt)%min(6,len(options))]
 used.update([first['chunk_id'],second['chunk_id']]);return [byid[first['chunk_id']],byid[second['chunk_id']]]

def main():
 batch.setup();batch.validate_campaign()
 from ragas.testset.synthesizers.multi_hop.specific import MultiHopSpecificQuerySynthesizer
 MultiHopSpecificQuerySynthesizer.__dataclass_fields__['generate_query_reference_prompt'].default.instruction=GEN
 core.REVIEW=REVIEW;core.select_rows=pair
 manifest=core.OUT/'prompt-revision-3.json'
 record={'revision':3,'script_sha256':core.sha(Path(__file__)),'parent_revision_sha256':core.sha(Path(revision2.__file__)),'reason':'Exclude indices/acronym cross-references; prefer highly related distinct evidence. Admit genuine same-dimension comparison as standard multi-context synthesis, reject parallel unrelated lookups.','original_policy_sha256':core.sha(core.OUT/'policy.json'),'original_ledger_preserved':True,'original_deadline_preserved':True,'native_synthesizer_preserved':True,'generation_instruction':GEN,'review_instruction':REVIEW}
 if manifest.exists():assert core.read(manifest)==record
 else:core.save(manifest,record)
 if 'remove-known-errors' in sys.argv:
  bad={'MH120-20261124-046':'can be radar identified does not imply already identified; answer imposes a stronger condition than source','MH120-20261124-080':'620 kHz frequency alone does not establish that the station is a commercial broadcast station; ambiguity between NDB and AM broadcast applicability'}
  state=core.read(core.OUT/'state.json');removed=[c for c in state['accepted'] if c['qid'] in bad]
  history=core.read(core.OUT/'refinement-archive.json')
  for c in removed:
   history.append({'candidate':c,'reason':bad[c['qid']],'removed_at':time.time()})
   state['excluded'].append({'job':c['generation']['job'],'slot':c['slot'],'category':c['category'],'source':c['primary_source'],'error_type':'AssistantSourceAudit','reason':bad[c['qid']]})
  core.save(core.OUT/'refinement-archive.json',history);state['accepted']=[c for c in state['accepted'] if c['qid'] not in bad];core.save(core.OUT/'state.json',state)
 asyncio.run(core.run(int(sys.argv[1]) if len(sys.argv)>1 and sys.argv[1].isdigit() else 0))
if __name__=='__main__':main()
