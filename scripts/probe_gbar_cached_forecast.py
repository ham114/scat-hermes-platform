"""Check the final production postprocessor against the original CUDA response, without POST."""

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261108-gbar-live"
CODE = """
import json,os,sqlite3
from pathlib import Path
from application.task_store import digest
from workflow_runtime.gpu import run_gpu_trial,GPURegistration
from workflow_runtime.data import load_input
r=Path.cwd(); task='8121b9d0764e4ff584d4146fe2b57a39'
def connect(path):
    c=sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True);c.row_factory=sqlite3.Row;return c
with connect(r/os.environ['HERMES_TASK_STATE_FILE']) as db:
    row=dict(db.execute('SELECT * FROM model_tasks WHERE task_id=?',(task,)).fetchone());row['snapshot']=json.loads(row['snapshot'])
    backend=dict(db.execute('SELECT * FROM skill_backend_executions WHERE task_id=?',(task,)).fetchone())
assert backend['state']=='succeeded' and backend['calls']==1
plan=row['snapshot']['workflow']; frozen=load_input(r,plan['input_sha256'],scope=row['scope'])
identity=digest({'execution':backend['id'],'chunk':0})
with connect(r/'var/gpu/requests.sqlite') as db:
    original=dict(db.execute('SELECT * FROM gpu_requests WHERE id=?',(identity,)).fetchone())
    before=db.execute('SELECT count(*),sum(calls) FROM gpu_requests').fetchone()
assert original['task']==task and original['execution']==backend['id'] and original['state']=='succeeded'
raw=json.loads(original['output']);assert digest(raw)==original['output_sha']
assert all(v is None for s in raw['payload']['series'] for x in s['expected_values'] for v in x)
factory=GPURegistration.backend
def cached_backend(self):
    b=factory(self)
    def forbidden(*args,**kwargs):raise AssertionError('GPU POST forbidden in cached readback')
    b.execute=forbidden
    return b
GPURegistration.backend=cached_backend
output=run_gpu_trial(r,row,backend['id'],plan['trials'][0],frozen)
assert digest(output)==backend['output_sha'] and output['prediction_count']==3 and not output['samples']
with connect(r/'var/gpu/requests.sqlite') as db:
    assert tuple(db.execute('SELECT count(*),sum(calls) FROM gpu_requests').fetchone())==tuple(before)
    assert db.execute('SELECT output_sha FROM gpu_requests WHERE id=?',(identity,)).fetchone()[0]==original['output_sha']
print(json.dumps({'status':'passed','final_image_postprocessor_used':True,'original_cuda_response_reused':True,'original_output_sha256':original['output_sha'],'gpu_posts_added':0,'prediction_count':3,'error_samples':0,'backend_output_sha256':backend['output_sha']}))
"""


def main():
    result = subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            "scat-hermes-h7b-actions-1",
            "/opt/conda/envs/scat-agent/bin/python",
            "-B",
            "-",
        ],
        input=CODE,
        text=True,
        capture_output=True,
        timeout=50,
        check=True,
    )
    report = json.loads(result.stdout)
    (OUT / "cached-forecast-final-image.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
