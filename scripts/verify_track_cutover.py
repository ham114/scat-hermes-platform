"""Post-cutover read-only verification; never submits a model task or model request."""

import hashlib
import json
import sqlite3
import stat
import subprocess
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261107-track-drawing-failure"


def main():
    plan = json.loads((OUT / "deployment-plan.json").read_text())
    result = json.loads((OUT / "deployment-result.json").read_text())
    assert result["applied"]
    roles = [
        "platform",
        "actions",
        "worker",
        "hermes-main",
        "hermes-worker",
        "author-api",
        "hermes-author",
    ]
    rows = {}
    token = None
    for name in roles:
        row = json.loads(
            subprocess.check_output(
                ["docker", "inspect", "scat-hermes-h7b-" + name + "-1"], text=True
            )
        )[0]
        assert row["State"].get("Health", {}).get("Status") == "healthy", name
        role = (
            "author"
            if name == "author-api"
            else "native"
            if name.startswith("hermes-")
            else "business"
        )
        expected = plan["images"][role]["image_id"]
        if name == "hermes-author":
            expected = plan["base_images"]["native"]
        assert row["Image"] == expected, name
        rows[name] = {
            "health": "healthy",
            "image_id": row["Image"],
            "dns": row["HostConfig"].get("Dns"),
        }
        if name == "platform":
            token = dict(x.split("=", 1) for x in row["Config"]["Env"] if "=" in x)["AUTH_SECRET"]
    boot = hashlib.sha256(Path("/proc/sys/kernel/random/boot_id").read_bytes()).hexdigest()
    assert boot == json.loads((OUT / "cutover-preflight.json").read_text())["boot_sha256"]
    assert (ROOT / "var/deploy/autonomy/active-release").read_text().strip() == plan["release"]
    scheduler = subprocess.check_output(
        ["systemctl", "--user", "is-active", "scat-auto-skill-learning.service"], text=True
    ).strip()
    assert scheduler == "active"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def get(path):
        req = urllib.request.Request(
            "http://127.0.0.1:18120/api/v1/hermes" + path,
            headers={"Authorization": "Bearer " + token},
        )
        return json.load(opener.open(req, timeout=30))

    audit = json.loads((OUT / "audit.json").read_text())
    base = "/threads/" + audit["thread_id"] + "/runs/" + audit["run_id"]
    state = get(base)
    assert state["status"] == "failed"
    pages = []
    for old in audit["receipts"]:
        if old["operation"] != "get_scat_track_points":
            continue
        data = get(base + "/results/" + old["result_id"])
        analysis = data["analysis_result"]
        trajectory = next(b for b in analysis["blocks"] if b["type"] == "trajectory_2d")
        assert len(trajectory["points"]) == old["point_count"]
        assert analysis["context"]["readonly_payload_sha256"] == data["payload_sha256"]
        pages.append(
            {
                "start": old["pagination"]["start"],
                "points": len(trajectory["points"]),
                "chart": True,
            }
        )
    catalog = get("/workflow-catalog")
    assert catalog["production_gpu"] is False
    assert [r["model_id"] for r in catalog["models"]] == catalog["model_ids"]
    backups = list((OUT / "state-before-switch").rglob("*"))
    dbs = [p for p in backups if p.suffix in {".sqlite", ".db"}]
    assert dbs
    for path in dbs:
        assert stat.S_IMODE(path.stat().st_mode) & 0o077 == 0
        with sqlite3.connect("file:" + path.as_posix() + "?mode=ro", uri=True) as db:
            assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    source_probe = (
        "import pathlib,hashlib,json; expected="
        + repr(plan["source_hashes"])
        + "; print(json.dumps({p:hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()==h for p,h in expected.items()}))"
    )
    source_checks = {}
    for name in plan["services"]:
        py = (
            "/opt/conda/envs/"
            + ("scat-hermes" if name.startswith("hermes-") else "scat-agent")
            + "/bin/python"
        )
        checked = json.loads(
            subprocess.check_output(
                ["docker", "exec", "scat-hermes-h7b-" + name + "-1", py, "-B", "-c", source_probe],
                text=True,
            )
        )
        assert all(checked.values())
        source_checks[name] = checked
    report = {
        "status": "passed",
        "active_release": plan["release"],
        "containers": rows,
        "server_not_rebooted": True,
        "boot_sha256": boot,
        "scheduler": scheduler,
        "private_backups_checked": len(dbs),
        "existing_failed_run_status": state["status"],
        "existing_receipt_pages": pages,
        "source_checks": source_checks,
        "production_gpu": False,
        "new_model_calls": 0,
        "new_prediction_requests": 0,
    }
    (OUT / "cutover-verification.json").write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                "status": report["status"],
                "release": plan["release"],
                "server_not_rebooted": True,
                "pages": len(pages),
                "backups": len(dbs),
            }
        )
    )


if __name__ == "__main__":
    main()
