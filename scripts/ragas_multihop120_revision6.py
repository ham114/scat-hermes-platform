"""Explicitly authorized request-only extension; no accounting or deadline reset."""
import asyncio,json,sqlite3,sys,time
from pathlib import Path
import ragas_multihop120_revision5 as previous
import ragas_aviation_budget as meter
core=previous.core;BaseBudget=meter.CampaignBudget

class ExtendedBudget(BaseBudget):
 def __init__(self,folder):
  super().__init__(folder)
  approval=core.read(core.OUT/'request-extension-approval.json')
  assert approval['authorized'] is True and approval['original_policy_sha256']==core.sha(core.OUT/'policy.json')
  assert approval['max_requests']==1200 and self.policy['max_requests']==1000
  for k in ['max_provider_tokens','max_conservative_units','expires_at']:assert approval[k]==self.policy[k]
  digest=core.sha(core.OUT/'request-extension-approval.json')
  with sqlite3.connect(self.path) as db:
   existing=db.execute("select value from meta where key='request_extension_approval_sha256'").fetchone()
   if existing:assert existing[0]==digest
   else:
    count,units,charge=db.execute('select count(*),sum(reserved),sum(charge) from calls').fetchone()
    assert count==1000 and units<self.policy['max_conservative_units'] and charge<self.policy['max_provider_tokens'] and time.time()<self.policy['expires_at']
    assert not db.execute("select count(*) from calls where state='admitted'").fetchone()[0]
    db.execute('insert into meta values(?,?)',('request_extension_approval_sha256',digest))
    db.execute("delete from meta where key='exhausted'")
    core.save(core.OUT/'request-extension-applied.json',{'authorization_sha256':digest,'original_policy_sha256':approval['original_policy_sha256'],'at':time.time(),'previous_requests':count,'previous_conservative_units':units,'previous_provider_charge':charge,'effective_max_requests':1200,'original_deadline':self.policy['expires_at'],'counts_reservations_charges_preserved':True})
  self.policy={**self.policy,'max_requests':1200}

def pair(slot,attempt,pools,byid,vectors,used):
 chosen=previous.pair(slot,attempt,pools,byid,vectors,used)
 hints={
  4:'Use a completely specified issuance timestamp and explicitly say no other same-region SIGMETs are issued in between if asking next serial. Do not infer next serial from hourly timing alone.',
  7:'Ask a geodetic datum comparison: CAT062 geometric altitude is above WGS84 ellipsoid, while FAA GPS/MSL or pressure altitude uses another reference; can the same numerical height be directly substituted? Do not supply either source datum in the question.',
  12:'Retrieve cooling magnitude from the source rather than stating it in the question, then compare mechanisms of icing susceptibility for the two carburetor designs in one causal explanation.',
  26:'Ask what a VALID PRE-EMERGENCY Mode 3/A code 7500 means historically, and whether it proves a current hijack. Preserve the temporal pre-emergency qualification; distinguish historical encoded value from active status.',
  41:'If using a compass-card value with PIREP wind, construct ONE relative-direction result requiring both card lookup and wind-group decoding. Supply magnetic variation explicitly. Do not separately ask a compass lookup and unrelated wind decode.',
  44:'For Class E membership, use actual source exceptions, such as airspace below 1200 ft above ground, if present. A lower Class E base does not prevent 14500 ft from being Class E. If the only missing datum is local terrain elevation, explain how it affects AGL and the exception. Do not invent insufficiency.',
  47:'Do NOT give away the CAT062 axis directions. Use a simple displacement such as X=0 and Y=100 m, and the FAA variation example to ask the magnetic direction to that target. Both axis orientation and variation must be retrieved. No-wind if comparing heading with track.',
  67:'Compare tracker credibility of reported flight level with FAA pressure-altitude datum/true-altitude interpretation. Credible means reasonable change from previous detection, not guaranteed true geometric height. Do not copy source definitions into question.',
  74:'Use a hypothetical instance where unknown authorization or equipment actually changes permission. Do not call a combination insufficient if an already known rule absolutely forbids the proposed dual-use destination/alternate plan.',
  86:'Two signed two-complement fields scale directly by LSB. Vmin/Vmax are allowed physical ranges, NEVER additive offsets. Choose raw values whose scaled outputs are within the documented range, compare encoding resolution or outputs, and do not infer datum information absent from these excerpts.',
  92:'Do not pair redundant copies of the same MM rule; ask a missing-input question with distinct necessary contributions from the two actual excerpts.',
  94:'Do not put the source maximum temperature drop into the question. Retrieve it to compute an internal temperature and then apply the other excerpt to a single supported decision. Do not overstate carburetor-heat exceptions.',
  107:'Ask for encoding a pressure altitude measured with 29.92 inHg setting. Retrieve how that becomes a flight level from the FAA text and the LSB from CAT062. Do not equate an arbitrary MSL altitude with a flight level. Vmin is a bound, not a bias.',
  111:'Do not determine certificate duration from current pilot age; the relevant age is at the examination. Prefer a different coherent topic if this is not clearly supported.',
  115:'Never describe a SYNOP numeric cloud amount code as an actual METAR code. Use a hypothetical cloud observation coded under WMO, then compare the inference available from FAA satellite coverage products.',
  120:'Do not apply Special VFR-only provisions to ordinary VFR or IFR pickup. If using special-VFR weather/contact rules, explicitly place the flight under that authorization and scope.'}
 core.TYPE_HINTS[slot['category']]={'cross_document':'ONE coherent two-document application or same-phenomenon comparison; both nonredundant sources required.','cross_chapter':'ONE coherent cross-section application or same-phenomenon comparison; both nonredundant sources required.','numeric_conditions':'Numerical result or quantitative comparison requiring both sources; preserve units and datum; retrieve source constants rather than leaking them in the question.','insufficient':'A genuinely missing instance input must block one concrete conclusion after BOTH necessary excerpt rules are used.'}[slot['category']]
 if slot['slot'] in hints:core.TYPE_HINTS[slot['category']]+= '\nSPECIFIC CONSTRUCTION CHECK: '+hints[slot['slot']]
 return chosen

def main():
 meter.CampaignBudget=ExtendedBudget
 original_pair=previous.pair
 # previous.main installs previous.pair, so replace that module binding while retaining its implementation.
 global pair
 configured_pair=pair
 def bound(slot,attempt,pools,byid,vectors,used):
  previous.pair=original_pair
  try:return configured_pair(slot,attempt,pools,byid,vectors,used)
  finally:previous.pair=bound
 previous.pair=bound
 manifest=core.OUT/'prompt-revision-6.json';record={'revision':6,'script_sha256':core.sha(Path(__file__)),'parent_script_sha256':core.sha(Path(previous.__file__)),'reason':'Authorized +200 requests without reset; focused construction hints for source and arithmetic errors identified in second assistant audit.','original_policy_sha256':core.sha(core.OUT/'policy.json'),'extension_approval_sha256':core.sha(core.OUT/'request-extension-approval.json'),'original_ledger_preserved':True,'original_deadline_preserved':True}
 if manifest.exists():assert core.read(manifest)==record
 else:core.save(manifest,record)
 previous.main()
if __name__=='__main__':main()
