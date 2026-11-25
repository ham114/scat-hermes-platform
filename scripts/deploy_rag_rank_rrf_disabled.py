"""Deploy the verified ranking combination while keeping both RAG entrypoints off."""

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import subprocess
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / 'var/rag/deployments/weighted2-rank-rrf-20261126'
OUT = ROOT / 'output/rag-production-disabled-20261126'
MAIN = 'scat-hermes-h7b-hermes-main-1'
UNITS = ['scat-hermes-api.service', 'scat-hermes-rag.service']
FILES = [
    'src/rag_retrieval/candidates.py', 'src/rag_retrieval/ranking.py',
    'src/rag_retrieval/production.py', 'src/rag_retrieval/runtime.py',
    'src/rag_retrieval/rerank_comparison.py',
    'src/hermes_runtime/entrypoint.py', 'src/hermes_runtime/real_rag.py',
    'scripts/run_hermes_api.py', 'scripts/run_container_service.py', 'scripts/serve_rag_production.py',
    'scripts/rag_service_enabled.py', 'scripts/manage_rag_production.py',
    'scripts/generate_compose_h7b.py', 'infra/compose/compose.yaml',
    'infra/systemd/user/scat-hermes-rag.service', 'infra/systemd/user/scat-hermes-api.service',
    'services/rag/qwen3-production-v1.json', 'services/rag/real-agent-production-v1.json',
]


def read(p):
    return json.loads(Path(p).read_text(encoding='utf8'))


def save(p, value):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf8')


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def run(args, timeout=180):
    p = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    if p.returncode:
        raise RuntimeError(str(args[:3]) + ': ' + p.stderr[-1200:] + p.stdout[-800:])
    return p.stdout


def inspect():
    return json.loads(run(['docker', 'inspect', MAIN]))[0]


def no_active_rag():
    for p in (ROOT/'var/deploy/h7b/rag-operations').glob('*/invocations.sqlite'):
        with sqlite3.connect(f'file:{p}?mode=ro', uri=True) as db:
            assert db.execute("select count(*) from rag_invocations where status in ('starting','running')").fetchone()[0] == 0, 'active_RAG_run'


def prepare():
    assert not (STATE/'before.json').exists(), 'deployment_already_prepared'
    no_active_rag()
    info = inspect()
    compose = Path(info['Config']['Labels']['com.docker.compose.project.config_files'])
    assert compose.is_relative_to(ROOT) and compose.is_file()
    original = read(compose)
    backups = {}
    for name in FILES + ['.env']:
        p = ROOT/name
        backups[name] = sha(p) if p.exists() else None
        if p.exists():
            target = STATE/'before'/name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(p.read_bytes())
            if name == '.env':target.chmod(0o600)
    save(STATE/'compose-before.json', original)
    save(STATE/'before.json', {'files':backups, 'compose':str(compose),
         'compose_sha256':sha(compose), 'main_id':info['Id'], 'prepared_at':time.time()})
    print(json.dumps({'status':'prepared', 'backed_up_files':len(backups), 'production_changed':False}))


def smoke():
    """Fresh local model scoring on six preselected development/regression questions."""
    import sys
    sys.path.insert(0, str(ROOT/'src'))
    from rag_retrieval.production import load_config, Retriever
    from rag_retrieval.ranking import rank_candidates, RRF_RANKING
    os.environ.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', TOKENIZERS_PARALLELISM='false')
    cfg = load_config(ROOT, 'services/rag/qwen3-production-v1.json')
    assert cfg['enabled'] is False
    native = read(ROOT/'services/rag/real-agent-production-v1.json')
    assert native['enabled'] is False
    source = ROOT/'output/rag-rerank-postprocess-20261125/rank_rrf-results.json'
    rows = read(source)['cases']
    controls = {r['id']:r for r in read(ROOT/'output/rag-fusion-serial-20261125/weighted2-results.json')['cases']}
    for row in rows:
        original = controls[row['id']]
        pool = original['pool']
        assert rank_candidates(pool, [original['scores'][c] for c in pool], RRF_RANKING) == row['ranking']
    engine = Retriever(ROOT, cfg)
    # Fixed positional sample plus the historical old Top5 regression sentinel.
    selected = [r for r in rows if r['cohort']=='new120'][:5]
    selected += [next(r for r in rows if r['id']=='P0-20261123-110')]
    checked = []
    for row in selected:
        started = time.monotonic()
        actual = engine.search(row['question'], 10)
        ids = [r['chunk_id'] for r in actual]
        assert ids == row['ranking'][:10], 'fresh_production_parity_failed:' + row['id']
        checked.append({'id':row['id'], 'top10_parity':True, 'seconds':time.monotonic()-started})
    save(OUT/'fresh-smoke.json', {'status':'passed', 'cases':checked, 'identity':engine.identity,
         'source_sha256':sha(source), 'saved_full_ranking_parity_cases':len(rows),
         'local_model_pairs':600, 'generation_calls':0,
         'external_model_calls':0, 'production_listener_started':False})
    print(json.dumps({'status':'passed', 'fresh_top10_parity_cases':len(checked)}))


def health(port, path='/health'):
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f'http://127.0.0.1:{port}{path}', timeout=4) as response:
            return json.load(response)
    except (OSError, ValueError):return None


