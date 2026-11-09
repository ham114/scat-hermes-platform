"""Hash-checked source/record whitelist sync; raw business payloads stay server-private."""
import hashlib
import json
import os
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261108-release-readiness"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    os.umask(0o077)
    archive = ROOT / ".tmp/release-readiness-final-source.zip"
    expected = json.loads((ROOT / ".tmp/release-readiness-final-source-manifest.json").read_text())
    assert sha(archive) == expected["sha256"]
    assert json.loads((OUT / "final-closeout.json").read_text())["status"] == "closed"
    with zipfile.ZipFile(archive) as z:
        manifest = json.loads(z.read("MANIFEST.json"))
        assert set(z.namelist()) == {*manifest, "MANIFEST.json"} and len(manifest) == expected["files"]
        for relative, checksum in manifest.items():
            path = Path(relative)
            assert not path.is_absolute() and ".." not in path.parts and "private" not in path.parts
            assert path.parts[0] in {"src", "scripts", "docs", "tests", "var"} or relative in {"README.md", "SCAT_HERMES_PLATFORM_DEVELOPMENT_AND_TEST_PLAN.md"}
            assert path.parts[0] != "var" or path.parts[:3] == ("var", "validation", "20261108-release-readiness")
            destination = ROOT / path
            assert destination.resolve().is_relative_to(ROOT.resolve()) and not destination.is_symlink()
            body = z.read(relative)
            assert hashlib.sha256(body).hexdigest() == checksum
            if relative.startswith("src/") or relative in {"scripts/run_autonomy_proxy.py", "scripts/run_workflow_tool.py", "scripts/run_model_task_tool.py"}:
                assert destination.is_file() and sha(destination) == checksum, "live_core_source_mismatch:" + relative
        for relative, checksum in manifest.items():
            destination = ROOT / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            pending = destination.with_name(destination.name + ".sync-pending")
            pending.write_bytes(z.read(relative))
            pending.replace(destination)
            assert sha(destination) == checksum
    result = {"status": "verified", "files": manifest, "archive_sha256": expected["sha256"], "live_core_sources_unchanged_during_record_sync": True, "raw_business_payloads_in_archive": False}
    (OUT / "final-source-sync.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    names = [
        "final-closeout.json", "campaign-closeout.json", "campaign-b-authorization.json", "final-source-sync.json",
        "native-campaign.json", "native-campaign-b.json", "natural-business.json", "natural-cfpl-worst.json",
        "natural-experiment.json", "natural-materialization-report.json", "natural-materialization-report-r2.json",
        "memory-semantic.json", "rag-retrieval-new.json", "rag-answers-new.json", "health-30m.json",
        "health-postswitch-5m.json", "health-postswitch-report-recovery.json", "mixed-load.json",
        "cfpl-paging-indexes.json", "cfpl-paging-performance.json", "gpu-conda-artifacts.json", "gpu-conda-image.json",
        "gpu-conda-candidate.json", "gpu-conda-acceptance.json", "gpu-conda-production-plan.json", "gpu-conda-production-result.json",
        "gpu-conda-production-rollback.json", "training-partition-audit.json", "quality-release-review.json",
        "proxy-campaign-runtime.json", "saved-gbar-output.json", "saved-gbar-browser.json", "legacy-renderer-browser.json",
        "readonly-browser-https-browser.json", "identity-live-private.json", "backend-full-scope-final-r2.log",
        "control-scope-tests.log", "readonly-owner-tests-r2.log", "scope-fix/live-control-scope.json", "scope-fix/deployment-plan.json",
        "scope-fix/deployment-result.json", "scope-fix/cluster-smoke.json", "owner-fix/live-owner-admission.json",
    ]
    files = ["var/validation/20261108-release-readiness/" + n for n in names]
    files.append("services/hermes/gpu-models.v1.json")
    for filename in ["conda.lock.json", "wheels.lock.json", "artifacts.lock.json", "wheel-install-aliases.json"]:
        destination = OUT / ("gpu-conda-" + filename)
        assert not destination.exists()
        shutil.copyfile(ROOT / "var/deploy/gpu/conda-migration/context" / filename, destination)
        files.append(str(destination.relative_to(ROOT)))
    export = ROOT / ".tmp/release-readiness-final-evidence.zip"
    hashes = {p: sha(ROOT / p) for p in files}
    with zipfile.ZipFile(export, "x", zipfile.ZIP_DEFLATED) as z:
        for p in files:
            assert "private" not in Path(p).parts and (ROOT / p).stat().st_size < 2000000
            z.write(ROOT / p, p)
        z.writestr("MANIFEST.json", json.dumps(hashes, ensure_ascii=False, indent=2))
    metadata = {"files": hashes, "archive_sha256": sha(export), "raw_business_payloads_exported": False}
    (OUT / "final-evidence-export.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"source_files": len(manifest), "evidence_files": len(hashes), "evidence_archive_sha256": metadata["archive_sha256"], "raw_business_payloads_exported": False}))


if __name__ == "__main__":
    main()
