"""Build and switch the diagnostics release using the active deployment as its base."""

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "var/deploy/langfuse/release"
SOURCE = ROOT / ".tmp/langfuse/candidate"
FILES = [
    "src/hermes_runtime/diagnostics.py",
    "src/hermes_runtime/client.py",
    "src/hermes_runtime/entrypoint.py",
    "src/hermes_runtime/agent_settings.py",
    "src/service/diagnostics_api.py",
    "src/service/hermes_app.py",
    "src/service/hermes_api.py",
    "src/application/model_tasks.py",
    "src/application/workflow_tasks.py",
    "scripts/run_hermes_api.py",
    "services/hermes/langfuse.lock.json",
]
ROLES = [
    "platform",
    "actions",
    "worker",
    "hermes-main",
    "hermes-worker",
    "hermes-author",
    "author-api",
]


def run(args, *, timeout=120):
    try:
        return subprocess.check_output(
            args, cwd=ROOT, text=True, stderr=subprocess.STDOUT, timeout=timeout
        )
    except subprocess.CalledProcessError as exc:
        if STATE.exists():
            path = STATE / "last-command-error.log"
            path.write_text(exc.output or "Command failed without output")
            path.chmod(0o600)
        raise


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    path.chmod(0o600)


def inspect(role):
    return json.loads(run(["docker", "inspect", "scat-hermes-h7b-" + role + "-1"]))[0]


def idle():
    base = ROOT / "var/deploy/h7b/shared/state"
    with sqlite3.connect("file:" + str(base / "tasks.sqlite") + "?mode=ro", uri=True) as db:
        count = db.execute(
            "SELECT count(*) FROM skill_jobs WHERE state IN ('queued','running','cancel_requested')"
        ).fetchone()[0]
        if count:
            raise RuntimeError("Wait for active background jobs before switching")
    with sqlite3.connect("file:" + str(base / "bindings.sqlite") + "?mode=ro", uri=True) as db:
        if db.execute("SELECT count(*) FROM runtime_admissions").fetchone()[0]:
            raise RuntimeError("Wait for active conversations before switching")


def prepare():
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    if (STATE / "before.json").exists():
        raise RuntimeError("Release already prepared")
    original = Path(
        inspect("platform")["Config"]["Labels"]["com.docker.compose.project.config_files"]
    )
    document = json.loads(original.read_text())
    unit = Path(
        run(
            [
                "systemctl",
                "--user",
                "show",
                "scat-hermes-portal-v2.service",
                "--property=FragmentPath",
                "--value",
            ]
        ).strip()
    )
    save(
        STATE / "before.json",
        {
            "compose": str(original),
            "unit": str(unit),
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "images": {role: inspect(role)["Image"] for role in ROLES},
        },
    )
    save(STATE / "compose-before.json", document)
    (STATE / "portal-before.service").write_text(unit.read_text())
    overlay = STATE / "build/overlay"
    for name in FILES:
        destination = overlay / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((SOURCE / name).read_bytes())
    plugin = ROOT / "var/deploy/langfuse/plugin"
    plugin.mkdir(exist_ok=True, mode=0o700)
    lock = json.loads((SOURCE / "services/hermes/langfuse.lock.json").read_text())
    for name, expected in lock["files"].items():
        source = ROOT / ".tmp/langfuse/official" / name
        if hashlib.sha256(source.read_bytes()).hexdigest() != expected:
            raise RuntimeError("Official plugin source digest mismatch")
        (plugin / name).write_bytes(source.read_bytes())
        (plugin / name).chmod(0o600)
    sources = {
        role: json.loads(run(["docker", "image", "inspect", inspect(role)["Image"]]))[0][
            "RepoTags"
        ][0]
        for role in ROLES
    }
    for role in ROLES:
        native = role.startswith("hermes-")
        base = (
            "scat-langfuse-native-base:4.14.0"
            if role in {"hermes-main", "hermes-worker"}
            else sources[role]
        )
        text = "FROM " + base + "\nUSER root\n"
        if role == "hermes-author":
            text += "COPY wheels/ /tmp/langfuse-wheels/\nRUN /opt/conda/envs/scat-hermes/bin/python -m pip install --no-index --find-links=/tmp/langfuse-wheels langfuse==4.14.0 && /opt/conda/envs/scat-hermes/bin/python -m pip check\nRUN rm -rf /tmp/langfuse-wheels\n"
        text += "COPY --chown=1000:1000 overlay/ " + str(ROOT) + "/\nUSER 1000:1000\n"
        (STATE / ("build/" + role + ".Dockerfile")).write_text(text)
        service = document["services"][role]
        service["image"] = "scat-langfuse-" + role + ":20261126"
        service.setdefault("environment", {}).update(
            {
                "SCAT_LANGFUSE_CONFIG": "var/deploy/langfuse/client.json",
                "SCAT_DIAGNOSTICS_STATE_FILE": "var/deploy/h7b/shared/state/diagnostics.sqlite",
            }
        )
        for name in ["client.json", *(["plugin"] if native else [])]:
            path = str(ROOT / "var/deploy/langfuse" / name)
            service.setdefault("volumes", []).append(
                {
                    "type": "bind",
                    "source": path,
                    "target": path,
                    "read_only": True,
                    "bind": {"create_host_path": False},
                }
            )
    # Keep the currently deployed RAG switch and model/data mounts verbatim.
    assert document["services"]["platform"]["environment"]["SCAT_RAG_BASELINE_ENABLED"] == "false"
    save(STATE / "compose-after.json", document)
    wheels = STATE / "build/wheels"
    shutil.copytree(ROOT / ".tmp/langfuse/wheels", wheels, dirs_exist_ok=True)
    print("Prepared role images and rollback configuration")


