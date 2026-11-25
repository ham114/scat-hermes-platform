"""Topic-matched official evidence planning with an authorized request extension."""
import argparse
import collections
import fcntl
import re
import sqlite3
import sys
import time
from pathlib import Path
import ragas_holdout120_campaign as campaign
import ragas_holdout120_revision2 as v2
import ragas_holdout120_revision3 as v3
import ragas_holdout120_core_v5 as core
import ragas_aviation_budget as meter
import ragas_holdout_budget_extension as extension

TOPICS={
 'altimetry':r'pressure altitude|altimeter|barometric|QNH|29\.92',
 'density':r'density altitude|air density|density of (?:the )?air',
 'height_datum':r'geopotential|geometric altitude|mean sea level|WGS.?84|ellipsoid',
 'heading':r'magnetic (?:north|heading|variation)|true (?:north|heading)|compass|declination',
 'wind':r'wind speed|wind direction|anemometer|gust',
 'carb_icing':r'carburett?or|carburetor',
 'airframe_icing':r'supercooled|rime|clear ice|airframe icing|icing conditions',
 'turbulence':r'turbulence|turbulent|mountain wave',
 'visibility':r'visibility|meteorological optical|\bMOR\b',
 'rvr':r'\bRVR\b|runway visual range',
 'weather_radar':r'weather radar|reflectivity|Doppler|dBZ',
 'humidity':r'dew point|dewpoint|relative humidity|wet.bulb|saturation',
 'cloud_amount':r'cloud cover|cloud amount|octas|oktas|ceilometer|ceiling',
 'precipitation':r'precipitation|raindrop|rainfall|hail|snowfall',
 'weather_advisories':r'SIGMET|AIRMET|G-AIRMET',
 'pilot_reports':r'PIREP|pilot report',
 'weather_reports':r'METAR|SPECI|TAF|AWOS|ASOS',
 'approach_categories':r'approach categor|circling|\bVREF\b',
 'visual_minima':r'special VFR|VFR minima|visual meteorological|cloud clearance',
 'ils':r'\bILS\b|localizer|glideslope|glide slope',
 'rnav':r'RNAV|\bRNP\b|\bPBN\b|RAIM|WAAS|GBAS',
 'vor':r'\bVOR\b|\bVORTAC\b|\bDME\b',
 'adf':r'\bADF\b|\bNDB\b|nondirectional beacon',
 'clearance':r'readback|read back|ATC clearance|IFR clearance|\bCTAF\b',
 'surveillance':r'transponder|Mode S|Mode A|Mode C|ADS.B',
 'vertical_rate':r'vertical rate|rate of climb|rate of descent|vertical speed',
 'flight_level':r'flight level|measured flight level|selected altitude',
 'speed':r'true airspeed|indicated airspeed|calibrated airspeed|Mach number|ground speed',
 'stall':r'load factor|stall speed|stalling|angle of attack',
 'turn':r'bank angle|rate of turn|turn radius|radius of turn',
 'balance':r'center of gravity|centre of gravity|weight and balance|\bmoment\b',
 'engine':r'manifold pressure|fuel.air|mixture|piston|turbocharg',
 'electrical':r'alternator|voltage|electrical system|generator',
 'pitot_static':r'pitot|static port|static system|vertical speed indicator|\bVSI\b',
 'illusion':r'the leans|Coriolis|somatogravic|vestibular|semicircular',
 'era_time':r'accumulation|accumulated|forecast step|validity time|analysis time',
 'vertical_coordinate':r'model level|pressure level|hybrid|sigma level',
 'temperature':r'\bkelvin\b|\bCelsius\b|thermometer|temperature gradient|lapse rate',
 'stability':r'adiabatic|convection|inversion|atmospheric stability',
 'runway_lighting':r'runway light|approach light|\bPAPI\b|\bVASI\b',
 'performance':r'takeoff distance|landing distance|accelerate.stop|braking|runway length',
}
_CACHE={}


def topics(row):
    cid=row['chunk_id']
    if cid not in _CACHE:_CACHE[cid]={k for k,p in TOPICS.items()if re.search(p,row['text'],re.I)}
    return _CACHE[cid]


