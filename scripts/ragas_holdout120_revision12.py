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
import ragas_holdout120_core_v12 as core
import ragas_aviation_budget as meter
import ragas_holdout_budget_extension_v4 as extension
from ragas_holdout_budget_extension_v4 import effective_policy as approved_policy
import socket,ssl

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
_original_reliable=v3.reliable
def reliable_without_equation_damage(row):
    return _original_reliable(row) and '$$'not in row['text']
v3.reliable=reliable_without_equation_damage


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


_MANUAL={}
_RESERVED=set()
_auto_pair=pair
_auto_alternatives=alternatives

def validate_manual_plans():
    global _MANUAL,_RESERVED
    config=core.read(core.OUT/'assistant-source-plans-v3.json');assert config['validated']is True and config['corpus_sha256']==core.SHA
    _MANUAL=config['items'];_RESERVED={cid for p in _MANUAL.values()for cid in p['chunks']}
    assert len(_RESERVED)==2*len(_MANUAL)
    _,rows=core.load_rows();byid={r['chunk_id']:r for r in rows};pools=core.read(core.OUT/'selection-pools.json');eligible={r['chunk_id']for group in pools.values()for r in group}
    slots={str(r['slot']):r for r in core.read(core.OUT/'slots.json')};state=core.read(core.OUT/'state.json')
    gold_owner={e['chunk_id']:str(c['slot'])for c in state['accepted']for cl in c['claims']for e in cl['evidence']}
    for key,p in _MANUAL.items():
        slot=slots[key];a,b=[byid[cid]for cid in p['chunks']]
        assert set(p['chunks'])<=eligible and len(set(p['chunks']))==2
        assert a['source_id']==slot['primary_source'] and len({a['source_id'],b['source_id']})==(2 if slot['require_cross_source']else 1)
        assert a['section_path'][-3:]!=b['section_path'][-3:] and len(p['goal'])>30
        assert len(p['fact_quotes'])==2
        for fact in p['fact_quotes']:
            assert fact['chunk_id']in p['chunks'] and 12<=len(fact['quote'])<=600 and fact['quote']in byid[fact['chunk_id']]['text']
        assert {f['chunk_id']for f in p['fact_quotes']}==set(p['chunks'])
        for cid in p['chunks']:assert cid not in gold_owner or gold_owner[cid]==key,(key,cid,'already committed')
    core.MANUAL_SOURCE_PLANS=_MANUAL
    return {'validated_manual_source_pairs':len(_MANUAL),'official_source_quotes':2*len(_MANUAL),'old_gold_excluded':True,'model_calls':0}

def guided_pair(slot,attempt,pools,byid,vectors,used):
    key=str(slot['slot'])
    if key in _MANUAL:
        chosen=[byid[cid]for cid in _MANUAL[key]['chunks']]
        owners={e['chunk_id']:c['slot']for c in core.read(core.OUT/'state.json')['accepted']for cl in c['claims']for e in cl['evidence']}
        assert not any(r['chunk_id']in owners for r in chosen)
    else:chosen=_auto_pair(slot,attempt,pools,byid,vectors,used|_RESERVED)
    used.update(r['chunk_id']for r in chosen);return chosen

def guided_alternatives(slot,chosen,pools,byid,vectors,used):
    if str(slot['slot'])in _MANUAL:return chosen
    return _auto_alternatives(slot,chosen,pools,byid,vectors,used|_RESERVED)


def run(limit):
    campaign.configure();pre=core.read(core.OUT/'preflight.json');approval=core.read(campaign.DEST/'user-approval.json')
    assert approval['authorized'] is True and approval['user_instruction']=='再次生成120题，准备再次测评。批准额外额度'
    assert approval['preflight_sha256']==core.sha(core.OUT/'preflight.json')
    assert pre['plan_sha256']==core.sha(core.OUT/'plan.json')
    for n,h in pre['source_sha256'].items():assert core.sha(campaign.ROOT/'scripts'/n)==h
    plan=core.read(core.OUT/'plan.json')
    for p,h in plan['protected_files_sha256'].items():assert core.sha(campaign.ROOT/p)==h
    from ragas.testset.synthesizers.multi_hop.specific import MultiHopSpecificQuerySynthesizer
    MultiHopSpecificQuerySynthesizer.__dataclass_fields__['generate_query_reference_prompt'].default.instruction=campaign.GEN+campaign.QUALITY
    assert (core.OUT/'policy.json').exists()
    policy=approved_policy(core.read(core.OUT/'policy.json'),core.OUT);assert time.time()<policy['expires_at']
    original=campaign.socket.socket.connect;original_ex=campaign.socket.socket.connect_ex
    def check(sock,address):
        if sock.family in (campaign.socket.AF_INET,campaign.socket.AF_INET6):
            if address[0]!=pre['verified_dns_address'] or address[1]!=443 or time.time()>policy['expires_at']:raise RuntimeError('unapproved_generation_network_destination')
    def connect(sock,address):check(sock,address);return original(sock,address)
    def connect_ex(sock,address):check(sock,address);return original_ex(sock,address)
    campaign.socket.socket.connect=connect;campaign.socket.socket.connect_ex=connect_ex
    campaign.asyncio.run(core.run(limit))
    assert all(core.sha(campaign.ROOT/p)==h for p,h in plan['protected_files_sha256'].items())


