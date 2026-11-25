"""Small, key-free checkpoint bundles for an intermittently connected server."""
import argparse,hashlib,io,json,sqlite3,tarfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/'var/validation/20261125-ragas-holdout120'
BUNDLE=OUT.parent/'20261125-ragas-holdout120-sync.tgz'
def main():
 p=argparse.ArgumentParser();p.add_argument('action',choices=['pack','extract']);a=p.parse_args()
 if a.action=='extract':
  with tarfile.open(BUNDLE,'r:gz')as archive:
   members=archive.getmembers()
   assert all(m.isfile()and not Path(m.name).is_absolute()and '..'not in Path(m.name).parts for m in members)
   for m in members:
    target=(OUT/m.name).resolve();assert target.is_relative_to(OUT.resolve());target.parent.mkdir(parents=True,exist_ok=True)
    data=archive.extractfile(m).read();tmp=target.with_suffix(target.suffix+'.syncing');tmp.write_bytes(data);tmp.replace(target)
  print(json.dumps({'checkpoint_files_received':len(members)}));return
 import fcntl
 with (OUT/'campaign.lock').open('a')as lock:
  fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  with sqlite3.connect(OUT/'calls.sqlite')as db:assert not db.execute("select count(*)from calls where state='admitted'").fetchone()[0]
  state=json.loads((OUT/'state.json').read_text());jobs={c['generation']['job']for c in state['accepted']}
  # Include saved attempts for every slot so false judge rejections can be
  # inspected offline, without restarting generation or making model calls.
  for key in state['attempts']:
   n=state['attempts'].get(key,0)
   for attempt in range(max(1,n-2),n+1):jobs.add(f'slot-{int(key):03d}-attempt-{attempt:02d}')
  files=[p for p in OUT.glob('*.json')if p.name!='old-question-fingerprints.json']
  files+=[OUT/'candidates.jsonl']
  files+=[p for job in jobs for p in (OUT/'jobs').glob(job+'.*.json')]
  files=list(dict.fromkeys(p for p in files if p.exists()))
  with tarfile.open(BUNDLE,'w:gz')as archive:
   for path in files:archive.add(path,arcname=path.relative_to(OUT).as_posix(),recursive=False)
  print(json.dumps({'files':len(files),'bytes':BUNDLE.stat().st_size,'sha256':hashlib.sha256(BUNDLE.read_bytes()).hexdigest(),'no_keys':True}))
if __name__=='__main__':main()