def pair(slot,attempt,pools,byid,vectors,used):
    import numpy as np
    candidates=[r for r in pools[slot['primary_source']]if r['chunk_id']not in used and v3.reliable(byid[r['chunk_id']]) and topics(byid[r['chunk_id']])]
    if slot['category']=='numeric_conditions':
        numeric=[r for r in candidates if re.search(r'\d',byid[r['chunk_id']]['text'])]
        if numeric:candidates=numeric
    assert candidates,'No unused topic-bearing primary evidence'
    offset=(slot['slot']*43+attempt*53)%len(candidates);candidates=candidates[offset:]+candidates[:offset]
    other=[r for group in pools.values()for r in group if r['chunk_id']not in used and v3.reliable(byid[r['chunk_id']]) and
           (r['source_id']!=slot['primary_source']if slot['require_cross_source']else r['source_id']==slot['primary_source'])]
    firsts=candidates[:100];matrix=vectors[[r['index']for r in firsts]]@vectors[[r['index']for r in other]].T
    options=[]
    for i,a in enumerate(firsts):
        theme=topics(byid[a['chunk_id']])
        for j in np.argsort(-matrix[i],kind='stable')[:180]:
            b=other[j];score=float(matrix[i,j]);shared=theme&topics(byid[b['chunk_id']])
            if .43<score<.90 and shared and a['chunk_id']!=b['chunk_id']and a['section']!=b['section']:
                options.append((len(shared),score,i,a,b));break
    assert options,'No same-phenomenon complementary candidate'
    options.sort(key=lambda x:(-x[0],-x[1],x[2]));_,_,_,a,b=options[(slot['slot']+attempt)%min(20,len(options))]
    used.update([a['chunk_id'],b['chunk_id']]);return[byid[a['chunk_id']],byid[b['chunk_id']]]


def alternatives(slot,chosen,pools,byid,vectors,used):
    import numpy as np
    a=chosen[0];theme=topics(a);index={r['chunk_id']:r['index']for group in pools.values()for r in group}
    candidates=[r for group in pools.values()for r in group if r['chunk_id']not in used and v3.reliable(byid[r['chunk_id']]) and
        (r['source_id']!=a['source_id']if slot['require_cross_source']else r['source_id']==a['source_id']) and
        r['section']!=a['section_path'][-3:]and theme&topics(byid[r['chunk_id']])]
    if not candidates:return chosen
    sims=vectors[[r['index']for r in candidates]]@vectors[index[a['chunk_id']]]
    result=list(chosen);sections={(r['source_id'],tuple(r['section_path'][-3:]))for r in result};counts=collections.Counter(r['source_id']for r in result)
    for j in np.argsort(-sims,kind='stable'):
        r=byid[candidates[j]['chunk_id']];section=(r['source_id'],tuple(r['section_path'][-3:]))
        if not .40<float(sims[j])<.91 or section in sections or slot['require_cross_source']and counts[r['source_id']]>=2:continue
        result.append(r);sections.add(section);counts[r['source_id']]+=1
        if len(result)>=6:break
    return result


