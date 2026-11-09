"""Stage the independent HTTPS portal and atomically add its Nginx site."""

import argparse
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
OUT = ROOT / "var/validation/20261108-platform-closeout"
SITE = Path("/etc/nginx/conf.d/scat-hermes-portal.conf")


def run(args, timeout=40):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT, timeout=timeout)


def stage():
    assert os.getuid() == 1000
    for port in [8444, 18158]:
        with socket.socket() as s:
            assert s.connect_ex(("127.0.0.1", port)) != 0, "portal_port_in_use"
    manifest = json.loads((DEPLOY / "staging/manifest.json").read_text())
    archive = DEPLOY / "staging/frontend-dist.zip"
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == manifest["zip_sha256"]
    release = "20261108-portal-" + manifest["zip_sha256"][:12]
    folder = DEPLOY / "releases" / release
    folder.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(archive) as z:
        assert set(z.namelist()) == set(manifest["files"])
        for name in z.namelist():
            rel = Path(name)
            assert not rel.is_absolute() and ".." not in rel.parts
            content = z.read(name)
            assert hashlib.sha256(content).hexdigest() == manifest["files"][name]
            p = folder / "dist" / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(content)
    shutil.copyfile(DEPLOY / "staging/manifest.json", folder / "manifest.json")
    original = Path("/etc/nginx/conf.d/scat.conf").read_text()

    def setting(key):
        values = re.findall(r"^\s*" + key + r"\s+([^;]+);", original, re.MULTILINE)
        assert len(values) == 1 and not any(x in values[0] for x in ["\n", "\r", "{"])
        return values[0]

    site = (
        "server {\n    listen 172.26.67.148:8444 ssl;\n    server_name 172.26.67.148;\n"
        + "    ssl_certificate "
        + setting("ssl_certificate")
        + ";\n    ssl_certificate_key "
        + setting("ssl_certificate_key")
        + ";\n"
        + "    auth_basic "
        + setting("auth_basic")
        + ";\n    auth_basic_user_file "
        + setting("auth_basic_user_file")
        + ";\n"
        + "    access_log off;\n    client_max_body_size 1m;\n    location / {\n        proxy_pass http://127.0.0.1:18158;\n"
        + '        proxy_http_version 1.1;\n        proxy_set_header Host $host;\n        proxy_set_header Authorization "";\n'
        + '        proxy_set_header X-Authenticated-User "";\n        proxy_set_header Connection "";\n'
        + "        proxy_set_header X-Portal-Authenticated-User $remote_user;\n"
        + "        proxy_buffering off;\n        proxy_cache off;\n        proxy_read_timeout 650s;\n        proxy_send_timeout 30s;\n    }\n}\n"
    )
    (folder / "nginx.conf").write_text(site)
    relative = str(folder.relative_to(ROOT))
    unit = (
        "[Unit]\nDescription=SCAT production portal assets and private API relay\nAfter=network-online.target\n"
        "[Service]\nType=simple\nWorkingDirectory="
        + str(ROOT)
        + "\nEnvironment=PYTHONDONTWRITEBYTECODE=1\n"
        "ExecStart=/home/h3c/anaconda3/envs/scat-agent/bin/python -B scripts/serve_portal.py --release "
        + relative
        + " --user HYN\n"
        "Restart=on-failure\nRestartSec=5\nTimeoutStopSec=30\nUMask=0077\nMemoryMax=1G\nTasksMax=128\n"
        "[Install]\nWantedBy=default.target\n"
    )
    (folder / "scat-hermes-portal.service").write_text(unit)
    plan = {
        "release": release,
        "files": len(manifest["files"]),
        "zip_sha256": manifest["zip_sha256"],
        "old_site_sha256": hashlib.sha256(original.encode()).hexdigest(),
        "candidate_site_sha256": hashlib.sha256(site.encode()).hexdigest(),
        "url": "https://172.26.67.148:8444/",
        "auth": "existing_basic_auth_file",
        "old_site_preserved": True,
        "applied": False,
    }
    (OUT / "portal-plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    run(["systemctl", "--user", "link", str(folder / "scat-hermes-portal.service")])
    run(["systemctl", "--user", "daemon-reload"])
    run(["systemctl", "--user", "start", "scat-hermes-portal.service"])
    for i in range(40):
        try:
            with urllib.request.urlopen("http://127.0.0.1:18158/portal-health", timeout=1) as r:
                assert json.load(r)["status"] == "ready"
            break
        except OSError:
            if i == 39:
                raise
            time.sleep(0.25)
    print(
        json.dumps(
            {"staged": True, "release": release, "loopback_ready": True, "https_applied": False}
        )
    )


def nginx_apply():
    assert os.geteuid() == 0, "nginx_apply_requires_existing_sudo"
    plan = json.loads((OUT / "portal-plan.json").read_text())
    folder = DEPLOY / "releases" / plan["release"]
    old = Path("/etc/nginx/conf.d/scat.conf")
    assert hashlib.sha256(old.read_bytes()).hexdigest() == plan["old_site_sha256"]
    assert not SITE.exists() and not SITE.is_symlink()
    payload = (folder / "nginx.conf").read_bytes()
    assert hashlib.sha256(payload).hexdigest() == plan["candidate_site_sha256"]
    pid = Path("/run/nginx.pid").read_text().strip()
    fd = os.open(SITE, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    with os.fdopen(fd, "wb") as f:
        f.write(payload)
    try:
        run(["/usr/sbin/nginx", "-t"])
        run(["/usr/sbin/nginx", "-s", "reload"])
    except Exception:
        SITE.unlink()
        run(["/usr/sbin/nginx", "-t"])
        run(["/usr/sbin/nginx", "-s", "reload"])
        raise
    assert hashlib.sha256(old.read_bytes()).hexdigest() == plan["old_site_sha256"]
    assert Path("/run/nginx.pid").read_text().strip() == pid
    plan.update({"applied": True, "nginx_master_pid_unchanged": True, "server_rebooted": False})
    result = OUT / "portal-result.json"
    result.write_text(json.dumps(plan, indent=2) + "\n")
    result.chmod(0o644)
    os.chown(result, 1000, 1000)
    print(
        json.dumps(
            {
                "applied": True,
                "url": plan["url"],
                "old_site_preserved": True,
                "nginx_reloaded_without_restart": True,
            }
        )
    )


def upgrade():
    assert os.getuid() == 1000
    current = json.loads((OUT / "portal-result.json").read_text())
    assert current["applied"] and current.get("allowed_user") == "HYN"
    manifest = json.loads((DEPLOY / "staging/manifest.json").read_text())
    archive = DEPLOY / "staging/frontend-dist.zip"
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == manifest["zip_sha256"]
    release = "20261108-portal-" + manifest["zip_sha256"][:12]
    assert release != current["release"]
    folder = DEPLOY / "releases" / release
    if folder.exists():
        assert (
            json.loads((folder / "manifest.json").read_text())["zip_sha256"]
            == manifest["zip_sha256"]
        )
        assert all(
            hashlib.sha256((folder / "dist" / p).read_bytes()).hexdigest() == sha
            for p, sha in manifest["files"].items()
        )
    else:
        folder.mkdir()
    with zipfile.ZipFile(archive) as z:
        assert set(z.namelist()) == set(manifest["files"])
        for name in z.namelist():
            rel = Path(name)
            assert not rel.is_absolute() and ".." not in rel.parts
            content = z.read(name)
            assert hashlib.sha256(content).hexdigest() == manifest["files"][name]
            path = folder / "dist" / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
    shutil.copyfile(archive, folder / "frontend-dist.zip")
    shutil.copyfile(DEPLOY / "staging/manifest.json", folder / "manifest.json")
    previous = DEPLOY / "releases" / current["release"]
    old_unit = previous / "scat-hermes-portal.service"
    unit_text = old_unit.read_text()
    assert "--user HYN" in unit_text
    (folder / "scat-hermes-portal.service").write_text(
        unit_text.replace(str(previous.relative_to(ROOT)), str(folder.relative_to(ROOT)))
    )
    shutil.copyfile(previous / "nginx.conf", folder / "nginx.conf")
    (OUT / "portal-upgrade-plan.json").write_text(
        json.dumps(
            {"previous": current["release"], "target": release, "nginx_change": False}, indent=2
        )
        + "\n"
    )

    def activate(unit):
        run(["systemctl", "--user", "stop", "scat-hermes-portal.service"])
        disabled = subprocess.run(
            ["systemctl", "--user", "disable", "scat-hermes-portal.service"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        if disabled.returncode:
            config = Path.home() / ".config/systemd/user"
            assert not any(
                p.exists() or p.is_symlink()
                for p in [
                    config / "scat-hermes-portal.service",
                    config / "default.target.wants/scat-hermes-portal.service",
                ]
            ), "owned_unit_link_not_removed"
        run(["systemctl", "--user", "link", "--force", str(unit)])
        run(["systemctl", "--user", "daemon-reload"])
        run(["systemctl", "--user", "enable", "--now", "scat-hermes-portal.service"])

    try:
        activate(folder / "scat-hermes-portal.service")
        for index in range(40):
            try:
                with urllib.request.urlopen(
                    "http://127.0.0.1:18158/portal-health", timeout=1
                ) as response:
                    assert json.load(response)["status"] == "ready"
                break
            except OSError:
                if index == 39:
                    raise
                time.sleep(0.25)
    except Exception:
        activate(old_unit)
        raise
    shutil.copyfile(OUT / "portal-result.json", OUT / "portal-result-before-upgrade.json")
    current.update(
        {
            "previous_frontend_release": current["release"],
            "release": release,
            "zip_sha256": manifest["zip_sha256"],
            "files": len(manifest["files"]),
            "frontend_upgrade_nginx_changed": False,
        }
    )
    (OUT / "portal-result.json").write_text(json.dumps(current, indent=2) + "\n")
    (DEPLOY / "active-release").write_text(release + "\n")
    print(json.dumps({"upgraded": True, "release": release, "nginx_changed": False}))


def nginx_rollback():
    assert os.geteuid() == 0
    plan = json.loads((OUT / "portal-result.json").read_text())
    assert plan["applied"] and not SITE.is_symlink()
    payload = SITE.read_bytes()
    assert hashlib.sha256(payload).hexdigest() == plan["candidate_site_sha256"]
    assert (
        hashlib.sha256(Path("/etc/nginx/conf.d/scat.conf").read_bytes()).hexdigest()
        == plan["old_site_sha256"]
    )
    SITE.unlink()
    try:
        run(["/usr/sbin/nginx", "-t"])
        run(["/usr/sbin/nginx", "-s", "reload"])
    except Exception:
        SITE.write_bytes(payload)
        raise
    plan["applied"] = False
    plan["rolled_back"] = True
    result = OUT / "portal-result.json"
    result.write_text(json.dumps(plan, indent=2) + "\n")
    os.chown(result, 1000, 1000)
    print(json.dumps({"portal_removed": True, "old_site_preserved": True}))


def scope_user():
    assert os.geteuid() == 0
    plan = json.loads((OUT / "portal-result.json").read_text())
    assert plan["applied"] and "allowed_user" not in plan
    current = SITE.read_bytes()
    assert hashlib.sha256(current).hexdigest() == plan["candidate_site_sha256"]
    assert b"X-Portal-Authenticated-User" not in current
    updated = current.replace(
        b'        proxy_set_header Authorization "";',
        b'        proxy_set_header Authorization "";\n        proxy_set_header X-Portal-Authenticated-User $remote_user;',
    )
    assert updated != current
    SITE.write_bytes(updated)
    try:
        run(["/usr/sbin/nginx", "-t"])
        run(["/usr/sbin/nginx", "-s", "reload"])
    except Exception:
        SITE.write_bytes(current)
        run(["/usr/sbin/nginx", "-s", "reload"])
        raise
    folder = DEPLOY / "releases" / plan["release"]
    for path, content in [
        (folder / "nginx-before-scope.conf", current),
        (folder / "nginx.conf", updated),
    ]:
        path.write_bytes(content)
        os.chown(path, 1000, 1000)
    plan["candidate_site_sha256"] = hashlib.sha256(updated).hexdigest()
    plan["allowed_user"] = "HYN"
    result = OUT / "portal-result.json"
    result.write_text(json.dumps(plan, indent=2) + "\n")
    os.chown(result, 1000, 1000)
    print(json.dumps({"scoped_user": "HYN", "old_site_changed": False}))


if __name__ == "__main__":
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=["stage", "nginx-apply", "scope-user", "nginx-rollback", "upgrade"]
    )
    args = parser.parse_args()
    if args.action == "stage":
        stage()
    elif args.action == "scope-user":
        scope_user()
    elif args.action == "nginx-rollback":
        nginx_rollback()
    elif args.action == "upgrade":
        upgrade()
    else:
        nginx_apply()
