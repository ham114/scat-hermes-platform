"""Key-free, bounded checkpoint transfer for the completed local evidence test."""
import argparse,hashlib,json,tarfile,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/'var/validation/20261125-rag-holdout120-rerank-serial-v1'
BUNDLE=OUT.parent/'20261125-rag-holdout120-rerank-serial-sync.tgz'
def main():
 p=argparse.ArgumentParser();p.add_argument('action',choices=['pack','extract','install-report']);p.add_argument('--sha256');a=p.parse_args()
 if a.action=='install-report':
  report=ROOT/'output/rag-holdout120-rerank-20261125';archive=report.with_suffix('.zip')
  assert a.sha256 and hashlib.sha256(archive.read_bytes()).hexdigest()==a.sha256
  with zipfile.ZipFile(archive)as z:
   assert len(z.infolist())<1000 and sum(x.file_size for x in z.infolist())<100*1024*1024
   for item in z.infolist():
    target=(report.parent/item.filename).resolve()
    assert target.is_relative_to(report.resolve()) and not item.is_dir()
    target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(z.read(item))
  manifest=json.loads((report/'manifest.json').read_text())
  assert all(hashlib.sha256((report/n).read_bytes()).hexdigest()==h for n,h in manifest['files'].items())
  print(json.dumps({'report_installed':True,'manifest_files_verified':len(manifest['files']),'archive_sha256':a.sha256,'no_model_calls':True}));return
 if a.action=='extract':
  with tarfile.open(BUNDLE,'r:gz')as z:
   items=z.getmembers()
   for m in items:
    assert m.isfile() and not Path(m.name).is_absolute() and '..'not in Path(m.name).parts
    target=(OUT/m.name).resolve();assert target.is_relative_to(OUT.resolve());target.parent.mkdir(parents=True,exist_ok=True)
    data=z.extractfile(m).read();tmp=target.with_suffix(target.suffix+'.syncing');tmp.write_bytes(data);tmp.replace(target)
  print(json.dumps({'files_received':len(items)}));return
 import fcntl
 with (OUT/'run.lock').open('a')as lock:
  fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  summary=json.loads((OUT/'summary.json').read_text());assert summary['status']=='completed'
  files=[*OUT.glob('*.json'),*(OUT/'shared').glob('*.json')]
  # Include server source, since the local checkout has additional unpublished work.
  with tarfile.open(BUNDLE,'w:gz')as z:
   for f in files:z.add(f,arcname=f.relative_to(OUT).as_posix(),recursive=False)
   for f in sorted((ROOT/'src/rag_retrieval').glob('*.py')):z.add(f,arcname='source-snapshot/'+f.relative_to(ROOT).as_posix(),recursive=False)
  print(json.dumps({'files':len(files),'bytes':BUNDLE.stat().st_size,'sha256':hashlib.sha256(BUNDLE.read_bytes()).hexdigest(),'no_keys':True}))
if __name__=='__main__':main()