def build():
    for role in ROLES:
        print("Building " + role, flush=True)
        subprocess.run(
            [
                "docker",
                "build",
                "--pull=false",
                "--network=none",
                "-f",
                str(STATE / ("build/" + role + ".Dockerfile")),
                "-t",
                "scat-langfuse-" + role + ":20261126",
                str(STATE / "build"),
            ],
            check=True,
            cwd=ROOT,
        )


def stage_portal():
    archive = ROOT / ".tmp/langfuse/frontend.zip"
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    folder = ROOT / "var/deploy/portal/releases" / ("20261126-identity-langfuse-" + digest[:12])
    folder.mkdir(exist_ok=True)
    files = {}
    with zipfile.ZipFile(archive) as z:
        if z.testzip() is not None:
            raise ValueError("Frontend archive failed integrity check")
        for name in z.namelist():
            if Path(name).is_absolute() or ".." in Path(name).parts:
                raise ValueError("Invalid asset path")
            body = z.read(name)
            target = folder / "dist" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
            files[name] = hashlib.sha256(body).hexdigest()
    save(folder / "manifest.json", {"zip_sha256": digest, "files": files})
    return folder


def portal(folder):
    original = (STATE / "portal-before.service").read_text()
    text, count = re.subn(
        r"--release\s+\S+", "--release " + folder.relative_to(ROOT).as_posix(), original
    )
    if count != 1:
        raise RuntimeError("Unexpected portal service command")
    before = json.loads((STATE / "before.json").read_text())
    Path(before["unit"]).write_text(text)
    run(["systemctl", "--user", "daemon-reload"])
    run(["systemctl", "--user", "restart", "scat-hermes-portal-v2.service"])
    return folder.name


def apply():
    idle()
    # Validate and stage the complete archive before replacing any business service.
    folder = stage_portal()
    before = json.loads((STATE / "before.json").read_text())
    try:
        run(
            [
                "docker",
                "compose",
                "-f",
                str(STATE / "compose-after.json"),
                "up",
                "-d",
                "--no-deps",
                *ROLES,
            ],
            timeout=240,
        )
        for _ in range(60):
            if all(
                inspect(role)["State"].get("Health", {}).get("Status") == "healthy"
                for role in ROLES
            ):
                break
            time.sleep(2)
        else:
            raise RuntimeError("Role health check failed")
        release = portal(folder)
        if Path("/proc/sys/kernel/random/boot_id").read_text().strip() != before["boot_id"]:
            raise RuntimeError("Unexpected host restart")
        save(
            STATE / "result.json",
            {
                "status": "deployed",
                "portal_release": release,
                "images": {role: inspect(role)["Image"] for role in ROLES},
                "host_restarted": False,
            },
        )
        print(
            json.dumps({"status": "deployed", "portal_release": release, "host_restarted": False})
        )
    except Exception:
        import traceback

        traceback.print_exc()
        rollback()
        raise


def rollback():
    before = json.loads((STATE / "before.json").read_text())
    run(
        ["docker", "compose", "-f", before["compose"], "up", "-d", "--no-deps", *ROLES], timeout=240
    )
    Path(before["unit"]).write_text((STATE / "portal-before.service").read_text())
    run(["systemctl", "--user", "daemon-reload"])
    run(["systemctl", "--user", "restart", "scat-hermes-portal-v2.service"])
    print("Restored previous business and portal release")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "build", "apply", "rollback"])
    command = parser.parse_args().action
    {"prepare": prepare, "build": build, "apply": apply, "rollback": rollback}[command]()
