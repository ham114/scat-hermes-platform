"""Stage, activate or roll back only this project's aviation corpus deployment."""
from __future__ import annotations
import argparse,copy,hashlib,json,os,signal,socket,subprocess,time
from contextlib import closing
from pathlib import Path
from urllib.request import build_opener,ProxyHandler

ROOT=Path(__file__).resolve().parents[1]
TARGET=ROOT/'var/rag/corpora/kg_20261123_aviation_v5'
STATE=ROOT/'var/rag/deployments/aviation-v5'
CONTAINER='scat-hermes-h7b-hermes-main-1'
QWEN=ROOT/'services/rag/qwen3-production-v1.json'
NATIVE=ROOT/'services/rag/real-agent-production-v1.json'
OPENER=build_opener(ProxyHandler({}))

def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def read(p):return json.loads(p.read_text())
def save(p,v):
    tmp=p.with_suffix(p.suffix+'.writing');tmp.write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n');tmp.replace(p)
def run(args,timeout=180):
    p=subprocess.run(args,cwd=ROOT,capture_output=True,text=True,timeout=timeout)
    if p.returncode:raise RuntimeError('Command failed: '+str(args[:3])+': '+p.stderr[-800:])
    return p.stdout.strip()
def health(port):
    try:
        with OPENER.open(f'http://127.0.0.1:{port}/health/ready' if port in (18133,18134) else f'http://127.0.0.1:{port}/health',timeout=4) as response:return json.load(response)
    except (OSError,ValueError):return None
def wait(port,digest=None,seconds=120):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        value=health(port)
        if value and (digest is None or value.get('corpus_sha256')==digest):return value
        time.sleep(1)
    raise TimeoutError('Service did not become ready: '+str(port))
def inspect():return json.loads(run(['docker','inspect',CONTAINER]))[0]

