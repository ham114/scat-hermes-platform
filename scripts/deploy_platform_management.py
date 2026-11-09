"""Publish tested management routes and a DNS-scoped memory proxy without rebooting."""

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import time
import urllib.request
from pathlib import Path

import deploy_forecast_contract as base

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "var/deploy/autonomy"
OUT = ROOT / "var/validation/20261108-platform-closeout"
SERVICES = ["platform", "actions", "worker", "author-api"]
FILES = [
    "src/service/memory_api.py",
    "src/hermes_runtime/honcho_memory.py",
    "src/hermes_runtime/experience_learning.py",
    "src/hermes_runtime/agent_settings.py",
    "src/service/agent_settings_api.py",
    "src/service/skill_authoring_api.py",
    "src/hermes_runtime/daily_budget.py",
    "scripts/run_autonomy_proxy.py",
]


def run(args, timeout=180):
    return subprocess.check_output(
        args, cwd=ROOT, stderr=subprocess.STDOUT, text=True, timeout=timeout
    )


def mount(service, path, readonly=True):
    value = str(ROOT / path)
    service["volumes"] = [m for m in service["volumes"] if m["target"] != value]
    service["volumes"].append(
        {
            "type": "bind",
            "source": value,
            "target": value,
            "read_only": readonly,
            "bind": {"create_host_path": False},
        }
    )


def proxy_unit(image):
    args = [
        "/usr/bin/docker",
        "run",
        "--rm",
        "--pull=never",
        "--name=scat-memory-model-proxy-1",
        "--network=host",
        "--dns=223.5.5.5",
        "--dns=119.29.29.29",
        "--read-only",
        "--user=1000:1000",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true",
        "--pids-limit=128",
        "--memory=1g",
        "--cpus=1",
        "--log-opt=max-size=4m",
        "--log-opt=max-file=3",
        "--tmpfs=/tmp:rw,noexec,nosuid,size=64m",
        "--env=PYTHONDONTWRITEBYTECODE=1",
        "--env=SCAT_MODEL_CAMPAIGN_FILE",
    ]
    for path, ro in [
        ("var/deploy/autonomy/proxy.json", True),
        ("var/deploy/autonomy/secrets", True),
        ("var/deploy/honcho/secrets/honcho.env", True),
        ("var/autonomy/budget", False),
        ("var/deploy/autonomy/campaign-closeout.json", True),
    ]:
        absolute = str(ROOT / path)
        args += [
            "--mount=type=bind,src=" + absolute + ",dst=" + absolute + (",readonly" if ro else "")
        ]
    args += [
        "--workdir=" + str(ROOT),
        "--entrypoint=/opt/conda/envs/scat-agent/bin/python",
        image,
        "-B",
        "scripts/run_autonomy_proxy.py",
    ]
    assert all(not any(c.isspace() for c in x) for x in args)
    return (
        "[Unit]\nDescription=SCAT memory model proxy with scoped DNS and persistent budget\nAfter=network-online.target\n"
        "[Service]\nType=simple\nWorkingDirectory="
        + str(ROOT)
        + "\nEnvironment=SCAT_MODEL_CAMPAIGN_FILE=var/deploy/autonomy/campaign-closeout.json\n"
        "ExecStart=/usr/bin/sg docker -c 'exec "
        + " ".join(args)
        + "'\nExecStop=/usr/bin/sg docker -c 'exec /usr/bin/docker stop --time=30 scat-memory-model-proxy-1'\n"
        "TimeoutStopSec=45\nRestart=on-failure\nRestartSec=10\nUMask=0077\n[Install]\nWantedBy=default.target\n"
    )


