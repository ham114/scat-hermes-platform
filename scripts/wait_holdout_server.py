"""Bounded foreground network watch requested by the user; never submit a job."""
import base64, datetime, json, shlex, socket, subprocess, time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/'var/validation/20261125-ragas-holdout120'
REMOTE_ROOT='/home/h3c/project/python/scat-hermes-platform'
code='''import json,sqlite3
from pathlib import Path
p=Path("var/validation/20261125-ragas-holdout120")
with sqlite3.connect(p/"calls.sqlite",timeout=20)as d:
 d.execute("begin immediate")
 d.execute("insert or ignore into meta(key,value)values(?,?)",("exhausted","quality_review_pause"))
 print(json.dumps({"quality_pause":d.execute("select value from meta where key='exhausted'").fetchone()[0],"requests_charge_reserved":d.execute("select count(*),sum(charge),sum(reserved)from calls").fetchone(),"states":dict(d.execute("select state,count(*)from calls group by state"))}))
s=json.loads((p/"state.json").read_text())
print(json.dumps({"accepted":len(s["accepted"]),"status":s["status"]}))
'''
command='cd '+shlex.quote(REMOTE_ROOT)+' && /home/h3c/anaconda3/envs/ragas-eval/bin/python -c '+shlex.quote("exec(__import__('base64').b64decode('"+base64.b64encode(code.encode()).decode()+"'))")
end=time.monotonic()+600
while time.monotonic()<end:
 ping=subprocess.run(['ping','-n','1','-w','1500','172.26.67.148'],capture_output=True,timeout=5)
 row={'time':datetime.datetime.now().astimezone().isoformat(),'ping_returncode':ping.returncode}
 try:
  with socket.create_connection(('172.26.67.148',22),timeout=3):pass
  row['ssh_port_reachable']=True
 except OSError:row['ssh_port_reachable']=False
 with (OUT/'network-probes.jsonl').open('a',encoding='utf8')as f:f.write(json.dumps(row)+'\n')
 print(json.dumps(row),flush=True)
 if row['ssh_port_reachable']:
  r=subprocess.run(['ssh','-o','ConnectTimeout=8','-o','BatchMode=yes','h3c@172.26.67.148',command],capture_output=True,text=True,encoding='utf8',errors='replace',timeout=35)
  print(json.dumps({'ssh_returncode':r.returncode,'result':r.stdout,'error':r.stderr}),flush=True)
  if r.returncode==0:break
 time.sleep(25)