def prepare():
    ready=read(TARGET/'ready.json');assert read(TARGET/'artifact-audit.json')['status']=='passed'
    assert health(18133)['generation_id']=='kg_20261026_research_v4'
    STATE.mkdir(parents=True,exist_ok=True)
    info=inspect();compose=Path(info['Config']['Labels']['com.docker.compose.project.config_files'])
    original=read(compose);updated=copy.deepcopy(original)
    assert original['name']=='scat-hermes-h7b' and original['services']['hermes-main']['command']==['main']
    volumes=updated['services']['hermes-main']['volumes']
    assert not any(v.get('target')==str(NATIVE) for v in volumes)
    volumes.append({'type':'bind','source':str(TARGET/'real-agent-config.json'),'target':str(NATIVE),'read_only':True,'bind':{'create_host_path':False}})
    # Assert the only Compose change is the new read-only native config mount.
    check=copy.deepcopy(updated);check['services']['hermes-main']['volumes'].pop();assert check==original
    target_compose=STATE/'compose.aviation-v5.json';save(target_compose,updated)
    run(['docker','compose','-f',str(target_compose),'config','--quiet'])
    if not (STATE/'before.json').exists():
        (STATE/'qwen3-production-v4.json').write_bytes(QWEN.read_bytes())
        (STATE/'real-agent-production-v4.json').write_bytes(NATIVE.read_bytes())
        save(STATE/'before.json',{'container_id':info['Id'],'original_compose':str(compose),'original_compose_sha256':sha(compose),
            'qwen_sha256':sha(QWEN),'native_sha256':sha(NATIVE),'previous_health':health(18133),'candidate':ready})
    before=read(STATE/'before.json');assert before['container_id']==info['Id']
    with socket.socket() as probe:probe.bind(('127.0.0.1',18134))
    with (STATE/'candidate.log').open('a') as log:
        command=['/home/h3c/anaconda3/envs/scat-rag-train/bin/python','-B','scripts/serve_rag_production.py','--config','services/rag/qwen3-aviation-v5.json','--port','18134']
        child=subprocess.Popen(command,cwd=ROOT,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
    save(STATE/'candidate-process.json',{'pid':child.pid,'command':command})
    result=wait(18134,ready['corpus_sha256'],180)
    save(STATE/'candidate-ready.json',result);print(json.dumps(result),flush=True)

def stop_candidate():
    p=STATE/'candidate-process.json'
    if not p.exists():return
    pid=read(p)['pid'];cmd=Path(f'/proc/{pid}/cmdline')
    if cmd.exists():
        args=cmd.read_bytes().split(b'\0')
        if b'scripts/serve_rag_production.py' not in args or b'18134' not in args:raise ValueError('Candidate PID identity changed')
        os.kill(pid,signal.SIGTERM)

def rollback():
    before=read(STATE/'before.json');compose=Path(before['original_compose'])
    assert sha(compose)==before['original_compose_sha256']
    QWEN.write_bytes((STATE/'qwen3-production-v4.json').read_bytes())
    NATIVE.write_bytes((STATE/'real-agent-production-v4.json').read_bytes())
    run(['systemctl','--user','restart','scat-hermes-rag.service'])
    result=wait(18133,before['previous_health']['corpus_sha256'])
    run(['docker','compose','-f',str(compose),'up','-d','--no-deps','hermes-main'])
    wait(18121,seconds=120);stop_candidate()
    save(STATE/'rollback.json',{'status':'rolled_back','health':result,'external_model_calls':0})

def activate():
    before=read(STATE/'before.json');ready=read(TARGET/'ready.json')
    assert sha(QWEN)==before['qwen_sha256'] and sha(NATIVE)==before['native_sha256']
    assert inspect()['Id']==before['container_id'],'Current deployment changed since staging'
    smoke=read(ROOT/'var/validation/20261123-aviation-v5/retrieval-18134/smoke-summary.json')
    assert smoke['source_presence_passed']==smoke['total']==16,'Source retrieval smoke did not pass'
    regression=read(ROOT/'var/validation/20261123-aviation-v5/retrieval-18134/regression-summary.json')
    assert sum(g['execution_passed'] for g in regression['groups'].values())==113
    # Data changes do not authorize interrupting an active native knowledge run.
    import sqlite3
    for db_path in (ROOT/'var/deploy/h7b/rag-operations').glob('*/invocations.sqlite'):
        with closing(sqlite3.connect(f'file:{db_path}?mode=ro',uri=True)) as db:
            active=db.execute("select count(*) from rag_invocations where status in ('starting','running')").fetchone()[0]
            if active:raise RuntimeError('Active knowledge run; defer service switch')
    try:
        QWEN.write_bytes((TARGET/'retrieval-config.json').read_bytes());NATIVE.write_bytes((TARGET/'real-agent-config.json').read_bytes())
        run(['systemctl','--user','restart','scat-hermes-rag.service'])
        retrieval=wait(18133,ready['corpus_sha256'])
        run(['docker','compose','-f',str(STATE/'compose.aviation-v5.json'),'up','-d','--no-deps','hermes-main'])
        main_health=wait(18121,seconds=150)
        code="import json,sys;from pathlib import Path;sys.path.insert(0,'src');from hermes_runtime.real_rag import load_config,load_corpus;p=Path('services/rag/real-agent-production-v1.json');c=load_config(p);r=load_corpus(c);print(json.dumps({'generation_id':c['generation_id'],'chunks':len(r),'corpus_sha256':c['corpus_sha256']}))"
        native=json.loads(run(['docker','exec',CONTAINER,'/usr/local/bin/scat-python','-B','-c',code]))
        assert native['corpus_sha256']==ready['corpus_sha256'] and native['chunks']==ready['chunk_count']
        stop_candidate()
        result={'status':'activated','retrieval':retrieval,'main_health':main_health,'native_read_contract':native,
            'compose':str(STATE/'compose.aviation-v5.json'),'rollback_command':'python scripts/manage_aviation_corpus.py rollback',
            'external_model_calls':0,'qa_accuracy_not_remeasured':True}
        save(STATE/'activated.json',result);print(json.dumps(result),flush=True)
    except Exception:
        rollback();raise

def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=['prepare','activate','rollback']);a=p.parse_args()
    {'prepare':prepare,'activate':activate,'rollback':rollback}[a.action]()

if __name__=='__main__':main()
