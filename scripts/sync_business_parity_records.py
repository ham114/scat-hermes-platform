"""Apply the closed source/evidence bundle with path and SHA guards, then clear task temp."""

import hashlib
import json
import shutil
import sys
import zipfile
from pathlib import Path

if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    temporary = root / ".tmp/business-parity"
    output = root / "var/validation/20261108-business-parity"
    archive_path = temporary / "source-evidence.zip"
    assert (
        len(sys.argv) == 2 and hashlib.sha256(archive_path.read_bytes()).hexdigest() == sys.argv[1]
    )
    with zipfile.ZipFile(archive_path) as archive:
        manifest_name = "var/validation/20261108-business-parity/sync-manifest.json"
        manifest = json.loads(archive.read(manifest_name))
        assert set(archive.namelist()) == set(manifest["files"]) | {manifest_name}
        for name, expected in manifest["files"].items():
            path = Path(name)
            assert not path.is_absolute() and ".." not in path.parts
            target = (root / path).resolve()
            assert target.is_relative_to(root.resolve()) and not (root / path).is_symlink()
            assert path.parts[0] in {
                "src",
                "tests",
                "frontend",
                "docs",
                "scripts",
                "var",
            } or name in {"README.md", "SCAT_HERMES_PLATFORM_DEVELOPMENT_AND_TEST_PLAN.md"}
            content = archive.read(name)
            assert hashlib.sha256(content).hexdigest() == expected
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        (root / manifest_name).write_bytes(archive.read(manifest_name))
    assert all(
        hashlib.sha256((root / name).read_bytes()).hexdigest() == sha
        for name, sha in manifest["files"].items()
    )
    assert (
        temporary.resolve() == root.resolve() / ".tmp/business-parity"
        and not temporary.is_symlink()
    )
    shutil.rmtree(temporary)
    report = {
        "status": "passed",
        "files_verified": len(manifest["files"]),
        "server_temp_removed": not temporary.exists(),
        "known_private_values_found": manifest["known_private_values_found"],
        "frontend_zip_scan": manifest["frontend_zip_scan"],
    }
    (output / "sync-result.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))
