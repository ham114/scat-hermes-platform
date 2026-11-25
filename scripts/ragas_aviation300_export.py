"""Package only final public-document candidates and their accepted-job evidence."""
from contextlib import closing
from datetime import datetime
import hashlib,json,shutil,sqlite3,time,zipfile
from pathlib import Path
from urllib.request import build_opener,ProxyHandler
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/'reports/batch-20261124-aviation300'
P=ROOT.parent/'scat-hermes-platform'
def read(p):return json.loads(p.read_text())
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def save(p,v):p.write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n')

def main():
    audit=read(OUT/'audit.json');similarity=read(OUT/'similarity-audit.json');policy=read(OUT/'policy.json')
    assert audit['status']=='passed' and audit['candidates']==300
    assert sha(OUT/'candidates.jsonl')==audit['candidates_sha256']==similarity['candidates_sha256']
    cases=[json.loads(x) for x in (OUT/'candidates.jsonl').read_text().splitlines()]
    with build_opener(ProxyHandler({})).open('http://127.0.0.1:18133/health/ready',timeout=10) as response:health=json.load(response)
    assert health['corpus_sha256']==policy['corpus_sha256'] and health['chunk_count']==8173
    active=[]
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():continue
        try:args=(proc/'cmdline').read_bytes().split(b'\0')
        except (OSError,PermissionError):continue
        if any(Path(x.decode(errors='replace')).name in ('ragas_aviation300.py','ragas_aviation300_refine.py') for x in args if x):active.append(proc.name)
    assert not active,active
    with closing(sqlite3.connect(f'file:{OUT}/calls.sqlite?mode=ro',uri=True)) as db:
        exceptional=[]
        for identity,job,inp,out in db.execute("select id,job,input,output from calls where state!='returned'"):
            p=OUT/'requests'/(identity+'.json')
            exceptional.append({'job':job,'input_tokens':inp,'output_tokens':out,'finish_reason':read(p).get('finish_reason') if p.exists() else 'no_known_response'})
    notes={'scope':'Assistant construction review, not independent human admission or QA scoring',
        'quality_v2':{'removed_candidates':len(read(OUT/'quality-v2-removed.json')['removed']),
            'reason':'Replace templated boundary questions, non-quantitative questions in numeric slots and one over-specified reference; original outputs retained.'},
        'quality_v3':{'removed_candidates':len(read(OUT/'quality-v3-removed.json')['removed']),
            'reason':'Replace two protocol bit-layout questions affected by flattened-table ambiguity and one reference with irrelevant glideslope/decision-height detail.'},
        'type_adjudications':read(OUT/'assistant-type-adjudications.json')['records'],
        'flag_dispositions':[
            {'qid':'AV300-20261124-030','decision':'retain','reason':'Long English multi-context question retained as a minority long-condition sample; remains below the 1000-character project input limit.'},
            {'qid':'AV300-20261124-040','decision':'retain numeric','reason':'Asks magnetic-direction runway numbering with nearest-10-degree rounding.'},
            {'qid':'AV300-20261124-043','decision':'retain numeric','reason':'Asks explicit two-bit encoding values.'},
            {'qid':'AV300-20261124-101','decision':'retain numeric','reason':'Asks the magnitude of a temperature bias in kelvin.'},
            {'qid':'AV300-20261124-200','decision':'retain numeric','reason':'Asks the 19-year averaging period.'},
            {'qid':'AV300-20261124-254','decision':'retain numeric; prioritize review','reason':'Asks the balloon upper-wind observation altitude. Before scoring, distinguish the core 35 km-and-above statement from optional GBON pressure-level details.'}],
        'gold_warning':'References and required_claims are model-reviewed candidate labels. Confirm minimum necessary answer points before evaluation; optional explanations must not automatically become required scoring points.',
        'same_provider_initial_review_only':True,'human_approved':0}
    save(OUT/'assistant-quality-notes.json',notes)
    closeout={'status':'generation-complete','completed_at':datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
        'candidates':300,'requests':audit['requests'],'provider_total_tokens':audit['provider_input_tokens']+audit['provider_output_tokens'],
        'exceptional_calls':exceptional,'generation_processes_remaining':active,'production_health_unchanged':health,
        'qa_system_calls':0,'external_model_calls_in_closeout':0,'training_runs':0,'human_approved':0}
    save(OUT/'closeout.json',closeout)
    target=OUT/'delivery';target.mkdir(exist_ok=True)
    names=['candidates.jsonl','ragas-testset.json','plan.json','policy.json','preflight.json','summary.json','audit.json','similarity-audit.json','assistant-type-adjudications.json','assistant-quality-notes.json','closeout.json','final-native-export.json','quality-v2-removed.json','quality-v3-removed.json']
    for name in names:shutil.copy2(OUT/name,target/name)
    for c in cases:
        job=c['generation']['job']
        for suffix in ('scenario','ragas','review'):
            source=OUT/'jobs'/f'{job}.{suffix}.json';dest=target/'jobs'/source.name;dest.parent.mkdir(exist_ok=True);shutil.copy2(source,dest)
    (target/'sources').mkdir(exist_ok=True)
    for p in (ROOT/'scripts').glob('ragas_aviation*.py'):shutil.copy2(p,target/'sources'/p.name)
    shutil.copy2(OUT/'calls.sqlite',target/'calls.sqlite')
    manifest={'scope':'300 native Ragas generated single-turn candidates, not approved benchmark','files':{p.relative_to(target).as_posix():sha(p) for p in sorted(target.rglob('*')) if p.is_file() and p.name!='evidence-manifest.json'},'excluded_from_delivery':['.env','old-question-fingerprints.json','all rejected-job request data'],'corpus_sha256':policy['corpus_sha256']}
    save(target/'evidence-manifest.json',manifest)
    archive=OUT/'aviation300-evidence.zip'
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
        for p in sorted(target.rglob('*')):
            if p.is_file():z.write(p,p.relative_to(target).as_posix())
    print(json.dumps({'status':'packaged','candidates':300,'files':len(manifest['files'])+1,'bytes':archive.stat().st_size,'sha256':sha(archive),'closeout':closeout},ensure_ascii=False))

if __name__=='__main__':main()
