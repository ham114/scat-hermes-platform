"""Package public metadata only; raw receipts and SQLite backups stay on server."""

import hashlib
import json
import zipfile
from pathlib import Path

if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    source = root / "var/validation/20261108-business-parity"
    target = root / ".tmp/business-parity/public-evidence.zip"
    files = [
        p
        for p in source.rglob("*")
        if p.is_file()
        and not any(part.startswith("private") for part in p.relative_to(source).parts)
        and p.suffix in {".json", ".log"}
    ]
    assert all(not p.is_symlink() for p in files)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.writestr(path.relative_to(source).as_posix(), path.read_bytes())
    print(
        json.dumps(
            {
                "files": len(files),
                "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                "raw_exported": False,
            }
        )
    )