def stage():
    previous = (DEPLOY / "active-release").read_text().strip()
    original = DEPLOY / "releases" / previous
    compose = json.loads((original / "compose.yaml").read_text())
    live = {name: base.container(name) for name in SERVICES}
    assert all(v["State"]["Health"]["Status"] == "healthy" for v in live.values())
    assert all(v["Image"] == compose["services"][name]["image"] for name, v in live.items())
    hashes = {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in FILES}
    manifest = json.loads((ROOT / "config/agent-settings-targets.example.json").read_text())
    assert all(
        (ROOT / p / "profiles" / role / "config.yaml").is_file()
        for role, p in manifest["profiles"].items()
    )
    manifest_path = DEPLOY / "settings-targets.json"
    if manifest_path.exists():
        assert json.loads(manifest_path.read_text()) == manifest
    else:
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        manifest_path.chmod(0o600)
    (ROOT / "var/agent-settings").mkdir(exist_ok=True)
    (ROOT / "var/agent-settings").chmod(0o700)
    identity = hashlib.sha256(
        json.dumps(
            {"previous": previous, "hashes": hashes, "manifest": manifest}, sort_keys=True
        ).encode()
    ).hexdigest()[:12]
    release = "20261108-management-" + identity
    folder = DEPLOY / "releases" / release
    folder.mkdir(exist_ok=False)
    folder.chmod(0o700)
    (folder / "rollback-compose.json").write_bytes((original / "compose.yaml").read_bytes())
    for p in FILES:
        dest = folder / "patch" / p
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / p, dest)
    images = json.loads((original / "images.lock.json").read_text())
    for role, service in [("business", "platform"), ("author", "author-api")]:
        assert shutil.disk_usage(ROOT).free > 10 * 1024**3
        baseline = live[service]["Image"]
        tag = "scat-management-base-" + role + ":" + identity
        run(["docker", "image", "tag", baseline, tag])
        dockerfile = folder / ("Dockerfile." + role)
        dockerfile.write_text(
            "FROM "
            + tag
            + "\n"
            + "".join(
                "COPY --chown=1000:1000 patch/" + p + " " + str(ROOT / p) + "\n" for p in FILES
            )
        )
        target = "scat-management-" + role + ":" + identity
        (OUT / ("build-" + role + ".log")).write_text(
            run(
                [
                    "docker",
                    "build",
                    "--network=none",
                    "--pull=false",
                    "-f",
                    str(dockerfile),
                    "-t",
                    target,
                    str(folder),
                ],
                300,
            )
        )
        images[role] = {
            "tag": target,
            "image_id": run(["docker", "image", "inspect", target, "--format", "{{.Id}}"]).strip(),
            "base_image_id": baseline,
        }
    for name in SERVICES:
        compose["services"][name]["image"] = images[
            "author" if name == "author-api" else "business"
        ]["image_id"]
        compose["services"][name]["dns"] = live[name]["HostConfig"].get("Dns") or []
    author = compose["services"]["author-api"]
    author["environment"]["SCAT_AGENT_SETTINGS_MANIFEST"] = (
        "var/deploy/autonomy/settings-targets.json"
    )
    for path in [
        "var/deploy/autonomy/settings-targets.json",
        manifest["autonomy"],
        manifest["proxy"],
        manifest["policy"],
        manifest["tools"],
        "var/autonomy/budget",
        *manifest["profiles"].values(),
    ]:
        assert (ROOT / path).exists()
        mount(author, path)
    mount(author, "var/agent-settings", False)
    for filename, value in [("compose.yaml", compose), ("images.lock.json", images)]:
        (folder / filename).write_text(json.dumps(value, indent=2) + "\n")
    source = json.loads((original / "source.lock.json").read_text())
    source.update(hashes)
    (folder / "source.lock.json").write_text(json.dumps(source, indent=2) + "\n")
    if (original / "features.json").exists():
        shutil.copyfile(original / "features.json", folder / "features.json")
    (folder / "memory-proxy.service").write_text(proxy_unit(images["business"]["image_id"]))
    plan = {
        "previous": previous,
        "release": release,
        "services": SERVICES,
        "images": images,
        "source_hashes": hashes,
        "before_container_ids": {n: row["Id"] for n, row in live.items()},
        "boot_sha256": hashlib.sha256(
            Path("/proc/sys/kernel/random/boot_id").read_bytes()
        ).hexdigest(),
        "skill_change": False,
        "gpu_configuration_changed": False,
        "host_dns_changed": False,
        "applied": False,
    }
    (OUT / "deployment-plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    run(base.compose_command(folder / "compose.yaml") + ["config", "--quiet"])
    # No model request: check container resolver and verified TLS only.
    code = 'import socket,ssl,json;s=socket.create_connection(("api.deepseek.com",443),timeout=5);t=ssl.create_default_context().wrap_socket(s,server_hostname="api.deepseek.com");print(json.dumps({"dns_tls":"passed","tls":t.version()}));t.close()'
    result = run(
        [
            "docker",
            "run",
            "--rm",
            "--pull=never",
            "--network=host",
            "--dns=223.5.5.5",
            "--dns=119.29.29.29",
            "--read-only",
            "--user=1000:1000",
            "--cap-drop=ALL",
            "--entrypoint=/opt/conda/envs/scat-agent/bin/python",
            images["business"]["image_id"],
            "-B",
            "-c",
            code,
        ],
        30,
    )
    (OUT / "proxy-dns-tls.json").write_text(result)
    print(
        json.dumps({"built": True, "release": release, "applied": False, "proxy_dns_tls": "passed"})
    )


def backup(path, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    with (
        sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True) as src,
        sqlite3.connect(target) as dst,
    ):
        src.backup(dst)
        assert dst.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    target.chmod(0o600)


def review_rollback():
    plan = json.loads((OUT / "deployment-plan.json").read_text())
    assert not (OUT / "deployment-result.json").exists()
    assert (DEPLOY / "active-release").read_text().strip() == plan["previous"]
    folder = DEPLOY / "releases" / plan["release"]
    rollback = json.loads((folder / "rollback-compose.json").read_text())
    assert all(
        base.container(n)["Image"] == rollback["services"][n]["image"]
        and base.container(n)["State"]["Health"]["Status"] == "healthy"
        for n in SERVICES
    )
    assert (DEPLOY / "units/scat-memory-model-proxy.service").read_bytes() == (
        OUT / "private-before-management/memory-proxy.service"
    ).read_bytes()
    base.idle()
    with sqlite3.connect(
        "file:" + str(ROOT / "var/autonomy/budget/state.sqlite") + "?mode=ro", uri=True
    ) as db:
        assert (
            db.execute("SELECT count(*) FROM model_calls WHERE state='reserved'").fetchone()[0] == 0
        )
    assert not (OUT / "deployment-plan-first.json").exists()
    shutil.copyfile(OUT / "deployment-plan.json", OUT / "deployment-plan-first.json")
    shutil.copyfile(OUT / "apply.log", OUT / "apply-first-failed.log")
    shutil.copyfile(folder / "memory-proxy.service", folder / "memory-proxy-first-failed.service")
    (folder / "memory-proxy.service").write_text(proxy_unit(plan["images"]["business"]["image_id"]))
    plan["before_container_ids"] = {n: base.container(n)["Id"] for n in SERVICES}
    plan["reviewed_rollback"] = True
    plan["attempt"] = 2
    (OUT / "deployment-plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    print(
        json.dumps(
            {"reviewed_rollback": True, "retry_attempt": 2, "model_requests_before_retry": 0}
        )
    )


def apply(*, defer_scheduler=False, attempt=1):
    plan = json.loads((OUT / "deployment-plan.json").read_text())
    folder = DEPLOY / "releases" / plan["release"]
    assert (DEPLOY / "active-release").read_text().strip() == plan["previous"]
    assert (
        hashlib.sha256(Path("/proc/sys/kernel/random/boot_id").read_bytes()).hexdigest()
        == plan["boot_sha256"]
    )
    assert all(base.container(n)["Id"] == v for n, v in plan["before_container_ids"].items())
    base.idle()
    scheduler = run(
        ["systemctl", "--user", "is-active", "scat-auto-skill-learning.service"]
    ).strip()
    assert attempt == plan.get("attempt", 1)
    private = OUT / (
        "private-before-management" if attempt == 1 else "private-before-management-attempt2"
    )
    private.mkdir(exist_ok=False)
    private.chmod(0o700)
    unit = DEPLOY / "units/scat-memory-model-proxy.service"
    shutil.copyfile(unit, private / "memory-proxy.service")
    (private / "memory-proxy.service").chmod(0o600)
    campaign_path = DEPLOY / "campaign-closeout.json"
    if attempt == 1:
        assert not campaign_path.exists(), "existing_campaign_requires_audit"
        campaign_path.write_text(
            json.dumps(
                {
                    "id": plan["release"],
                    "max_requests": 24,
                    "max_units": 750000,
                    "expires_at": time.time() + 1800,
                }
            )
            + "\n"
        )
        campaign_path.chmod(0o600)
    else:
        campaign = json.loads(campaign_path.read_text())
        assert (
            plan["reviewed_rollback"]
            and campaign["id"] == plan["release"]
            and campaign["max_requests"] == 24
            and campaign["max_units"] == 750000
            and time.time() < campaign["expires_at"]
        )
    try:
        run(["systemctl", "--user", "stop", "scat-auto-skill-learning.service"])
        base.idle()
        run(["systemctl", "--user", "stop", "scat-memory-model-proxy.service"])
        with sqlite3.connect(
            "file:" + str(ROOT / "var/autonomy/budget/state.sqlite") + "?mode=ro", uri=True
        ) as db:
            assert (
                db.execute("SELECT count(*) FROM model_calls WHERE state='reserved'").fetchone()[0]
                == 0
            )
        run(
            base.compose_command(folder / "rollback-compose.json")
            + ["--profile", "author", "stop", "--timeout", "30", *SERVICES]
        )
        base.idle()
        for path in [
            ROOT / "var/autonomy/budget/state.sqlite",
            ROOT / "var/autonomy/memory/state.sqlite",
            *list((ROOT / "var/deploy/h7b/shared/state").glob("*.sqlite")),
            ROOT / "var/deploy/h7b/shared/skills/catalog.sqlite",
        ]:
            if path.exists():
                backup(path, private / path.relative_to(ROOT))
        try:
            run(
                base.compose_command(folder / "compose.yaml")
                + [
                    "--profile",
                    "author",
                    "up",
                    "-d",
                    "--no-deps",
                    "--wait",
                    "--wait-timeout",
                    "180",
                    *SERVICES,
                ],
                240,
            )
            shutil.copyfile(folder / "memory-proxy.service", unit)
            unit.chmod(0o600)
            run(["systemctl", "--user", "daemon-reload"])
            run(["systemctl", "--user", "start", "scat-memory-model-proxy.service"])
            for health_attempt in range(30):
                try:
                    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                        "http://127.0.0.1:18142/health", timeout=1
                    ) as response:
                        assert json.load(response)["status"] == "ready"
                    break
                except OSError:
                    if health_attempt == 29:
                        raise
                    time.sleep(0.2)
            (DEPLOY / "active-release").write_text(plan["release"])
            plan["applied"] = True
            plan["scheduler_restore_pending"] = defer_scheduler and scheduler == "active"
            (OUT / "deployment-result.json").write_text(json.dumps(plan, indent=2) + "\n")
        except Exception:
            run(["systemctl", "--user", "stop", "scat-memory-model-proxy.service"])
            shutil.copyfile(private / "memory-proxy.service", unit)
            run(["systemctl", "--user", "daemon-reload"])
            run(
                base.compose_command(folder / "rollback-compose.json")
                + ["--profile", "author", "up", "-d", "--no-deps", "--wait", *SERVICES],
                240,
            )
            run(["systemctl", "--user", "start", "scat-memory-model-proxy.service"])
            raise
    finally:
        if not plan.get("applied"):
            run(
                base.compose_command(folder / "rollback-compose.json")
                + ["--profile", "author", "up", "-d", "--no-deps", "--wait", *SERVICES],
                240,
            )
            run(["systemctl", "--user", "start", "scat-memory-model-proxy.service"])
        if scheduler == "active" and (not defer_scheduler or not plan.get("applied")):
            run(["systemctl", "--user", "start", "scat-auto-skill-learning.service"])
    print(json.dumps({"applied": True, "release": plan["release"], "server_rebooted": False}))


if __name__ == "__main__":
    os.umask(0o077)
    OUT.mkdir(parents=True, exist_ok=True)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["build", "apply", "review-rollback"])
    parser.add_argument("--defer-scheduler", action="store_true")
    parser.add_argument("--attempt", type=int, choices=[1, 2], default=1)
    args = parser.parse_args()
    if args.action == "build":
        stage()
    elif args.action == "review-rollback":
        review_rollback()
    else:
        apply(defer_scheduler=args.defer_scheduler, attempt=args.attempt)
