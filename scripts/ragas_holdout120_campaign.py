"""New120 Ragas generation: fixed source strata, no retrieval-result feedback."""
import argparse
import ast
import asyncio
import collections
import copy
import fcntl
import hashlib
import importlib.metadata
import json
import random
import re
import socket
import ssl
import sys
import time
from pathlib import Path

import ragas_holdout120_core as core

ROOT=Path(__file__).resolve().parents[1]
_prompts={target.id:ast.literal_eval(node.value)
          for node in ast.parse((ROOT/'scripts/ragas_multihop120_revision4.py').read_text(encoding='utf8')).body
          if isinstance(node,ast.Assign) for target in node.targets
          if isinstance(target,ast.Name) and target.id in {'GEN','REVIEW'}}
GEN=_prompts['GEN'];REVIEW=_prompts['REVIEW']
OUT=core.OUT
DEST=ROOT/'output/ragas-holdout120-20261125'
QUOTAS={'faa-aim':36,'faa-instrument-flying-handbook':22,'faa-phak':18,
        'faa-aviation-weather-handbook':16,'wmo-no8-volume1':12,
        'faa-pilot-controller-glossary':6,'eurocontrol-asterix-cat062':6,
        'ecmwf-era5-documentation':4}
TYPES={'cross_document':60,'cross_chapter':30,'numeric_conditions':20,'insufficient':10}
LIMITS={'max_requests':1200,'max_provider_tokens':8000000,'max_conservative_units':40000000,
        'wall_seconds':7200,'max_output_tokens':2400}
QUALITY='''
Preserve all source qualifiers and distinguish possibility from completion, may from must, and OR from AND. Do not infer aircraft certification from weight alone or administrative approvals not stated in the excerpts. Distinguish a bound from a bias, geometric/MSL/pressure datums, mean wind from gust extrema, measurement requirements from reanalysis characteristics, and historical encoding from current operational state. Check arithmetic, units, rounding and row/column meanings. Hypothetical instance inputs are allowed but source constants and rules must be retrieved rather than supplied by the question. Do not invent a hierarchy to settle apparent manual differences. Avoid flattened acronym grids, unsupported text, anomalous source values and definition-only corroboration. Use only supplied official excerpts, never external project knowledge.'''
TYPE_HINTS={
    'cross_document':'One coherent application or same-phenomenon comparison requiring two different official document families; both nonredundant contexts necessary.',
    'cross_chapter':'One coherent application or same-phenomenon comparison requiring two distinct sections of the same named document; both nonredundant contexts necessary.',
    'numeric_conditions':'An actual numerical result, conversion, bound, encoding or quantitative comparison requiring BOTH excerpts and all applicable conditions. Supply instance inputs, retrieve source constants; no qualitative-only questions.',
    'insufficient':'A single concrete missing instance input prevents one specific conclusion after BOTH source rules are used. State the known scope and unknown input; document silence never establishes real-world nonexistence.'}


def eligible(r):
    heading=' '.join(r['section_path']).lower();text=r['text']
    return (r['source_id'] in QUOTAS and r['token_count']>=80 and len(text)>230 and
            not any(x in heading for x in ['contents','index','abbreviation','acronym','list of','acknowledg','bibliograph','references cited']) and
            not any(x in text for x in ['SOURCE ANOMALY','Table extraction unavailable','Unavailable source rows','contents navigation only']) and
            not ('AVBL' in text and 'NOTAM' in text) and len(re.findall(r'\bSee\b',text))<4)


