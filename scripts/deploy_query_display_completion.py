"""Stage/apply the bounded trajectory and skill recovery patch against running images."""

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
OUT = ROOT / "var/validation/20261107-query-display-completion"
SERVICES = ["platform", "actions", "worker", "hermes-main", "hermes-worker"]
BUSINESS = [
    "src/application/readonly_display.py",
    "src/service/hermes_api.py",
    "src/workflow_runtime/catalog.py",
    "src/application/trajectory_view.py",
    "src/hermes_runtime/trajectory_reply.py",
    "src/hermes_runtime/skill_guard.py",
    "src/hermes_runtime/skills.py",
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
    # Preserve the deployed API and native runtime outside the reviewed functions.
    payloads["src/service/hermes_api.py"] = replace_function(
        read_source("platform", "src/service/hermes_api.py"),
        payloads["src/service/hermes_api.py"],
        "get_result",
    )
    candidate_api = (ROOT / "src/service/hermes_api.py").read_text()
    for name in ["_poll", "list_results", "messages"]:
        payloads["src/service/hermes_api.py"] = replace_function(
            payloads["src/service/hermes_api.py"], candidate_api, name
        )
    node = next(
        n
        for n in ast.parse(candidate_api).body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "get_trajectory_view"
    )
    first = min(d.lineno for d in node.decorator_list) - 1
    payloads["src/service/hermes_api.py"] += (
        "\n\n" + "".join(candidate_api.splitlines(keepends=True)[first : node.end_lineno]) + "\n"
    )
    candidate_skills = payloads["src/hermes_runtime/skills.py"]
    live_skills = read_source("hermes-main", "src/hermes_runtime/skills.py")

    def handler_bounds(source):
        cls = next(
            n
            for n in ast.parse(source).body
            if isinstance(n, ast.ClassDef) and n.name == "NativeSkillBridge"
        )
        method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "handler")
        return method.lineno - 1, method.end_lineno

    a, b = handler_bounds(live_skills)
    c, d = handler_bounds(candidate_skills)
    payloads["src/hermes_runtime/skills.py"] = "".join(
        live_skills.splitlines(keepends=True)[:a]
        + candidate_skills.splitlines(keepends=True)[c:d]
        + live_skills.splitlines(keepends=True)[b:]
    )
    payloads[NATIVE] = replace_function(
        read_source("hermes-main", NATIVE), (ROOT / NATIVE).read_text(), "profile_config"
    )
    marker = "            actual = set(agent.valid_tool_names)"
    assert payloads[NATIVE].count(marker) == 1
    payloads[NATIVE] = payloads[NATIVE].replace(
        marker,
        "            if run_id and scope and args.profile == 'scat-main' and not args.background_job:\n                from hermes_runtime.trajectory_reply import attach_reply_guard\n\n                attach_reply_guard(agent, results, scope, run_id)\n"
        + marker,
    )
    for source in payloads.values():
        ast.parse(source)
    hashes = {p: hashlib.sha256(s.encode()).hexdigest() for p, s in payloads.items()}
    identity = hashlib.sha256(
        json.dumps({"base": previous, "files": hashes}, sort_keys=True).encode()
    ).hexdigest()[:12]
    release = "20261107-query-display-" + identity
    folder = DEPLOY / "releases" / release
    folder.mkdir(parents=True, exist_ok=True)
    folder.chmod(0o700)
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
        base_tag = "scat-hermes-query-base-" + role + ":" + base.split(":")[-1][:16]
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
        tag = "scat-hermes-query-display-" + role + ":" + identity
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
        "active_skill_bundle_changed": False,
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
    scheduler = run(
        ["systemctl", "--user", "is-active", "scat-auto-skill-learning.service"]
    ).strip()
    backup = OUT / "state-before-switch"
    backup.mkdir(exist_ok=False)
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
        try:
            run(
                compose_command(folder / "compose.yaml")
                + ["up", "-d", "--no-deps", "--wait", "--wait-timeout", "180", *SERVICES],
                timeout=240,
            )
        except Exception:
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