def probe_endpoint():
    pre=core.read(core.OUT/'preflight.json');policy=approved_policy(core.read(core.OUT/'policy.json'),core.OUT)
    started=time.time();result={'at':started,'hostname':'api.deepseek.com','address':pre['verified_dns_address'],'tls_checks':0,'model_calls':0,'no_key_sent':True}
    try:
        for index in range(3):
            assert time.time()<policy['expires_at']
            with socket.create_connection((pre['verified_dns_address'],443),timeout=5)as raw:
                with ssl.create_default_context().wrap_socket(raw,server_hostname='api.deepseek.com')as tls:assert tls.getpeercert()
            result['tls_checks']+=1
            if index<2:time.sleep(3)
        result['passed']=True
    except Exception as exc:
        result.update(passed=False,error_type=type(exc).__name__)
    result['elapsed']=time.time()-started
    history=core.read(core.OUT/'provider-connectivity-checks.json')if (core.OUT/'provider-connectivity-checks.json').exists()else []
    history.append(result);core.save(core.OUT/'provider-connectivity-checks.json',history)
    assert result.get('passed')is True,'Provider transport remains unstable; no model call started'


def main():
    assert sys.executable=='/home/h3c/anaconda3/envs/ragas-eval/bin/python'
    parser=argparse.ArgumentParser();parser.add_argument('--limit',type=int,default=0);parser.add_argument('--preflight',action='store_true');parser.add_argument('--manual-only',action='store_true');args=parser.parse_args()
    campaign.core=core;campaign.pair=pair;campaign.GEN+=v2.GEN_EXTRA;campaign.REVIEW+=v2.REVIEW_EXTRA
    campaign.GEN+='\nSCOPE/LOGIC GATE: Never transfer surface METAR to upper-air radiosonde values, CAT062 geometric rate to an unrelated TIS implementation, PIREP to ASRP, AGL to MSL/standard-atmosphere height, or an accident observation to universal impossibility. A 95% interval is not a hard maximum. Do not confuse max representable value, resolution and uncertainty. Forecast horizon is not necessarily bulletin validity. Do not equate 1/4 degree with 25 percent. Never use one go/no-go blocker and an unrelated extra blocker as a multi-hop dependency. Prefer same-quantity contrasts under distinct conditions, code/physical-datum interpretation, or supported quantitative composition. Preserve may/should/ideally; do not turn recommendations into certification requirements. Do not make missing magnitude equivalent to missing sign only. Reject false-premise questions unless both excerpts support explicitly rejecting that premise. Limit the reference to facts directly requested by the question.\n';campaign.REVIEW+='\nSCOPE/LOGIC GATE: Never transfer surface METAR to upper-air radiosonde values, CAT062 geometric rate to an unrelated TIS implementation, PIREP to ASRP, AGL to MSL/standard-atmosphere height, or an accident observation to universal impossibility. A 95% interval is not a hard maximum. Do not confuse max representable value, resolution and uncertainty. Forecast horizon is not necessarily bulletin validity. Do not equate 1/4 degree with 25 percent. Never use one go/no-go blocker and an unrelated extra blocker as a multi-hop dependency. Prefer same-quantity contrasts under distinct conditions, code/physical-datum interpretation, or supported quantitative composition. Preserve may/should/ideally; do not turn recommendations into certification requirements. Do not make missing magnitude equivalent to missing sign only. Reject false-premise questions unless both excerpts support explicitly rejecting that premise. Limit the reference to facts directly requested by the question.\n'
    core.SINGLE_CONTEXT_REVIEW=v2.SINGLE
    core.PAIR_PLANNING=v3.PLANNER+'''\nUse short context IDs E0..E5 exactly. If require_cross_source is true one selected ID MUST be E0. If false, any TWO distinct sections from the provided primary-source document may be selected; do not force E0 when two other excerpts form a better genuine dependency. Return viable:false rather than violate the source constraint.'''
    core.PAIR_PLANNING+='\nSCOPE/LOGIC GATE: Never transfer surface METAR to upper-air radiosonde values, CAT062 geometric rate to an unrelated TIS implementation, PIREP to ASRP, AGL to MSL/standard-atmosphere height, or an accident observation to universal impossibility. A 95% interval is not a hard maximum. Do not confuse max representable value, resolution and uncertainty. Forecast horizon is not necessarily bulletin validity. Do not equate 1/4 degree with 25 percent. Never use one go/no-go blocker and an unrelated extra blocker as a multi-hop dependency. Prefer same-quantity contrasts under distinct conditions, code/physical-datum interpretation, or supported quantitative composition. Preserve may/should/ideally; do not turn recommendations into certification requirements. Do not make missing magnitude equivalent to missing sign only. Reject false-premise questions unless both excerpts support explicitly rejecting that premise. Limit the reference to facts directly requested by the question.\n'
    core.PLANNING_CANDIDATES=alternatives;meter.CampaignBudget=extension.ExtendedBudget
    validated=validate_manual_plans();campaign.pair=guided_pair;core.PLANNING_CANDIDATES=guided_alternatives
    core.SLOT_FILTER={int(k)for k in _MANUAL}if args.manual_only else set(range(1,121))-{int(k)for k in _MANUAL}
    core.MAX_NEW_ATTEMPTS_PER_SLOT=3 if args.manual_only else 0
    core.PAIR_PLANNING+=' An assistant_source_goal, when present, has been checked locally against the two excerpts. Follow that goal, verify the exact source facts yourself, keep the missing instance input unknown, and still return viable:false for any invalid pair. Do not create a finished question here.'
    if args.preflight:
        print(__import__('json').dumps(validated));return
    with(core.OUT/'campaign.lock').open('a')as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        policy=approved_policy(core.read(core.OUT/'policy.json'),core.OUT);approval=core.read(core.OUT/'continuation-extension-approval.json')
        record={'revision':12,'driver_sha256':core.sha(Path(__file__)),'core_v12_sha256':core.sha(Path(core.__file__)),
                'budget_extension_sha256':core.sha(Path(extension.__file__)),'original_policy_sha256':core.sha(core.OUT/'policy.json'),
                'resource_extension_approval_sha256':core.sha(core.OUT/'resource-extension-approval.json'),'continuation_extension_approval_sha256':core.sha(core.OUT/'continuation-extension-approval.json'),
                'reason':'Use the explicitly renewed policy in the core DNS expiration guard as well as the driver and ledger. Preserve source planning, native generation, review and ablation gates unchanged. Distinguish true heading TH, true course TC and magnetic course MC explicitly in the guided source goal. Native Ragas questions remain unedited; previous calls remain charged.',
                'additional_scope_gate':'\nSCOPE/LOGIC GATE: Never transfer surface METAR to upper-air radiosonde values, CAT062 geometric rate to an unrelated TIS implementation, PIREP to ASRP, AGL to MSL/standard-atmosphere height, or an accident observation to universal impossibility. A 95% interval is not a hard maximum. Do not confuse max representable value, resolution and uncertainty. Forecast horizon is not necessarily bulletin validity. Do not equate 1/4 degree with 25 percent. Never use one go/no-go blocker and an unrelated extra blocker as a multi-hop dependency. Prefer same-quantity contrasts under distinct conditions, code/physical-datum interpretation, or supported quantitative composition. Preserve may/should/ideally; do not turn recommendations into certification requirements. Do not make missing magnitude equivalent to missing sign only. Reject false-premise questions unless both excerpts support explicitly rejecting that premise. Limit the reference to facts directly requested by the question.\n','topic_patterns':TOPICS,'original_start_preserved':True,'deadline_extended_with_explicit_user_authorization':True,'original_ledger_preserved':True,
                'manual_source_plans_sha256':core.sha(core.OUT/'assistant-source-plans-v3.json'),'transport_pause_on_first_failure':True,'maximum_inflight_requests':4,'generation_and_review_only':True,'retrieval_strategy_scores_used':False}
        p=core.OUT/'generation-revision-12.json'
        if p.exists():assert core.read(p)==record
        else:core.save(p,record)
        probe_endpoint()
        with sqlite3.connect(core.OUT/'calls.sqlite',timeout=20)as db:
            db.execute('begin immediate');count,units,charge=db.execute('select count(*),sum(reserved),sum(charge)from calls').fetchone()
            assert not db.execute("select count(*)from calls where state='admitted'").fetchone()[0]
            assert count<approval['max_requests']and units<policy['max_conservative_units']and charge<policy['max_provider_tokens']and time.time()<policy['expires_at']
            row=db.execute("select value from meta where key='exhausted'").fetchone()
            assert row is None or row[0]in ['quality_review_pause','provider_transport_pause']
            if row:db.execute("delete from meta where key='exhausted'and value in ('quality_review_pause','provider_transport_pause')")
        run(args.limit)


if __name__=='__main__':main()
