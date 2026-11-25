"""Read-only deployment close-out checks; no external model requests."""
from contextlib import closing
from datetime import datetime
import hashlib,json,socket,sqlite3,subprocess
from pathlib import Path
from urllib.request import build_opener,ProxyHandler
from zoneinfo import ZoneInfo
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
T=ROOT/'var/rag/corpora/kg_20261123_aviation_v5'
S=ROOT/'var/rag/deployments/aviation-v5'
def read(p):return json.loads(p.read_text())
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()

ready=read(T/'ready.json');activation=read(S/'activated.json');before=read(S/'before.json')
assert activation['status']=='activated'
with build_opener(ProxyHandler({})).open('http://127.0.0.1:18133/health/ready',timeout=10) as response:health=json.load(response)
assert health['corpus_sha256']==ready['corpus_sha256']==sha(T/'rag_chunks.v1.jsonl') and health['chunk_count']==8173
assert sha(ROOT/'services/rag/qwen3-production-v1.json')==sha(T/'retrieval-config.json')
assert sha(ROOT/'services/rag/real-agent-production-v1.json')==sha(T/'real-agent-config.json')
assert sha(ROOT/'evaluations/rag_p0_reviewed_20261123_v1/manifest.json')=='188d26b707418b78b8c4afe01f2d5e9fb2333f5e7e68143d569927b645a74732'
old=ROOT/'var/rag/corpora/kg_20261026_research_v4'
assert sha(old/'rag_chunks.v1.jsonl')=='208c2322f951ac53028dcb999b75222d4ed4cac945d4122c76dd9544f9d38c96'
assert np.array_equal(np.load(T/'bge-m3.npy',mmap_mode='r')[:149],np.load(old/'bge-m3.npy',mmap_mode='r'))
names=subprocess.check_output(['docker','ps','--filter','name=scat-hermes-h7b-','--format','{{.Names}}'],text=True).splitlines()
assert len(names)==7,names
infos=json.loads(subprocess.check_output(['docker','inspect',*names],text=True))
containers={x['Name'].lstrip('/'):{'status':x['State']['Status'],'health':x['State'].get('Health',{}).get('Status')} for x in infos}
assert all(x['status']=='running' and x['health']=='healthy' for x in containers.values()),containers
main=next(x for x in infos if x['Name'].endswith('hermes-main-1'))
mount=next(x for x in main['Mounts'] if x['Destination']==str(ROOT/'services/rag/real-agent-production-v1.json'))
assert mount['Source']==str(T/'real-agent-config.json') and not mount['RW']
assert main['Config']['Labels']['com.docker.compose.project.config_files']==str(S/'compose.aviation-v5.json')
with socket.socket() as connection:assert connection.connect_ex(('127.0.0.1',18134))!=0,'Candidate service not stopped'
with closing(sqlite3.connect(f'file:{ROOT}/var/validation/20261123-rag-p0/qa-ledger.sqlite?mode=ro',uri=True)) as db:
    previous_calls=db.execute('select count(*) from calls').fetchone()[0]
assert previous_calls==702
result={'status':'passed','completed_at':datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),'health':health,
    'containers':containers,'main_config_mount_read_only':True,'candidate_stopped':True,
    'old_corpus_and_frozen_dataset_unchanged':True,'old_149_vectors_unchanged':True,
    'previous_qa_ledger_calls_unchanged':previous_calls,'external_llm_calls_in_this_task':0,
    'qa_accuracy_not_remeasured':True,'rollback_compose':before['original_compose'],
    'scripts_sha256':{p.name:sha(p) for p in sorted((ROOT/'scripts').glob('*aviation*')) if p.is_file()}}
(S/'closeout-audit.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(result,ensure_ascii=False))
