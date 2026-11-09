"""Stage/apply the exact observation-window and future-forecast patch against running images."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "var/deploy/autonomy"
OUT = ROOT / "var/validation/20261107-forecast-contract"
SERVICES = ["platform", "actions", "worker", "hermes-main", "hermes-worker"]
BUSINESS = [
    "src/workflow_runtime/contracts.py",
    "src/workflow_runtime/catalog.py",
    "src/workflow_runtime/data.py",
    "src/workflow_runtime/windows.py",
    "src/workflow_runtime/forecast.py",
    "src/workflow_runtime/numerics.py",
    "src/workflow_runtime/report.py",
    "src/workflow_runtime/gpu.py",
    "src/application/workflow_tasks.py",
    "src/application/workflow_operations.py",
    "src/application/skill_jobs.py",
    "services/hermes/workflow-operations.v1.json",
    "src/hermes_runtime/skills.py",
    "src/hermes_runtime/skill_guard.py",
    "src/analysis_runs/contracts.py",
]
NATIVE = "scripts/run_hermes_api.py"


def run(args, timeout=180):
    return subprocess.check_output(
        args, cwd=ROOT, text=True, stderr=subprocess.STDOUT, timeout=timeout
    )


def container(name):
    return json.loads(run(["docker", "inspect", "scat-hermes-h7b-" + name + "-1"]))[0]


def read_source(service, path):
    return run(["docker", "exec", "scat-hermes-h7b-" + service + "-1", "cat", str(ROOT / path)])


def replace_function(original, candidate, name):
    def bounds(source):
        node = next(
            n
            for n in ast.parse(source).body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
        )
        return node.lineno - 1, node.end_lineno

    a, b = bounds(original)
    c, d = bounds(candidate)
    return "".join(
        original.splitlines(keepends=True)[:a]
        + candidate.splitlines(keepends=True)[c:d]
        + original.splitlines(keepends=True)[b:]
    )


def candidate_skill():
    code = """import json
from pathlib import Path
from hermes_runtime.release_store import ReleaseStore
s=ReleaseStore(Path.cwd()); r=s.current()
with s.connect() as db: active=db.execute('SELECT sha,generation FROM active WHERE id=1').fetchone()
m=json.loads((s.bundles/r.bundle_sha256/'lock.json').read_text())
print(json.dumps({'manifest':m,'files':{k:v.decode() for k,v in r.files.items()},'previous_sha':active[0],'previous_generation':active[1]}))
"""
    body = json.loads(
        run(
            [
                "docker",
                "exec",
                "scat-hermes-h7b-hermes-main-1",
                "/opt/conda/envs/scat-hermes/bin/python",
                "-c",
                code,
            ]
        )
    )
    versions = {
        "scat-model-comparison": "0.3.0",
        "scat-evaluation": "0.2.0",
        "scat-report": "0.2.0",
    }
    body["manifest"]["bundle_version"] = "s5-forecast-contract.1.0"
    for entry in body["manifest"]["skills"]:
        name = entry["name"]
        if name in versions:
            entry["version"] = versions[name]
            payload = (ROOT / "skills" / name / "SKILL.md").read_bytes()
            body["files"][name + "/SKILL.md"] = payload.decode()
            entry["files"]["SKILL.md"] = hashlib.sha256(payload).hexdigest()
        for child in entry.get("requires_skills", {}):
            if child in versions:
                entry["requires_skills"][child] = versions[child]
    body["target_sha"] = hashlib.sha256(
        json.dumps(
            body["manifest"], sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()
    return body


def skill_update(plan, action):
    body = json.loads((DEPLOY / "releases" / plan["release"] / "skill-release.json").read_text())
    assert body["target_sha"] == plan["skill_change"]["target_sha"]
    environment = dict(
        item.split("=", 1) for item in container("platform")["Config"]["Env"] if "=" in item
    )
    code = """import json,sys
from pathlib import Path
from hermes_runtime.release_store import ReleaseStore
d=json.load(sys.stdin); s=ReleaseStore(Path.cwd()); action=d['action']; b=d['bundle']
if action=='register':
    r=s.install(b['manifest'],{k:v.encode() for k,v in b['files'].items()}); assert r.bundle_sha256==b['target_sha']
