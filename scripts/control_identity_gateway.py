"""Root-only gateway transition with an idle CPU release gate and exact rollback file."""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261108-release-readiness"
SITE = Path("/etc/nginx/conf.d/scat-hermes-portal.conf")
MAIN = Path("/etc/nginx/conf.d/scat.conf")
GATE = Path("/var/lib/scat-hermes-portal/maintenance")
INCLUDE = Path("/etc/scat/hermes-portal-gateway.inc")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(name, value):
    path = OUT / name
    path.write_text(json.dumps(value, indent=2) + "\n")
    os.chown(path, 1000, 1000)
    path.chmod(0o600)


def nginx():
    subprocess.run(["/usr/sbin/nginx", "-t"], capture_output=True, check=True, timeout=15)
    subprocess.run(
        ["/usr/bin/systemctl", "reload", "nginx"], capture_output=True, check=True, timeout=20
    )


def main(action):
    assert os.geteuid() == 0
    if action == "snapshot":
        assert not (OUT / "nginx-site-snapshot.json").exists()
        text = SITE.read_text()
        assert "X-Portal-Gateway-Token" not in text
        target = OUT / "nginx-site-snapshot.conf"
        target.write_text(text)
        os.chown(target, 1000, 1000)
        target.chmod(0o600)
        save("nginx-site-snapshot.json", {"portal_sha256": sha(SITE), "main_sha256": sha(MAIN)})
        print(json.dumps({"status": "snapshot_saved", "password_file_read": False}))
        return
    plan = json.loads((OUT / "portal-plan.json").read_text())
    folder = ROOT / "var/deploy/portal/releases" / plan["release"]
    assert folder.resolve().is_relative_to(ROOT / "var/deploy/portal/releases")
    assert sha(MAIN) == plan["old_main_site_sha256"]
    legacy_gate = Path("/etc/scat/hermes-portal.maintenance")
    if action == "prepare" and sha(SITE) == plan["new_site_sha256"] and INCLUDE.exists() and not GATE.exists():
        GATE.write_text("release-readiness followup maintenance\n")
        GATE.chmod(0o644)
        print(json.dumps({"status": "existing_identity_gateway_gated", "nginx_unchanged": True}))
        return
    if action == "prepare" and legacy_gate.exists() and not GATE.exists():
        assert sha(SITE) == plan["new_site_sha256"]
        text = SITE.read_text()
        assert str(legacy_gate) in text
        GATE.parent.mkdir(mode=0o755, exist_ok=True)
        GATE.write_text("release-readiness maintenance\n")
        GATE.chmod(0o644)
        text = text.replace(str(legacy_gate), str(GATE))
        (folder / "nginx.conf").write_text(text)
        SITE.write_text(text)
        nginx()
        plan["new_site_sha256"] = sha(SITE)
        save("portal-plan.json", plan)
        save(
            "gateway-gate-repair.json",
            {
                "status": "public_gate_path_repaired",
                "reason": "private_parent_directory_cannot_be_traversed_by_worker",
                "new_gate": str(GATE),
            },
        )
        legacy_gate.unlink()
        print(json.dumps({"status": "gateway_prepare_repaired", "main_unchanged": True}))
        return
    if action == "prepare":
        assert sha(SITE) == plan["old_site_sha256"]
        assert sha(folder / "nginx.conf") == plan["new_site_sha256"]
        assert not GATE.exists() and not INCLUDE.exists()
        key = ROOT / "var/deploy/portal/keys/front.key"
        assert not key.is_symlink() and key.stat().st_mode & 0o077 == 0
        token = key.read_text().strip()
        assert re.fullmatch("[A-Za-z0-9_-]{64}", token)
        backup = Path("/etc/scat/portal-readiness-backups")
        backup.mkdir(mode=0o700, exist_ok=True)
        if (backup / "portal.conf").exists():
            assert sha(backup / "portal.conf") == plan["old_site_sha256"]
        else:
            shutil.copyfile(SITE, backup / "portal.conf")
        (backup / "portal.conf").chmod(0o600)
        GATE.parent.mkdir(mode=0o755, exist_ok=True)
        GATE.write_text("release-readiness maintenance\n")
        GATE.chmod(0o644)
        INCLUDE.write_text('proxy_set_header X-Portal-Gateway-Token "' + token + '";\n')
        INCLUDE.chmod(0o600)
        shutil.copyfile(folder / "nginx.conf", SITE)
        SITE.chmod(0o600)
        try:
            nginx()
        except Exception:
            shutil.copyfile(backup / "portal.conf", SITE)
            nginx()
            GATE.unlink()
            INCLUDE.unlink()
            raise
        save(
            "gateway-prepared.json",
            {
                "status": "public_maintenance_gate_active",
                "portal_sha256": sha(SITE),
                "main_unchanged": True,
            },
        )
    elif action == "open":
        assert GATE.exists() and sha(SITE) == plan["new_site_sha256"]
        result = json.loads((OUT / "deployment-result.json").read_text())
        assert result["status"] == "applied_behind_gate"
        assert json.loads((OUT / "identity-live-private.json").read_text())["status"] == "passed"
        GATE.unlink()
        save(
            "gateway-result.json",
            {
                "status": "identity_portal_open",
                "principals": plan["principals"],
                "main_unchanged": True,
                "password_file_changed": False,
                "server_rebooted": False,
            },
        )
    elif action == "rollback":
        backup = Path("/etc/scat/portal-readiness-backups/portal.conf")
        assert sha(backup) == plan["old_site_sha256"]
        shutil.copyfile(backup, SITE)
        SITE.chmod(0o600)
        nginx()
        if GATE.exists():
            GATE.unlink()
        if INCLUDE.exists():
            INCLUDE.unlink()
        save("gateway-rollback.json", {"status": "old_portal_restored", "main_unchanged": True})
    print(json.dumps({"status": "gateway_" + action, "main_unchanged": True}))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=["snapshot", "prepare", "open", "rollback"])
    main(p.parse_args().action)
