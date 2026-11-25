"""Finish this one-time local build after extraction, without changing live services."""
import json,subprocess,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
B=ROOT/'var/rag/sources/official-aviation-20261123'
deadline=time.monotonic()+1800
while len(list((B/'extracted').rglob('conversion.json')))<105:
    if time.monotonic()>deadline:raise TimeoutError('Extraction not complete; inspect extraction logs')
    time.sleep(10)
for environment,script,args in [
    ('docling','run_aviation_docling_batch.py',['--assemble-only']),
    ('docling','build_aviation_corpus.py',[]),
    ('docling','audit_aviation_corpus.py',[]),
    ('scat-rag-train','embed_aviation_corpus.py',[]),
]:
    print(json.dumps({'stage':'starting','script':script}),flush=True)
    result=subprocess.run([f'/home/h3c/anaconda3/envs/{environment}/bin/python','-B',str(ROOT/'scripts'/script),*args],cwd=ROOT,timeout=3600)
    if result.returncode:raise RuntimeError('Build stage failed: '+script)
    print(json.dumps({'stage':'passed','script':script}),flush=True)
print('Candidate build completed; production has not been switched.',flush=True)
