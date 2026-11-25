"""Focused replacement of an unsupported PBN-approval premise."""
import sys
from pathlib import Path
import ragas_multihop120_revision6 as active
core=active.core;original=active.pair
def pair(slot,attempt,pools,byid,vectors,used):
 if slot['slot']!=74:return original(slot,attempt,pools,byid,vectors,used)
 ids=core.read(core.OUT/'final-refinement-074-source.json')['chunk_ids'];chosen=[byid[cid] for cid in ids]
 used.update(ids)
 core.TYPE_HINTS['insufficient']='''Use these actual SID and STAR excerpts ONLY. A hypothetical flight intends to use public PBN SID and STAR with GPS, but the specific procedure PBN-box contents and evidence of meeting AC90 performance/required sensor or function specifications have not been supplied. Ask whether generic GPS equipment alone establishes eligibility for BOTH procedures, and which concrete procedure/aircraft information is still required. Retrieve the 95%-time system-error criterion. Never invent a separate administrative PBN approval requirement: these excerpts do not state one. Do not state a known prohibition and then call it missing information. Two phases require checking BOTH distinct procedure boxes.'''
 return chosen
def main():
 active.pair=pair
 manifest=core.OUT/'prompt-revision-7.json';record={'revision':7,'script_sha256':core.sha(Path(__file__)),'parent_script_sha256':core.sha(Path(active.__file__)),'reason':'Replace unsupported administrative PBN approval prerequisite with source-stated performance and actual procedure-box requirements.','original_policy_sha256':core.sha(core.OUT/'policy.json'),'original_ledger_preserved':True,'original_deadline_preserved':True}
 if manifest.exists():assert core.read(manifest)==record
 else:core.save(manifest,record)
 active.main()
if __name__=='__main__':main()
