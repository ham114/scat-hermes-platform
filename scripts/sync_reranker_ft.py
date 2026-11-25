"""Bounded transfer of completed offline adapters and evidence-only results."""
import argparse, hashlib, json, tarfile, zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'var/validation/20261125-reranker-lora-v1'
BUNDLE = OUT.parent / '20261125-reranker-lora-sync.tgz'
REPORT = ROOT / 'output/reranker-lora-20261125'
SCHEMES = ['p1_pointwise', 'p2_multi_positive', 'p3_pairwise']
sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()


def main():
    p = argparse.ArgumentParser()
    p.add_argument('action', choices=['pack', 'extract', 'install-report'])
    p.add_argument('--sha256')
    a = p.parse_args()
    if a.action == 'extract':
        assert a.sha256 and sha(BUNDLE) == a.sha256
        with tarfile.open(BUNDLE, 'r:gz') as z:
            items = z.getmembers()
            assert len(items) < 3000 and sum(m.size for m in items) < 250 * 1024**2
            for m in items:
                assert m.isfile() and not Path(m.name).is_absolute() and '..' not in Path(m.name).parts
                target = (OUT / m.name).resolve()
                assert target.is_relative_to(OUT.resolve())
                target.parent.mkdir(parents=True, exist_ok=True)
                data = z.extractfile(m).read()
                temp = target.with_suffix(target.suffix + '.syncing')
                temp.write_bytes(data)
                temp.replace(target)
        manifest = json.loads((OUT / 'sync-manifest.json').read_text(encoding='utf8'))
        assert all(sha(OUT / n) == h for n, h in manifest['files'].items())
        print(json.dumps({'received': len(items), 'all_hashes_verified': True}))
        return
    if a.action == 'install-report':
        archive = REPORT.with_suffix('.zip')
        assert a.sha256 and sha(archive) == a.sha256
        with zipfile.ZipFile(archive) as z:
            assert len(z.infolist()) < 3000 and sum(m.file_size for m in z.infolist()) < 250 * 1024**2
            for m in z.infolist():
                target = (REPORT.parent / m.filename).resolve()
                assert not m.is_dir() and target.is_relative_to(REPORT.resolve())
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(z.read(m))
        manifest = json.loads((REPORT / 'manifest.json').read_text(encoding='utf8'))
        assert all(sha(REPORT / n) == h for n, h in manifest['files'].items())
        print(json.dumps({'report_installed': True, 'verified_files': len(manifest['files']), 'sha256': a.sha256}))
        return
    import fcntl
    from reranker_ft_common import check_protected
    with (OUT / 'campaign.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        s = json.loads((OUT / 'campaign-summary.json').read_text())
        assert s['status'] == 'completed'
        check_protected()
        files = {f.relative_to(OUT).as_posix(): f for f in OUT.iterdir()
                 if f.is_file() and f.suffix in {'.json', '.jsonl', '.yaml', '.txt', '.log'}
                 and f.name != 'sync-manifest.json'}
        for name in [*SCHEMES, 'dev-base-scores', 'smoke-v3', 'ragfit-processed']:
            for f in (OUT / name).rglob('*'):
                if f.is_file() and f.suffix in {'.json', '.jsonl', '.safetensors', '.md'}:
                    files[f.relative_to(OUT).as_posix()] = f
        for name in ['pre-screen-v1', 'pre-screen-v2']:
            f = OUT / name / 'superseded.json'
            if f.exists(): files[f.relative_to(OUT).as_posix()] = f
        for pattern in ['scripts/*reranker_ft*.py', 'scripts/*ragfit_reranker*.py',
                        'scripts/rerank_postprocess_policies.py', 'scripts/evidence_retrieval_metrics.py',
                        'scripts/fusion_policy_experiments.py',
                        'services/rag/qwen3-production-v1.json', 'services/rag/qwen3-06b-download.json',
                        'services/rag/real-agent-production-v1.json',
                        'evaluations/rag_multihop120_candidates_20261124_v1/evaluation.json',
                        'evaluations/rag_aviation300_reviewed_20261124_v2/evaluation.json',
                        'tests/rag_retrieval/test_reranker_ft_contracts.py', 'src/rag_retrieval/*.py']:
            for f in ROOT.glob(pattern):
                files['source-snapshot/' + f.relative_to(ROOT).as_posix()] = f
        manifest = {'files': {n: sha(f) for n, f in sorted(files.items())}, 'no_base_weights': True}
        (OUT / 'sync-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        files['sync-manifest.json'] = OUT / 'sync-manifest.json'
        assert len(files) < 3000 and sum(f.stat().st_size for f in files.values()) < 250 * 1024**2
        with tarfile.open(BUNDLE, 'w:gz') as z:
            for n, f in sorted(files.items()): z.add(f, arcname=n, recursive=False)
        print(json.dumps({'files': len(files), 'bytes': BUNDLE.stat().st_size, 'sha256': sha(BUNDLE)}))


if __name__ == '__main__': main()
