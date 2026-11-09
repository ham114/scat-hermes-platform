"""Stage the identity portal beside the existing portal; public routing is unchanged."""

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "var/deploy/portal"
OUT = ROOT / "var/validation/20261108-release-readiness"


def main():
    assert os.getuid() == 1000
    assert not (OUT / "portal-plan.json").exists()
    with socket.socket() as s:
        assert s.connect_ex(("127.0.0.1", 18159)) != 0
    manifest = json.loads((ROOT / ".tmp/release-readiness/frontend-manifest.json").read_text())
    archive = ROOT / ".tmp/release-readiness/frontend-dist.zip"
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == manifest["zip_sha256"]
    release = "20261108-identity-portal-" + manifest["zip_sha256"][:12]
    folder = DEPLOY / "releases" / release
    folder.mkdir(exist_ok=True)
    with zipfile.ZipFile(archive) as z:
        assert set(z.namelist()) == set(manifest["files"])
        for name in z.namelist():
            path = Path(name)
            assert not path.is_absolute() and ".." not in path.parts
            data = z.read(name)
            assert hashlib.sha256(data).hexdigest() == manifest["files"][name]
            dest = folder / "dist" / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
    shutil.copyfile(
        ROOT / ".tmp/release-readiness/frontend-manifest.json", folder / "manifest.json"
    )
    original = (OUT / "nginx-site-snapshot.conf").read_text()
    (folder / "rollback-nginx.conf").write_text(original)
    site = original.replace("http://127.0.0.1:18158", "http://127.0.0.1:18159")
    assert site != original
    site = site.replace(
        "    location / {",
        "    if (-f /var/lib/scat-hermes-portal/maintenance) { return 503; }\n    location / {\n        include /etc/scat/hermes-portal-gateway.inc;",
    )
    assert "include /etc/scat/hermes-portal-gateway.inc;" in site
    # A prior explicit HYN-only Nginx guard must not survive the new registered directory.
    assert not re.search(r"if\s*\(\s*\$remote_user", site), "review_existing_user_guard"
    (folder / "nginx.conf").write_text(site)
    unit = """[Unit]
Description=SCAT identity portal and signed private API relay
After=network-online.target
[Service]
Type=simple
WorkingDirectory=ROOTPATH
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=/home/h3c/anaconda3/envs/scat-agent/bin/python -B scripts/serve_identity_portal.py --release RELEASEPATH
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
UMask=0077
MemoryMax=1G
TasksMax=128
[Install]
WantedBy=default.target
""".replace("ROOTPATH", str(ROOT)).replace("RELEASEPATH", str(folder.relative_to(ROOT)))
    unitpath = folder / "scat-hermes-portal-v2.service"
    unitpath.write_text(unit)
    plan = {
        "release": release,
        "zip_sha256": manifest["zip_sha256"],
        "old_site_sha256": hashlib.sha256(original.encode()).hexdigest(),
        "new_site_sha256": hashlib.sha256(site.encode()).hexdigest(),
        "old_main_site_sha256": json.loads((OUT / "nginx-site-snapshot.json").read_text())[
            "main_sha256"
        ],
        "principals": ["HYN", "ZYJ", "admin"],
        "public_switched": False,
    }
    (OUT / "portal-plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    for args in [
        ["systemctl", "--user", "link", str(unitpath)],
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "start", "scat-hermes-portal-v2.service"],
    ]:
        subprocess.run(args, check=True, capture_output=True, text=True, timeout=20)
    for attempt in range(40):
        try:
            with urllib.request.urlopen("http://127.0.0.1:18159/portal-health", timeout=1) as r:
                assert json.load(r)["status"] == "ready"
            break
        except OSError:
            if attempt == 39:
                raise
            time.sleep(0.25)
    print(
        json.dumps(
            {"status": "private_candidate_ready", "release": release, "public_switched": False}
        )
    )


if __name__ == "__main__":
    os.umask(0o077)
    main()
