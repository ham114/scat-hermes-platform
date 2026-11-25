"""Source-error-aware construction revision using the original campaign ledger."""
import asyncio,sys,time,re
from pathlib import Path
import ragas_multihop120 as batch
import ragas_multihop120_revision4 as previous
core=batch.core
PITFALLS='''
Additional source-audit rules: Never infer an aircraft certification category from weight alone. A mean wind is not the lull used in a peaks-versus-lulls gust criterion. MSL altitude is not a flight level without the standard pressure datum. 1 NM/s is 3600 knots, not 360. 'Pre-emergency' Mode 3/A is historical context, not proof of an active hijack. Do not apply observational measurement uncertainty requirements as certified limits for ERA5 reanalysis parameters. A general tendency does not exclude conditions outside its example table. Respect OR versus AND. Do not derive numerical range by a linear extrapolation from a general statement that altitude affects radio line-of-sight. An unknown pressure-surface altitude alone does not supply the along-route temperature/height profile. Do not say surface temperature alone guarantees rime morphology. Do not resolve conflicting manuals by inventing an authority hierarchy. Do not turn a general description into a mandatory observation correction. Neither the source constants nor the whole answer may be supplied by the question itself. Different services, instruments or reporting phases are not one reasoning problem merely because all occur at the same station/airport or use the same protocol. Avoid generic abbreviations and bit-position lookups from flattened grids; prefer prose or reviewed semantic field units.'''

def pair(slot,attempt,pools,byid,vectors,used):
 if slot['primary_source']=='eurocontrol-asterix-cat062':
  import numpy as np
  preferred={7:'kco_14176d784523d24373240933436bbb45',47:'kco_8da5371b1319feec805808bccc7aa2ba',67:'kco_b6518352f997fa973cb3103143ed6fba',107:'kco_5ae3da46db05e365a05337cbfa0dbb95',86:'kco_5a6f4ad24fc2b1303940b366e8d504ae'}
  if slot['slot']==26:
   first=next(r for r in byid.values() if r['source_id']=='eurocontrol-asterix-cat062' and 'Pre-Emergency Mode 3/A' in r['text'] and 'VA' in r['text'] and 'Valid Mode' in r['text'])
  else:first=byid[preferred[slot['slot']]]
  if not slot['require_cross_source']:
   second=byid['kco_5ae3da46db05e365a05337cbfa0dbb95']
  else:
   patterns={7:[r'GPS|GNSS',r'altitude',r'MSL|mean sea'],26:[r'7500',r'hijack'],47:[r'variation',r'true',r'magnetic'],67:[r'29\.92',r'altitude'],107:[r'29\.92',r'flight level|pressure altitude']}
   matches=[r for r in byid.values() if r['source_id'].startswith('faa-') and all(re.search(p,r['text'],re.I) for p in patterns[slot['slot']]) and not any(x in ' '.join(r['section_path']).lower() for x in ['contents','index','abbreviation']) and 250<len(r['text'])<4000]
   assert matches,'Missing curated CAT062 bridge'
   # Stable evidence search; rotate only among relevant prose, not generic grids.
   matches.sort(key=lambda r:('Table #' in r['text'],r['chunk_id'] in used,len(r['text']),r['chunk_id']))
   second=matches[(attempt-1)%min(4,len(matches))]
  assert first['chunk_id']!=second['chunk_id']
  used.update([first['chunk_id'],second['chunk_id']]);return [first,second]
 filtered={s:[r for r in group if not ('AVBL' in byid[r['chunk_id']]['text'] and 'NOTAM' in byid[r['chunk_id']]['text'])] for s,group in pools.items()}
 return previous.previous.pair(slot,attempt,filtered,byid,vectors,used)

def main():
 batch.setup();batch.validate_campaign()
 from ragas.testset.synthesizers.multi_hop.specific import MultiHopSpecificQuerySynthesizer
 MultiHopSpecificQuerySynthesizer.__dataclass_fields__['generate_query_reference_prompt'].default.instruction=previous.GEN+PITFALLS
 core.REVIEW=previous.REVIEW+PITFALLS;core.select_rows=pair
 core.TYPE_HINTS={
  'cross_document':'Use two official documents for ONE coherent application or SAME-phenomenon comparison; both nonredundant excerpts necessary. No unrelated lookups.',
  'cross_chapter':'Use two distinct sections of one document for ONE coherent application or SAME-phenomenon comparison; both excerpts necessary.',
  'numeric_conditions':'Ask a numerical result, quantitative contrast, encoding or bound requiring BOTH sources. Supply hypothetical instance inputs but never source constants or lookup values. Preserve datum and units.',
  'insufficient':'Use BOTH excerpts for one concrete instance conclusion blocked by a genuinely missing input. Do not claim a missing rule/conversion is an instance input or guarantee a conclusion from incomplete prerequisites.'}
 manifest=core.OUT/'prompt-revision-5.json'
 record={'revision':5,'script_sha256':core.sha(Path(__file__)),'parent_script_sha256':core.sha(Path(previous.__file__)),'reason':'Assistant audit: source-scope errors, wrong arithmetic/datum, false insufficiency, unrelated lookups and leaked constants. Exclude suspect NOTAM abbreviation grid pending source repair.','original_policy_sha256':core.sha(core.OUT/'policy.json'),'original_ledger_preserved':True,'original_deadline_preserved':True,'native_synthesizer_preserved':True,'added_source_audit_rules':PITFALLS}
 if manifest.exists():assert core.read(manifest)==record
 else:core.save(manifest,record)
 if 'refine' in sys.argv:
  mapping={r['qid']:r['reason'] for r in core.read(core.OUT/'refinement-request.json')};state=core.read(core.OUT/'state.json')
  removed=[c for c in state['accepted'] if c['qid'] in mapping];assert len(removed)==len(mapping)
  history=core.read(core.OUT/'refinement-archive.json')
  for c in removed:
   history.append({'candidate':c,'reason':mapping[c['qid']],'removed_at':time.time()})
   state['excluded'].append({'job':c['generation']['job'],'slot':c['slot'],'category':c['category'],'source':c['primary_source'],'error_type':'AssistantConstructionAudit','reason':mapping[c['qid']]})
  core.save(core.OUT/'refinement-archive.json',history);state['accepted']=[c for c in state['accepted'] if c['qid'] not in mapping];core.save(core.OUT/'state.json',state)
 asyncio.run(core.run(0))
if __name__=='__main__':main()