def pair(slot,attempt,pools,byid,vectors,used):
    import numpy as np
    pool=[r for r in pools[slot['primary_source']] if r['chunk_id'] not in used]
    if slot['category']=='numeric_conditions':
        numeric=[r for r in pool if re.search(r'\d',byid[r['chunk_id']]['text']) and re.search(
            r'\b(ft|feet|FL|LSB|hPa|knots?|kg|Kelvin|mm|m/s|percent|temperature|altitude|pressure|uncertainty|resolution)\b',byid[r['chunk_id']]['text'],re.I)]
        if numeric:pool=numeric
    assert pool,'No unused primary evidence'
    offset=(slot['slot']*29+attempt*31)%len(pool);pool=pool[offset:]+pool[:offset]
    other=[r for group in pools.values() for r in group if r['chunk_id'] not in used and
           (r['source_id']!=slot['primary_source'] if slot['require_cross_source'] else r['source_id']==slot['primary_source'])]
    assert other,'No unused secondary evidence'
    firsts=pool[:160];indices=np.array([r['index'] for r in other])
    matrix=vectors[[r['index'] for r in firsts]]@vectors[indices].T
    options=[]
    for i,first in enumerate(firsts):
        for j in np.argsort(-matrix[i],kind='stable')[:30]:
            second=other[j];score=float(matrix[i,j])
            if .52<score<.86 and first['section']!=second['section'] and first['chunk_id']!=second['chunk_id']:
                options.append((score,i,first,second));break
    assert options,'No coherent distinct-section evidence pair'
    options.sort(key=lambda x:(-x[0],x[1]));_,_,first,second=options[(slot['slot']+attempt)%min(12,len(options))]
    used.update([first['chunk_id'],second['chunk_id']])
    return [byid[first['chunk_id']],byid[second['chunk_id']]]


def configure():
    core.QUOTAS=QUOTAS;core.TYPES=TYPES;core.TYPE_HINTS=TYPE_HINTS
    core.REVIEW=REVIEW+QUALITY;core.select_rows=pair