def main():
    assert sys.executable=='/home/h3c/anaconda3/envs/ragas-eval/bin/python'
    parser=argparse.ArgumentParser();parser.add_argument('--limit',type=int,default=0);args=parser.parse_args()
    campaign.core=core;campaign.pair=pair;campaign.GEN+=v2.GEN_EXTRA;campaign.REVIEW+=v2.REVIEW_EXTRA
    campaign.GEN+='\nSCOPE/LOGIC GATE: Never transfer surface METAR to upper-air radiosonde values, CAT062 geometric rate to an unrelated TIS implementation, PIREP to ASRP, AGL to MSL/standard-atmosphere height, or an accident observation to universal impossibility. A 95% interval is not a hard maximum. Do not confuse max representable value, resolution and uncertainty. Forecast horizon is not necessarily bulletin validity. Do not equate 1/4 degree with 25 percent. Never use one go/no-go blocker and an unrelated extra blocker as a multi-hop dependency. Prefer same-quantity contrasts under distinct conditions, code/physical-datum interpretation, or supported quantitative composition. Preserve may/should/ideally; do not turn recommendations into certification requirements. Do not make missing magnitude equivalent to missing sign only. Reject false-premise questions unless both excerpts support explicitly rejecting that premise. Limit the reference to facts directly requested by the question.\n';campaign.REVIEW+='\nSCOPE/LOGIC GATE: Never transfer surface METAR to upper-air radiosonde values, CAT062 geometric rate to an unrelated TIS implementation, PIREP to ASRP, AGL to MSL/standard-atmosphere height, or an accident observation to universal impossibility. A 95% interval is not a hard maximum. Do not confuse max representable value, resolution and uncertainty. Forecast horizon is not necessarily bulletin validity. Do not equate 1/4 degree with 25 percent. Never use one go/no-go blocker and an unrelated extra blocker as a multi-hop dependency. Prefer same-quantity contrasts under distinct conditions, code/physical-datum interpretation, or supported quantitative composition. Preserve may/should/ideally; do not turn recommendations into certification requirements. Do not make missing magnitude equivalent to missing sign only. Reject false-premise questions unless both excerpts support explicitly rejecting that premise. Limit the reference to facts directly requested by the question.\n'
    core.SINGLE_CONTEXT_REVIEW=v2.SINGLE
    core.PAIR_PLANNING=v3.PLANNER+'''\nUse short context IDs E0..E5 exactly. If require_cross_source is true one selected ID MUST be E0. If false, any TWO distinct sections from the provided primary-source document may be selected; do not force E0 when two other excerpts form a better genuine dependency. Return viable:false rather than violate the source constraint.'''
    core.PAIR_PLANNING+='\nSCOPE/LOGIC GATE: Never transfer surface METAR to upper-air radiosonde values, CAT062 geometric rate to an unrelated TIS implementation, PIREP to ASRP, AGL to MSL/standard-atmosphere height, or an accident observation to universal impossibility. A 95% interval is not a hard maximum. Do not confuse max representable value, resolution and uncertainty. Forecast horizon is not necessarily bulletin validity. Do not equate 1/4 degree with 25 percent. Never use one go/no-go blocker and an unrelated extra blocker as a multi-hop dependency. Prefer same-quantity contrasts under distinct conditions, code/physical-datum interpretation, or supported quantitative composition. Preserve may/should/ideally; do not turn recommendations into certification requirements. Do not make missing magnitude equivalent to missing sign only. Reject false-premise questions unless both excerpts support explicitly rejecting that premise. Limit the reference to facts directly requested by the question.\n'
    core.PLANNING_CANDIDATES=alternatives;meter.CampaignBudget=extension.ExtendedBudget
    with(core.OUT/'campaign.lock').open('a')as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        policy=core.read(core.OUT/'policy.json');approval=core.read(core.OUT/'request-extension-approval.json')
        record={'revision':5,'driver_sha256':core.sha(Path(__file__)),'core_v5_sha256':core.sha(Path(core.__file__)),
                'budget_extension_sha256':core.sha(Path(extension.__file__)),'original_policy_sha256':core.sha(core.OUT/'policy.json'),
                'request_extension_approval_sha256':core.sha(core.OUT/'request-extension-approval.json'),
                'reason':'Match explicit technical themes before evidence feasibility planning, use short context IDs to avoid copy errors, and permit any two distinct same-document sections. Preserve all source-removal, answer-leakage and minimal-reference checks.',
                'additional_scope_gate':'\nSCOPE/LOGIC GATE: Never transfer surface METAR to upper-air radiosonde values, CAT062 geometric rate to an unrelated TIS implementation, PIREP to ASRP, AGL to MSL/standard-atmosphere height, or an accident observation to universal impossibility. A 95% interval is not a hard maximum. Do not confuse max representable value, resolution and uncertainty. Forecast horizon is not necessarily bulletin validity. Do not equate 1/4 degree with 25 percent. Never use one go/no-go blocker and an unrelated extra blocker as a multi-hop dependency. Prefer same-quantity contrasts under distinct conditions, code/physical-datum interpretation, or supported quantitative composition. Preserve may/should/ideally; do not turn recommendations into certification requirements. Do not make missing magnitude equivalent to missing sign only. Reject false-premise questions unless both excerpts support explicitly rejecting that premise. Limit the reference to facts directly requested by the question.\n','topic_patterns':TOPICS,'original_deadline_preserved':True,'original_ledger_preserved':True,
                'generation_and_review_only':True,'retrieval_strategy_scores_used':False}
        p=core.OUT/'generation-revision-5.json'
        if p.exists():assert core.read(p)==record
        else:core.save(p,record)
        with sqlite3.connect(core.OUT/'calls.sqlite',timeout=20)as db:
            db.execute('begin immediate');count,units,charge=db.execute('select count(*),sum(reserved),sum(charge)from calls').fetchone()
            assert not db.execute("select count(*)from calls where state='admitted'").fetchone()[0]
            assert count<approval['max_requests']and units<policy['max_conservative_units']and charge<policy['max_provider_tokens']and time.time()<policy['expires_at']
            row=db.execute("select value from meta where key='exhausted'").fetchone()
            assert row is None or row[0]=='quality_review_pause'
            if row:db.execute("delete from meta where key='exhausted'and value='quality_review_pause'")
        campaign.run(args.limit)


if __name__=='__main__':main()
