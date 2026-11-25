"""Export final QA evidence only; no model requests and no credentials in files."""
from contextlib import closing
import hashlib,json,shutil,sqlite3,sys,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'var/validation/20261124-rag-aviation300'
DATA=ROOT/'evaluations/rag_aviation300_reviewed_20261124_v1'
sys.path.insert(0,str(ROOT/'src'))
def read(p):return json.loads(p.read_text())
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def save(p,v):p.write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n')
def main():
    audit=read(OUT/'qa-audit.json');final=read(OUT/'qa-final.json')
    assert audit['status']=='passed' and final['status']=='completed' and audit['checked_cases']==300
    with closing(sqlite3.connect(f'file:{OUT}/qa-ledger.sqlite?mode=ro',uri=True)) as db:
        assert db.execute('select count(*) from calls').fetchone()[0]==audit['requests']
        assert db.execute("select count(*) from calls where state='admitted'").fetchone()[0]==0
    package=OUT/'delivery';package.mkdir(exist_ok=True)
    names=['qa-final.json','qa-audit.json','qa-answers.json','qa-diagnostics.json','qa-policy.json',
        'qa-inspection.json','scoring-rubric.json','scoring-rubric-manifest.json',
        'retrieval-summary.json','preparation.json','launch-status.json','diagnostic-adjudication.json']
    for name in names:shutil.copy2(OUT/name,package/name)
    target=package/'frozen-dataset';target.mkdir(exist_ok=True)
    for p in DATA.glob('*.json'):shutil.copy2(p,target/p.name)
    for directory in ['judge','gold-calibration','gold-rubric','case-results','retrieval']:
        target=package/directory;target.mkdir(exist_ok=True)
        for p in (OUT/directory).glob('*.json'):shutil.copy2(p,target/p.name)
    for c in final['cases']:
        folder=OUT/'qa'/c['case_id']
        for rel in ['turn-1.request.json','turn-1.admitted.json','turn-1/result.json','turn-1/native-result.json']:
            p=folder/rel
            if p.exists():
                target=package/'qa'/c['case_id']/rel;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,target)
    target=package/'sources';target.mkdir(exist_ok=True)
    for p in (ROOT/'scripts').glob('aviation300_qa_*.py'):shutil.copy2(p,target/p.name)
    shutil.copy2(OUT/'qa-ledger.sqlite',package/'qa-ledger.sqlite')
    from dotenv import dotenv_values
    from hermes_runtime.profiles import profile_home
    keys={dotenv_values(ROOT.parent/'ragas-evaluation/.env').get('DEEPSEEK_API_KEY'),
        dotenv_values(profile_home(ROOT,'scat-main')/'.env').get('DEEPSEEK_API_KEY')}
    keys=[k.encode() for k in keys if k and len(k)>10]
    scanned=0
    for p in package.rglob('*'):
        if p.is_file():
            assert all(k not in p.read_bytes() for k in keys),'credential_detected_do_not_export'
            scanned+=1
    manifest={'status':'final-evidence-package','cases':300,'individual_human_spot_checks_approved':5,
        'model_review_not_independent_human':True,'files':{p.relative_to(package).as_posix():sha(p) for p in sorted(package.rglob('*')) if p.is_file() and p.name!='evidence-manifest.json'},
        'secret_scan_files':scanned,'credential_matches':0,'external_calls_in_export':0}
    save(package/'evidence-manifest.json',manifest)
    archive=OUT/'aviation300-qa-evidence.zip'
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
        for p in sorted(package.rglob('*')):
            if p.is_file():z.write(p,p.relative_to(package).as_posix())
    print(json.dumps({'archive':str(archive),'bytes':archive.stat().st_size,'sha256':sha(archive),'files':scanned+1,'requests':audit['requests']}))
if __name__=='__main__':main()