else:
    target=b['previous_sha'] if action=='rollback' else b['target_sha']; s.load(target)
    with s.connect() as db:
        db.execute('BEGIN IMMEDIATE'); row=db.execute('SELECT sha,generation FROM active WHERE id=1').fetchone()
        expected=b['target_sha'] if action=='rollback' else b['previous_sha']; assert row[0]==expected
        if action=='activate': assert row[1]==b['previous_generation']
        if target!=row[0]:
            db.execute('UPDATE active SET sha=?,generation=generation+1 WHERE id=1',(target,))
            db.execute('INSERT INTO release_events(at,actor,previous,target,preview) VALUES(unixepoch(),?,?,?,?)',('operator:forecast-contract',row[0],target,d['release']+':'+action))
print(json.dumps({'skill_action':action,'verified':True}))
"""
    command = [
        "docker",
        "run",
        "--rm",
        "-i",
        "--network=none",
        "--user",
        "1000:1000",
        "--volumes-from",
        "scat-hermes-h7b-platform-1:rw",
        "-w",
        str(ROOT),
        "-e",
        "SCAT_SKILL_RELEASE_STATE="
        + environment.get("SCAT_SKILL_RELEASE_STATE", "var/skills/catalog.sqlite"),
        "--entrypoint",
        "/opt/conda/envs/scat-agent/bin/python",
        plan["images"]["business"]["image_id"],
        "-c",
        code,
    ]
    result = subprocess.run(
        command,
        input=json.dumps({"bundle": body, "action": action, "release": plan["release"]}),
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    print(result.stdout.strip(), flush=True)


def stage():
    previous = (DEPLOY / "active-release").read_text().strip()
    original = DEPLOY / "releases" / previous
    compose = json.loads((original / "compose.yaml").read_text())
    live = {name: container(name) for name in SERVICES}
    for name, row in live.items():
        assert row["Image"] == compose["services"][name]["image"], (
            "running_image_differs_from_release"
        )
        assert row["State"].get("Health", {}).get("Status") == "healthy"
        if row["HostConfig"].get("Dns"):
            compose["services"][name]["dns"] = row["HostConfig"]["Dns"]
        else:
            compose["services"][name].pop("dns", None)
    payloads = {path: (ROOT / path).read_text() for path in BUSINESS}
    live_jobs = read_source("platform", "src/application/skill_jobs.py")
    source_node = next(
        n
        for n in ast.parse(live_jobs).body
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "SOURCES" for t in n.targets)
    )
    sources = list(
        dict.fromkeys(
            [
                *ast.literal_eval(source_node.value),
                "src/workflow_runtime/windows.py",
                "src/workflow_runtime/forecast.py",
            ]
        )
    )
    replacement = "SOURCES = (\n" + "".join('    "' + p + '",\n' for p in sources) + ")\n"
    original_lines = live_jobs.splitlines(keepends=True)
    payloads["src/application/skill_jobs.py"] = (
        "".join(original_lines[: source_node.lineno - 1])
        + replacement
        + "".join(original_lines[source_node.end_lineno :])
    )
    payloads[NATIVE] = replace_function(
        read_source("hermes-main", NATIVE), (ROOT / NATIVE).read_text(), "profile_config"
    )
    payloads["src/hermes_runtime/skills.py"] = replace_function(
        read_source("hermes-main", "src/hermes_runtime/skills.py"),
        (ROOT / "src/hermes_runtime/skills.py").read_text(),
        "register_native_skills",
    )
    for path, value in payloads.items():
        if path.endswith(".py"):
            ast.parse(value)
    skill = candidate_skill()
    hashes = {p: hashlib.sha256(s.encode()).hexdigest() for p, s in payloads.items()}
    identity = hashlib.sha256(
        json.dumps(
            {"base": previous, "files": hashes, "skill": skill["target_sha"]}, sort_keys=True
        ).encode()
    ).hexdigest()[:12]
    release = "20261107-forecast-contract-" + identity
    folder = DEPLOY / "releases" / release
    folder.mkdir(parents=True, exist_ok=True)
    folder.chmod(0o700)
    (folder / "skill-release.json").write_text(json.dumps(skill, ensure_ascii=False))
    (folder / "rollback-compose.json").write_text(json.dumps(compose, indent=2))
    for path, text in payloads.items():
        target = folder / "patch" / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    bases = {"business": live["platform"]["Image"], "native": live["hermes-main"]["Image"]}
    assert all(live[n]["Image"] == bases["business"] for n in ["actions", "worker"])
    assert live["hermes-worker"]["Image"] == bases["native"]
    images = json.loads((original / "images.lock.json").read_text())
    for role, base in bases.items():
        # Workflow snapshots hash both script and catalog sources in both roles.
        paths = list(payloads)
        dockerfile = folder / ("Dockerfile." + role)
        base_tag = "scat-hermes-forecast-base-" + role + ":" + base.split(":")[-1][:16]
        run(["docker", "image", "tag", base, base_tag])
        assert run(["docker", "image", "inspect", base_tag, "--format", "{{.Id}}"]).strip() == base
        dockerfile.write_text(
            "FROM "
            + base_tag
            + "\n"
            + "".join(
                "COPY --chown=1000:1000 patch/" + path + " " + str(ROOT / path) + "\n"
                for path in paths
            )
        )
        tag = "scat-hermes-forecast-contract-" + role + ":" + identity
        if shutil.disk_usage(ROOT).free < 5 * 1024**3:
            raise ValueError("insufficient_build_disk_reserve")
        with (OUT / ("build-" + role + ".log")).open("a") as log:
            subprocess.run(
                [
                    "docker",
                    "build",
                    "--network=none",
                    "--pull=false",
                    "-f",
                    str(dockerfile),
                    "-t",
                    tag,
                    str(folder),
                ],
                cwd=ROOT,
                stdout=log,
                stderr=log,
                check=True,
                timeout=300,
            )
        images[role] = {
            "tag": tag,
            "image_id": run(["docker", "image", "inspect", tag, "--format", "{{.Id}}"]).strip(),
            "base_image_id": base,
        }
    for name in SERVICES:
        compose["services"][name]["image"] = images[
            "native" if name.startswith("hermes-") else "business"
        ]["image_id"]
    (folder / "compose.yaml").write_text(json.dumps(compose, indent=2))
    (folder / "images.lock.json").write_text(json.dumps(images, indent=2))
    source = json.loads((original / "source.lock.json").read_text())
    source.update(hashes)
    (folder / "source.lock.json").write_text(json.dumps(source, indent=2))
    if (original / "features.json").exists():
        shutil.copy2(original / "features.json", folder / "features.json")
    plan = {
        "previous": previous,
        "release": release,
        "services": SERVICES,
        "source_hashes": hashes,
        "base_images": bases,
        "images": images,
        "active_skill_bundle_changed": skill["previous_sha"] != skill["target_sha"],
        "skill_change": {
            k: skill[k] for k in ["previous_sha", "previous_generation", "target_sha"]
        },
        "gpu_configuration_changed": False,
        "author_services_changed": False,
        "existing_dns_preserved": True,
        "applied": False,
        "boot_sha256": hashlib.sha256(
            Path("/proc/sys/kernel/random/boot_id").read_bytes()
        ).hexdigest(),
    }
    (OUT / "deployment-plan.json").write_text(json.dumps(plan, indent=2))
    run(compose_command(folder / "compose.yaml") + ["config", "--quiet"])
    print(
        json.dumps({"release": release, "files": list(hashes), "build": "passed", "applied": False})
    )


def compose_command(path):
    return [
        "docker",
        "compose",
        "--project-name",
        "scat-hermes-h7b",
        "-f",
        str(path),
        "--profile",
        "author",
    ]


def idle():
    with sqlite3.connect(
        "file:" + str(ROOT / "var/deploy/h7b/shared/state/results.sqlite") + "?mode=ro", uri=True
    ) as db:
        if db.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='trajectory_views'"
        ).fetchone()[0]:
            import time

            if db.execute(
                "SELECT count(*) FROM trajectory_views WHERE state='building' AND lease>?",
                (time.time(),),
            ).fetchone()[0]:
                raise ValueError("trajectory_view_is_active")
    for path, query in [
        (
            "var/deploy/h7b/shared/state/tasks.sqlite",
            "SELECT count(*) FROM model_tasks WHERE state IN ('queued','claimed','running','unknown')",
        ),
        ("var/deploy/h7b/shared/state/bindings.sqlite", "SELECT count(*) FROM runtime_admissions"),
        (
            "var/deploy/h7b/shared/skills/catalog.sqlite",
            "SELECT count(*) FROM trials WHERE state='running'",
        ),
        (
            "var/autonomy/learning/state.sqlite",
            "SELECT count(*) FROM automatic_skill_jobs WHERE state IN ('reserved','authoring','unknown','trial_running')",
        ),
    ]:
        with sqlite3.connect("file:" + (ROOT / path).as_posix() + "?mode=ro", uri=True) as db:
            if db.execute(query).fetchone()[0]:
                raise ValueError("active_or_unknown_work_blocks_switch")


def apply():
    plan = json.loads((OUT / "deployment-plan.json").read_text())
    folder = DEPLOY / "releases" / plan["release"]
    assert (DEPLOY / "active-release").read_text().strip() == plan["previous"]
    assert (
        hashlib.sha256(Path("/proc/sys/kernel/random/boot_id").read_bytes()).hexdigest()
        == plan["boot_sha256"]
    )
    for name in SERVICES:
        expected = plan["base_images"]["native" if name.startswith("hermes-") else "business"]
        assert container(name)["Image"] == expected, "running_image_changed_since_plan"
    for path, expected in plan["source_hashes"].items():
        assert hashlib.sha256((folder / "patch" / path).read_bytes()).hexdigest() == expected
    idle()
    skill_update(plan, "register")
    scheduler = run(
        ["systemctl", "--user", "is-active", "scat-auto-skill-learning.service"]
    ).strip()
    backup = OUT / "state-before-switch" / plan["release"]
    backup.mkdir(parents=True, exist_ok=False)
    backup.chmod(0o700)
    platform_stopped = False
    try:
        run(["systemctl", "--user", "stop", "scat-auto-skill-learning.service"])
        run(
            compose_command(folder / "rollback-compose.json")
            + ["stop", "--timeout", "30", "platform"]
        )
        platform_stopped = True
        idle()
        for relative in [
            "var/deploy/h7b/shared/state",
            "var/deploy/h7b/shared/skills",
            "var/autonomy/learning",
            "var/deploy/h7b/shared/homes/main",
            "var/deploy/h7b/shared/homes/worker",
        ]:
            for path in (ROOT / relative).glob("*"):
                if path.suffix not in {".sqlite", ".db"}:
                    continue
                target = backup / path.relative_to(ROOT)
                target.parent.mkdir(parents=True, exist_ok=True)
                with (
                    sqlite3.connect("file:" + path.as_posix() + "?mode=ro", uri=True) as src,
                    sqlite3.connect(target) as dst,
                ):
                    src.backup(dst)
                target.chmod(0o600)
        skill_update(plan, "activate")
        try:
            run(
                compose_command(folder / "compose.yaml")
                + ["up", "-d", "--no-deps", "--wait", "--wait-timeout", "180", *SERVICES],
                timeout=240,
            )
        except Exception:
            skill_update(plan, "rollback")
            run(
                compose_command(folder / "rollback-compose.json")
                + ["up", "-d", "--no-deps", "--wait", "--wait-timeout", "180", *SERVICES],
                timeout=240,
            )
            platform_stopped = False
            raise
        (DEPLOY / "active-release").write_text(plan["release"])
        plan["applied"] = True
        plan["health"] = {
            name: container(name)["State"].get("Health", {}).get("Status") for name in SERVICES
        }
        (OUT / "deployment-result.json").write_text(json.dumps(plan, indent=2))
        print(json.dumps({"release": plan["release"], "health": plan["health"], "applied": True}))
    finally:
        if platform_stopped and not plan.get("applied"):
            run(
                compose_command(folder / "rollback-compose.json")
                + ["up", "-d", "--no-deps", "--wait", "platform"],
                timeout=180,
            )
        if scheduler == "active":
            run(["systemctl", "--user", "start", "scat-auto-skill-learning.service"])


if __name__ == "__main__":
    os.umask(0o077)
    OUT.mkdir(parents=True, exist_ok=True)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["build", "apply"])
    action = parser.parse_args().action
    stage() if action == "build" else apply()