def port_open(port):
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(('127.0.0.1', port)) == 0


def status():
    info = inspect()
    units = {n:subprocess.run(['systemctl','--user','show',n,'-p','ActiveState','-p','UnitFileState'],capture_output=True,text=True).stdout.splitlines() for n in UNITS}
    containers = {}
    for name in ['actions','hermes-worker','hermes-main','platform','worker']:
        x = json.loads(run(['docker','inspect','scat-hermes-h7b-'+name+'-1']))[0]
        containers[name] = {'running':x['State']['Running'], 'health':x['State'].get('Health',{}).get('Status'),
            'rag_enabled':[s for s in x['Config']['Env'] if s.startswith('SCAT_RAG_BASELINE_ENABLED=')]}
    return {'units':units, 'rag_ports_closed':not any(port_open(p) for p in [18133,18134]),
        'rag_configs_disabled':all(read(ROOT/n)['enabled'] is False for n in FILES[-2:]),
        'main_health':health(18121), 'platform_health':health(18120,'/health/ready'),
        'containers':containers, 'compose':info['Config']['Labels']['com.docker.compose.project.config_files']}


def activate():
    assert read(OUT/'fresh-smoke.json')['status']=='passed'
    assert read(OUT/'validation.json')['status']=='passed'
    before = read(STATE/'before.json')
    info = inspect()
    if info['Id'] != before['main_id']:
        assert info['Config']['Labels']['com.docker.compose.project.config_files'] == str(STATE/'compose.disabled.json'), 'unowned_main_container_change'
        assert info['Config']['Image'] == read(STATE/'compose-before.json')['services']['hermes-main']['image'], 'main_image_changed'
    no_active_rag()
    candidate = copy.deepcopy(read(STATE/'compose-before.json'))
    for value in candidate['services'].values():
        value.setdefault('environment',{})['SCAT_RAG_BASELINE_ENABLED']='false'
    main = candidate['services']['hermes-main']
    mounts = main['volumes']
    # Use the image's own API implementation; the entrypoint is compatible with
    # this existing image and omits the RAG opt-in before executing that API.
    patches = ['services/rag/real-agent-production-v1.json', 'scripts/run_container_service.py']
    for name in patches:
        target = str(ROOT/name)
        mounts[:] = [v for v in mounts if v.get('target')!=target]
        mounts.append({'type':'bind','source':target,'target':target,'read_only':True,
                       'bind':{'create_host_path':False}})
    candidate.setdefault('x-external-gpu-services',{}).setdefault('rag',{})['enabled']=False
    compose = STATE/'compose.disabled.json'
    save(compose, candidate)
    run(['docker','compose','-p','scat-hermes-h7b','-f',str(compose),'config','--quiet'])
    # Only project-owned units and links can be stopped or disabled.
    for name in UNITS:
        unit = Path('/home/h3c/.config/systemd/user')/name
        assert unit.is_symlink() and unit.resolve()==ROOT/'infra/systemd/user'/name
    run(['systemctl','--user','stop',*UNITS])
    for name in UNITS:
        link = Path('/home/h3c/.config/systemd/user/default.target.wants')/name
        if link.exists() or link.is_symlink():
            assert link.is_symlink() and link.resolve()==ROOT/'infra/systemd/user'/name
            link.unlink()
    run(['systemctl','--user','daemon-reload'])
    env = ROOT/'.env'
    lines = [s for s in env.read_text().splitlines() if not re.match(r'^(?:export\s+)?SCAT_RAG_BASELINE_ENABLED\s*=',s)]
    env.write_text('\n'.join(lines+['SCAT_RAG_BASELINE_ENABLED=false'])+'\n')
    env.chmod(0o600)
    for name in ['actions','hermes-worker','hermes-main','platform','worker']:
        run(['docker','compose','-p','scat-hermes-h7b','-f',str(compose),'up','-d','--no-deps',name])
        until = time.monotonic()+120
        while time.monotonic()<until:
            x=json.loads(run(['docker','inspect','scat-hermes-h7b-'+name+'-1']))[0]
            if x['State'].get('Health',{}).get('Status')=='healthy':break
            time.sleep(2)
        else:raise RuntimeError('container_not_healthy:'+name)
    current = status()
    assert current['rag_ports_closed'] and current['rag_configs_disabled']
    assert current['main_health'] and current['platform_health']
    assert all('ActiveState=inactive' in v and 'UnitFileState=enabled' not in v for v in current['units'].values())
    assert all(v['health']=='healthy' and v['rag_enabled']==['SCAT_RAG_BASELINE_ENABLED=false'] for v in current['containers'].values())
    save(OUT/'activation.json', {'status':'deployed_disabled', 'observed':current,
         'deployed_sha256':{n:sha(ROOT/n) for n in FILES}, 'external_model_calls':0,
         'corpus_and_vectors_changed':False, 'completed_at':time.time()})
    print(json.dumps({'status':'deployed_disabled','rag_ports_closed':True,'main_healthy':True}))


if __name__ == '__main__':
    os.umask(0o077)
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['prepare','smoke','activate','status'])
    a=p.parse_args()
    if a.action=='status':print(json.dumps(status()))
    else:globals()[a.action]()
