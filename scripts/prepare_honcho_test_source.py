"""Fetch a fixed Honcho source archive into the project; no host installation."""

import hashlib
import json
import os
import shutil
import sys
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMMIT = "e2d2aba182862deeb846e5cd4a30a41bce0ed82a"


def main():
    if sys.executable != "/home/h3c/anaconda3/envs/scat-agent/bin/python":
        raise ValueError("existing_APP_Conda_required")
    os.umask(0o077)
    target = ROOT / "var/deploy/honcho"
    temp = ROOT / ".tmp/honcho-test"
    temp.mkdir(parents=True, exist_ok=True)
    archive = target / ("honcho-" + COMMIT + ".tar.gz")
    if not archive.exists():
        staging = temp / "honcho-source.part"
        with (
            urllib.request.urlopen(
                "https://codeload.github.com/plastic-labs/honcho/tar.gz/" + COMMIT, timeout=90
            ) as response,
            staging.open("wb") as stream,
        ):
            shutil.copyfileobj(response, stream)
        staging.replace(archive)
    source = target / "source"
    source.mkdir(exist_ok=True)
    with tarfile.open(archive) as stream:
        for item in stream.getmembers():
            parts = Path(item.name).parts
            if len(parts) < 2:
                continue
            relative = Path(*parts[1:])
            if relative.is_absolute() or ".." in relative.parts or item.issym() or item.islnk():
                continue
            item.name = relative.as_posix()
            stream.extract(item, source, filter="data")
    manifest = {
        "commit": COMMIT,
        "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "archive_bytes": archive.stat().st_size,
        "host_installs": 0,
    }
    (target / "source.lock.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest))
    print("SOURCE_FILES", json.dumps([str(p.relative_to(source)) for p in source.iterdir()]))


if __name__ == "__main__":
    main()