def prepare():
    import numpy as np
    assert not (OUT/'plan.json').exists(),'frozen new campaign preserved'
    configure();allrows,rows=core.load_rows();byid={r['chunk_id']:r for r in rows}
    old=[];used=set();old_files=[]
    def evidence_ids(value):
        if isinstance(value,dict):
            if isinstance(value.get('chunk_id'),str):used.add(value['chunk_id'])
            for item in value.values():evidence_ids(item)
        elif isinstance(value,list):
            for item in value:evidence_ids(item)
    for folder in ['rag_p0_reviewed_20261123_v1','rag_p0_reviewed_20261124_v2',
                   'rag_aviation300_reviewed_20261124_v1','rag_aviation300_reviewed_20261124_v2',
                   'rag_multihop120_candidates_20261124_v1']:
        for name in ['evaluation.json','regression.json']:
            p=ROOT/'evaluations'/folder/name
            if not p.exists():continue
            old_files.append(p)
            for c in core.read(p)['cases']:
                evidence_ids(c)
                old.append(c['question']);old.extend(t['question'] for t in c.get('turns',[]))
                for cl in c.get('claims',[]):used.update(e['chunk_id'] for e in cl.get('evidence',[]))
                used.update(cid for g in c.get('supporting_evidence_groups',[]) for cid in g)
    old=list(dict.fromkeys(old));pools={s:[] for s in QUOTAS}
    for i,r in enumerate(allrows):
        if r['chunk_id'] not in used and eligible(r):pools[r['source_id']].append({
            'chunk_id':r['chunk_id'],'index':i,'source_id':r['source_id'],'section':r['section_path'][-3:]})
    rng=random.Random(20261125120)
    for p in pools.values():rng.shuffle(p)
    assert all(len(pools[s])>=QUOTAS[s]*3 for s in QUOTAS)
    tasks=[]
    for category,n in TYPES.items():
        for j in range(n):tasks.append({'category':category,'require_cross_source':category=='cross_document' or category in ['numeric_conditions','insufficient'] and j<n//2})
    rng.shuffle(tasks);remaining=dict(QUOTAS);slots=[]
    for i,t in enumerate(tasks):
        sid=max((s for s in QUOTAS if remaining[s]),key=lambda s:(remaining[s]/QUOTAS[s],remaining[s],s));remaining[sid]-=1
        slots.append({'slot':i+1,**t,'primary_source':sid,'language':'en' if i%5==4 else 'zh-CN'})
    core.save(OUT/'slots.json',slots);core.save(OUT/'selection-pools.json',pools)
    core.save(OUT/'old-question-fingerprints.json',[{'question':q,'normalized':core.norm(q)}for q in old])
    core.save(OUT/'prior-evidence-exclusions.json',{'chunk_ids':sorted(used),'old_questions_local_only':True})
    vectors=np.load(core.CORPUS/'bge-m3.npy',mmap_mode='r');selected=set();scenarios=[]
    for s in slots:
        chosen=pair(s,1,pools,byid,vectors,selected)
        assert not any(r['chunk_id'] in used for r in chosen)
        scenarios.append({'slot':s['slot'],'chunks':[r['chunk_id'] for r in chosen]})
    core.save(OUT/'planned-scenarios.json',scenarios)
    protected_paths=[ROOT/'services/rag/qwen3-production-v1.json',ROOT/'services/rag/real-agent-production-v1.json',
                     ROOT/'src/rag_retrieval/baseline.py',ROOT/'output/rag-fusion-serial-20261125/summary.json']
    # Existence/hash protection only; generator never reads prior performance values.
    for p in [ROOT/'var/validation/20261124-ragas-multihop120/calls.sqlite',ROOT/'var/validation/20261124-rag-multihop120-eval/qa-ledger.sqlite']:
        if p.exists():protected_paths.append(p)
    plan={'target':120,'batch':'HO120-20261125','corpus_generation':core.CORPUS.name,'corpus_sha256':core.SHA,
          'source_quotas':QUOTAS,'category_targets':TYPES,'languages':{'zh-CN':96,'en':24},
          'minimum_cross_document':75,'native_generation':'Ragas0.4.3 MultiHopSpecificQuerySynthesizer.generate_sample',
          'eligible_by_source':{s:len(p) for s,p in pools.items()},'prior_question_forms':len(old),
          'prior_evidence_ids_excluded':len(used),'no_previous_gold_chunks_reused':True,
          'previous_question_files_sha256':{p.relative_to(ROOT).as_posix():core.sha(p) for p in old_files},
          'protected_files_sha256':{p.relative_to(ROOT).as_posix():core.sha(p) for p in protected_paths},
          'generation_instruction':GEN+QUALITY,'review_instruction':REVIEW+QUALITY,'seed':20261125120,
          'no_retrieval_or_reranker_results_used_to_select_questions':True,
          'limits':LIMITS,'destination':'https://api.deepseek.com','existing_dedicated_key_stays_on_server':True,
          'old_questions_local_only':True,'official_documents_only':True,
          'source_exclusions':['all historical gold chunk IDs','TOCs/indices/acronyms','unsupported extraction notices','flagged source values','known NOTAM AVBL source anomaly'],
          'scope':'new question/reference generation, source-grounded review and local dedup only; no QA evaluation, strategy replay, training, business operations or production changes',
          'review_kind':'same-provider fresh-context model initial review plus assistant source audit; not independent human admission',
          'pairing_limit':'Local BGE passage similarity helps select related excerpts; this is not a fully independent human-authored benchmark.',
          'external_model_calls':0}
    core.save(OUT/'plan.json',plan);DEST.mkdir(parents=True,exist_ok=True);core.save(DEST/'plan.json',plan)
    print(json.dumps({'target':120,'old_questions':len(old),'excluded_old_gold_ids':len(used),'eligible':plan['eligible_by_source'],'calls':0}),flush=True)


def preflight():
    from ragas.testset.graph import Node,NodeType
    from ragas.testset.persona import Persona
    from ragas.testset.synthesizers.multi_hop.base import MultiHopScenario
    from ragas.testset.synthesizers.base import QueryStyle,QueryLength
    from ragas.testset.synthesizers.multi_hop.specific import MultiHopSpecificQuerySynthesizer
    assert importlib.metadata.version('ragas')=='0.4.3'
    allrows,rows=core.load_rows();byid={r['chunk_id']:r for r in rows};plan=core.read(OUT/'plan.json')
    for scenario in core.read(OUT/'planned-scenarios.json'):
        assert len(set(scenario['chunks']))==2 and all(cid in byid for cid in scenario['chunks'])
    sample=core.read(OUT/'planned-scenarios.json')[0]
    scenario=MultiHopScenario(nodes=[Node(type=NodeType.CHUNK,properties={'page_content':byid[c]['text']})for c in sample['chunks']],
        combinations=['rule','condition'],persona=Persona(name='Aviation analyst',role_description='Combine official evidence'),style=QueryStyle.PERFECT_GRAMMAR,length=QueryLength.MEDIUM)
    assert len(scenario.nodes)==2
    MultiHopSpecificQuerySynthesizer.__dataclass_fields__['generate_query_reference_prompt'].default.instruction=GEN+QUALITY
    key,model=core.credentials()
    resolution_method='server_dns'
    try:addresses=[socket.gethostbyname('api.deepseek.com')]
    except socket.gaierror:
        seed=core.read(OUT/'dns-candidates.json')
        assert seed['hostname']=='api.deepseek.com' and 0<=time.time()-seed['resolved_at']<3600
        addresses=seed['ipv4_addresses'];resolution_method='fresh_client_dns_then_server_tls_verification'
    address=None
    for candidate in addresses:
        try:
            with socket.create_connection((candidate,443),timeout=12) as raw:
                with ssl.create_default_context().wrap_socket(raw,server_hostname='api.deepseek.com') as tls:assert tls.getpeercert()
            address=candidate;break
        except OSError:continue
    assert address,'No TLS-verified DeepSeek endpoint reachable'
    sources=[Path(__file__),Path(core.__file__),ROOT/'scripts/ragas_aviation_budget.py',ROOT/'scripts/ragas_multihop120_revision4.py']
    result={'status':'passed','ragas_version':'0.4.3','key_present':bool(key),'model':model,
            'verified_dns_address':address,'resolution_method':resolution_method,'tls_hostname_verified':True,'plan_sha256':core.sha(OUT/'plan.json'),
            'source_sha256':{p.name:core.sha(p) for p in sources},'model_calls':0}
    core.save(OUT/'preflight.json',result);core.save(DEST/'preflight.json',result)
    print(json.dumps(result),flush=True)


def run(limit):
    configure();pre=core.read(OUT/'preflight.json');approval=core.read(DEST/'user-approval.json')
    assert approval['authorized'] is True and approval['user_instruction']=='再次生成120题，准备再次测评。批准额外额度'
    assert approval['preflight_sha256']==core.sha(OUT/'preflight.json')
    assert pre['plan_sha256']==core.sha(OUT/'plan.json')
    for n,h in pre['source_sha256'].items():assert core.sha(ROOT/'scripts'/n)==h
    plan=core.read(OUT/'plan.json')
    for p,h in plan['protected_files_sha256'].items():assert core.sha(ROOT/p)==h
    from ragas.testset.synthesizers.multi_hop.specific import MultiHopSpecificQuerySynthesizer
    MultiHopSpecificQuerySynthesizer.__dataclass_fields__['generate_query_reference_prompt'].default.instruction=GEN+QUALITY
    if not (OUT/'policy.json').exists():
        now=time.time();core.save(OUT/'policy.json',{'started_at':now,'expires_at':now+LIMITS['wall_seconds'],
            **{k:v for k,v in LIMITS.items() if k!='wall_seconds'},'data_destination':'https://api.deepseek.com',
            'model':pre['model'],'corpus_sha256':core.SHA,'verified_dns_address':pre['verified_dns_address'],
            'source_sha256':{p.name:core.sha(p) for p in [Path(core.__file__),ROOT/'scripts/ragas_aviation_budget.py']},
            'plan_sha256':core.sha(OUT/'plan.json'),'private_project_documents_sent':False,'old_questions_sent':False,'qa_evaluation':False})
    policy=core.read(OUT/'policy.json');assert time.time()<policy['expires_at']
    original=socket.socket.connect;original_ex=socket.socket.connect_ex
    def check(sock,address):
        if sock.family in (socket.AF_INET,socket.AF_INET6):
            if address[0]!=pre['verified_dns_address'] or address[1]!=443 or time.time()>policy['expires_at']:raise RuntimeError('unapproved_generation_network_destination')
    def connect(sock,address):check(sock,address);return original(sock,address)
    def connect_ex(sock,address):check(sock,address);return original_ex(sock,address)
    socket.socket.connect=connect;socket.socket.connect_ex=connect_ex
    asyncio.run(core.run(limit))
    assert all(core.sha(ROOT/p)==h for p,h in plan['protected_files_sha256'].items())


def main():
    assert sys.executable=='/home/h3c/anaconda3/envs/ragas-eval/bin/python'
    import os
    os.umask(0o077);OUT.mkdir(parents=True,exist_ok=True)
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['prepare','preflight','run']);parser.add_argument('--limit',type=int,default=0);args=parser.parse_args()
    with (OUT/'campaign.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if args.action=='prepare':prepare()
        elif args.action=='preflight':preflight()
        else:run(args.limit)


if __name__=='__main__':main()
