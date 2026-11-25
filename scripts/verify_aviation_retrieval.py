"""No-LLM retrieval smoke tests plus frozen P0 single-turn retrieval regression."""
from __future__ import annotations
import argparse,collections,hashlib,json,statistics,time
from pathlib import Path
from urllib.request import Request,build_opener,ProxyHandler

ROOT=Path(__file__).resolve().parents[1]
TARGET=ROOT/'var/rag/corpora/kg_20261123_aviation_v5'
OUT=ROOT/'var/validation/20261123-aviation-v5'
PROBES=[
 ('CAT-EN','eurocontrol-asterix-cat062','In ASTERIX Category 062, what is the encoding resolution of I062/136 Measured Flight Level?'),
 ('CAT-ZH','eurocontrol-asterix-cat062','ASTERIX CAT062 中 I062/185 的速度分辨率是多少，y轴指向哪里？'),
 ('PHAK-EN','faa-phak',"According to the FAA Pilot Handbook of Aeronautical Knowledge, what is the difference between pressure altitude and density altitude?"),
 ('PHAK-ZH','faa-phak','FAA飞行员航空知识手册中，迎角与机翼弦线、相对气流是什么关系？'),
 ('PCG-EN','faa-pilot-controller-glossary','How does the FAA Pilot/Controller Glossary define flight level?'),
 ('PCG-ZH','faa-pilot-controller-glossary','FAA飞行员管制员术语表如何区分 HEADING 与 TRACK？'),
 ('IFH-EN','faa-instrument-flying-handbook','In the FAA Instrument Flying Handbook, how does the attitude indicator show pitch and bank?'),
 ('IFH-ZH','faa-instrument-flying-handbook','FAA仪表飞行手册中，静压系统堵塞会怎样影响高度表和垂直速度表？'),
 ('AWH-EN','faa-aviation-weather-handbook','What does the FAA Aviation Weather Handbook explain about microbursts and low-level wind shear?'),
 ('AWH-ZH','faa-aviation-weather-handbook','FAA航空气象手册如何解释山地波与晴空湍流？'),
 ('AIM-EN','faa-aim','According to FAA AIM, how does RNP differ from RNAV with respect to onboard performance monitoring and alerting?'),
 ('AIM-ZH','faa-aim','FAA AIM 中什么是标准仪表离场程序 SID 和标准终端进场程序 STAR？'),
 ('WMO-EN','wmo-no8-volume1','In WMO No. 8 Volume I, how is surface wind direction referenced to true north and averaged?'),
 ('WMO-ZH','wmo-no8-volume1','WMO第8号指南第一卷对地面风观测的仪器暴露高度有什么要求？'),
 ('ERA5-EN','ecmwf-era5-documentation','In ERA5 data documentation, what are the units of geopotential and how do pressure levels differ from model levels?'),
 ('ERA5-ZH','ecmwf-era5-documentation','ECMWF ERA5 文档中的 geopotential 是什么单位，是否等同于以米表示的高度？'),
]

def read(p):return json.loads(p.read_text(encoding='utf8'))
def save(p,v):p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,choices=[18133,18134],default=18134);parser.add_argument('--smoke-only',action='store_true');args=parser.parse_args()
    corpus=TARGET/'rag_chunks.v1.jsonl';rows={r['chunk_id']:r for r in map(json.loads,corpus.read_text().splitlines())}
    digest=sha(corpus);opener=build_opener(ProxyHandler({}));base=f'http://127.0.0.1:{args.port}'
    with opener.open(base+'/health/ready',timeout=15) as response:health=json.load(response)
    assert health['corpus_sha256']==digest and health['generation_id']==TARGET.name
    directory=OUT/f'retrieval-{args.port}';directory.mkdir(parents=True,exist_ok=True)
    def query(case_id,text):
        p=directory/(case_id+'.json')
        if p.exists():
            v=read(p);assert v['corpus_sha256']==digest and v['query']==text;return v
        started=time.monotonic()
        request=Request(base+'/search',data=json.dumps({'query':text,'limit':10}).encode(),headers={'Content-Type':'application/json'})
        try:
            with opener.open(request,timeout=60) as response:payload=json.load(response)
            assert payload['corpus_sha256']==digest
            ranked=payload['results']
            for item in ranked:
                assert all(item[k]==rows[item['chunk_id']][k] for k in ('source_id','source_version','text','text_sha256'))
            v={'status':'passed','query':text,'corpus_sha256':digest,'results':ranked,'seconds':time.monotonic()-started}
        except Exception as exc:v={'status':'failed','query':text,'corpus_sha256':digest,'results':[],'error_type':type(exc).__name__,'error':str(exc),'seconds':time.monotonic()-started}
        save(p,v);return v
    probes=[]
    save(OUT/'probes-preregistration.json',{'kind':'source presence retrieval smoke, not answer accuracy','probes':PROBES,'retrieval_parameters_changed':False,'external_llm_calls':0})
    for case_id,source,text in PROBES:
        result=query(case_id,text);rank=next((i+1 for i,x in enumerate(result['results']) if x['source_id']==source),None)
        probes.append({'case_id':case_id,'source_id':source,'execution_status':result['status'],'source_rank':rank,'source_present_at_10':rank is not None})
        print(json.dumps(probes[-1]),flush=True)
    smoke={'health':health,'probes':probes,'source_presence_passed':sum(x['source_present_at_10'] for x in probes),'total':len(probes),'external_llm_calls':0}
    save(directory/'smoke-summary.json',smoke)
    if args.smoke_only:return
    cases=[(split,c) for split,name in [('reviewed_new','evaluation.json'),('development_regression','regression.json')] for c in read(ROOT/'evaluations/rag_p0_reviewed_20261123_v1'/name)['cases'] if not c.get('turns')]
    results=[]
    for split,case in cases:
        result=query(case['case_id'],case['question']);ids=[x['chunk_id'] for x in result['results']]
        groups=case['supporting_evidence_groups'];metrics={}
        for k in (5,10):
            coverage=sum(set(g)<=set(ids[:k]) for g in groups)/len(groups)
            metrics[f'group_coverage_at_{k}']=coverage;metrics[f'complete_at_{k}']=coverage==1
        results.append({'case_id':case['case_id'],'split':split,'status':result['status'],'seconds':result['seconds'],**metrics})
    groups={}
    for split in ('reviewed_new','development_regression'):
        rs=[r for r in results if r['split']==split]
        groups[split]={'cases':len(rs),'execution_passed':sum(r['status']=='passed' for r in rs),
            **{f'complete_at_{k}':sum(r[f'complete_at_{k}'] for r in rs) for k in (5,10)},
            **{f'group_coverage_at_{k}':statistics.mean(r[f'group_coverage_at_{k}'] for r in rs) for k in (5,10)}}
    summary={'generation_id':TARGET.name,'corpus_sha256':digest,'kind':'frozen P0 single-turn retrieval regression; no QA rerun',
        'groups':groups,'cases':results,'p50_seconds':statistics.median(r['seconds'] for r in results),
        'external_llm_calls':0,'dialogues_not_repeated':True,'smoke':smoke}
    save(directory/'regression-summary.json',summary);print(json.dumps({'groups':groups,'smoke_passed':smoke['source_presence_passed']}),flush=True)

if __name__=='__main__':main()
