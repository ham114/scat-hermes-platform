"""Package already-computed native Ragas results; no inference or network calls."""
import hashlib,importlib.metadata,json,shutil,sqlite3,sys,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];OUT=ROOT.parent/'ragas-evaluation/reports/standard-reeval-20261124'
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def read(p):return json.loads(p.read_text())
def main():
    audit=read(OUT/'audit.json');final=read(OUT/'final.json')
    assert audit['status']=='passed' and final['status']=='completed'
    with sqlite3.connect('file:'+str(OUT/'calls.sqlite')+'?mode=ro',uri=True) as db:
        assert db.execute('select count(*) from calls').fetchone()[0]==audit['requests']
    target=OUT/'delivery';target.mkdir(exist_ok=True)
    for name in ['inputs.jsonl','input-manifest.json','jobs.json','preparation.json','preflight.json','policy.json','policy-draft.json','approval.json','pilot-summary.json','final.json','audit.json','summary.json','metric-details.json','calls.sqlite','embeddings.sqlite','launch-status.json']:
        shutil.copy2(OUT/name,target/name)
    for folder in ['calls','results']:
        dest=target/folder;dest.mkdir(exist_ok=True)
        for p in (OUT/folder).glob('*.json'):shutil.copy2(p,dest/p.name)
    (target/'sources').mkdir(exist_ok=True)
    for name in ['ragas_standard_rescore.py','ragas_rescore_embedding.py','ragas_rescore_audit.py','ragas_rescore_export.py','ragas_rescore_launch.py','ragas_aviation_budget.py']:
        shutil.copy2(ROOT/'scripts'/name,target/'sources'/name)
    versions={}
    for name in ['ragas','openai','instructor','pydantic','numpy']:
        versions[name]=importlib.metadata.version(name)
    (target/'environment.json').write_text(json.dumps({'python':sys.version,'packages':versions,'embedding':read(OUT/'preflight.json')['embedding']},indent=2)+'\n')
    from dotenv import dotenv_values
    key=dotenv_values(ROOT.parent/'ragas-evaluation/.env').get('DEEPSEEK_API_KEY');assert key and len(key)>10
    files={}
    for p in sorted(target.rglob('*')):
        if not p.is_file() or p.name=='evidence-manifest.json':continue
        assert key.encode() not in p.read_bytes(),'credential_found_do_not_export'
        files[p.relative_to(target).as_posix()]=sha(p)
    manifest={'scope':'Native Ragas standard metrics on frozen saved QA answers; no new QA; continuous values, not accuracy or pass rate',
        'cases':426,'turns':439,'files':files,'credential_matches':0,'external_model_calls_in_export':0}
    (target/'evidence-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    archive=OUT/'ragas-standard-evidence.zip'
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
        for p in sorted(target.rglob('*')):
            if p.is_file():z.write(p,p.relative_to(target).as_posix())
    print(json.dumps({'archive':str(archive),'files':len(files)+1,'bytes':archive.stat().st_size,'sha256':sha(archive),'requests':audit['requests']}))
if __name__=='__main__':main()
