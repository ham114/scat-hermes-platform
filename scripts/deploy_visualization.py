"""Deploy the reviewed visualization lane over the active release, with rollback.

Run on the existing host only. Credentials are provisioned separately, never packed.
"""

import argparse
import copy
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import time
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path("/home/h3c/project/python/scat-hermes-platform")
SOURCE = ROOT / ".tmp/visualization-deploy-20261128/candidate"
STATE = ROOT / "var/deploy/visualization/20261128"
UPDATED = ("platform", "actions", "hermes-main")
EXISTING = (*UPDATED, "worker", "hermes-worker", "hermes-author", "author-api")
IMAGE_PREFIX = "scat-visualization"
UNIT = "scat-visualization-sandbox.service"


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    path.chmod(0o600)


def read(path):
    return json.loads(path.read_text())


def run(args, timeout=120):
    result = subprocess.run(
        args, cwd=ROOT, capture_output=True, text=True, timeout=timeout, check=False
    )
    if result.returncode:
        STATE.mkdir(parents=True, exist_ok=True)
        log = STATE / "last-command-error.log"
        log.write_text((result.stdout + result.stderr)[-30000:])
        log.chmod(0o600)
        raise RuntimeError("deployment_command_failed; inspect private release log")
    return result.stdout


def inspect(role):
    return json.loads(run(["docker", "inspect", "scat-hermes-h7b-" + role + "-1"]))[0]


def compose_candidate(before, images, image, root=ROOT):
    """Preserve active business configuration; credentials belong only to worker."""
    root = PurePosixPath(root.as_posix())
    document = copy.deepcopy(before)
    shared = "var/deploy/h7b/shared/visualizations"
    common = {
        "SCAT_VIS_ENABLED": "true",
        "SCAT_VIS_ARTIFACT_ROOT": shared,
        "SCAT_VIS_SANDBOX_IMAGE": image,
        "SCAT_VIS_SANDBOX_URL": "http://127.0.0.1:18166",
        "SCAT_VIS_SANDBOX_KEY_FILE": "var/deploy/h7b/secrets/visualization-sandbox.key",
    }
    platform = document["services"]["platform"]
    policy = platform["environment"]["SCAT_AUTONOMY_CONFIG"]
    preference_mounts = [
        v
        for v in platform["volumes"]
        if "/var/deploy/autonomy/" in v["source"] or "/var/autonomy/memory" in v["source"]
    ]
    for role in UPDATED:
        service = document["services"][role]
        service["image"] = images[role]
        service.setdefault("environment", {}).update(common)
        # The active RAG release bind-mounts its old role entrypoint. Use the new image copy.
        service["volumes"] = [
            v
            for v in service["volumes"]
            if v["target"] != str(root / "scripts/run_container_service.py")
        ]
        if role == "actions":
            service["environment"]["SCAT_AUTONOMY_CONFIG"] = policy
            targets = {v["target"] for v in service["volumes"]}
            service["volumes"].extend(
                copy.deepcopy(v) for v in preference_mounts if v["target"] not in targets
            )
    template = read(SOURCE / "infra/compose/compose.visualization.yaml")["services"][
        "visualization-worker"
    ]
    worker = copy.deepcopy(template)
    worker.pop("profiles", None)
    worker["image"] = images["platform"]
    worker["environment"].update(common | {"SCAT_AUTONOMY_CONFIG": policy})
    worker["volumes"].extend(copy.deepcopy(preference_mounts))
    document["services"]["visualization-worker"] = worker
    assert worker["env_file"] == [str(root / "var/deploy/h7b/secrets/visualization-worker.env")]
    for role in UPDATED:
        assert not any(
            "visualization-worker.env" in str(p)
            for p in document["services"][role].get("env_file", [])
        )
        assert not any(
            k.endswith("API_KEY") and k.startswith("SCAT_VIS_")
            for k in document["services"][role]["environment"]
        )
    return document


def idle():
    for name, query in (
        (
            "tasks.sqlite",
            "SELECT count(*) FROM skill_jobs WHERE state IN ('queued','running','cancel_requested')",
        ),
        ("bindings.sqlite", "SELECT count(*) FROM runtime_admissions"),
    ):
        with sqlite3.connect(
            (ROOT / "var/deploy/h7b/shared/state" / name).as_uri() + "?mode=ro", uri=True
        ) as db:
            if db.execute(query).fetchone()[0]:
                raise RuntimeError("active_business_work; retry after it finishes")


def prepare():
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    if (STATE / "before.json").exists():
        raise RuntimeError("release_already_prepared")
    manifest = read(SOURCE / "source-manifest.json")
    for name, sha in manifest["files"].items():
        path = (SOURCE / name).resolve()
        if (
            not path.is_relative_to(SOURCE.resolve())
            or hashlib.sha256(path.read_bytes()).hexdigest() != sha
        ):
            raise RuntimeError("candidate_source_digest_mismatch")
    lock = read(SOURCE / "services/visualization-renderer/runtime.lock.json")
    image = lock["validated_renderer_image_id"]
    run(["docker", "image", "inspect", image])
    original = Path(
        inspect("platform")["Config"]["Labels"]["com.docker.compose.project.config_files"]
    )
    before = read(original)
    portal_unit = Path(
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
            "portal_unit": str(portal_unit),
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "images": {role: inspect(role)["Image"] for role in EXISTING},
            "candidate": manifest["candidate"],
        },
    )
    save(STATE / "compose-before.json", before)
    (STATE / "portal-before.service").write_text(portal_unit.read_text())
    files = {}
    for name in manifest["files"]:
        if name.startswith(("src/", "scripts/", "services/visualization-renderer/")):
            path = ROOT / name
            files[name] = path.is_file()
            if path.is_file():
                target = STATE / "host-before" / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
    save(STATE / "host-files-before.json", files)
    overlay = STATE / "build/overlay"
    for name in files:
        target = overlay / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SOURCE / name, target)
    images = {role: IMAGE_PREFIX + "-" + role + ":20261128" for role in UPDATED}
    for role in UPDATED:
        (STATE / "build" / (role + ".Dockerfile")).write_text(
            "FROM "
            + read(STATE / "before.json")["images"][role]
            + "\nUSER root\nCOPY --chown=1000:1000 overlay/ "
            + str(ROOT)
            + "/\nUSER 1000:1000\n"
        )
    save(STATE / "compose-after.json", compose_candidate(before, images, image))
    (ROOT / "var/deploy/h7b/runtime-tmp/visualization-worker").mkdir(parents=True, exist_ok=True)
    (ROOT / "var/deploy/h7b/shared/visualizations").mkdir(parents=True, exist_ok=True)
    # Install no new service yet; archive validation precedes any business switch.
    with zipfile.ZipFile(SOURCE / "frontend.zip") as archive:
        if archive.testzip() is not None:
            raise RuntimeError("frontend_archive_invalid")
        files = {}
        sha = hashlib.sha256((SOURCE / "frontend.zip").read_bytes()).hexdigest()
        release = ROOT / "var/deploy/portal/releases" / ("20261128-visualization-" + sha[:12])
        for name in archive.namelist():
            target = (release / "dist" / name).resolve()
            if not target.is_relative_to((release / "dist").resolve()):
                raise RuntimeError("frontend_archive_path_invalid")
            target.parent.mkdir(parents=True, exist_ok=True)
            body = archive.read(name)
            target.write_bytes(body)
            files[name] = hashlib.sha256(body).hexdigest()
    save(release / "manifest.json", {"zip_sha256": sha, "files": files})
    save(STATE / "frontend.json", {"release": str(release), "sha256": sha})
    print("Prepared candidate, role images, portal assets and rollback metadata", flush=True)


def build():
    for role in UPDATED:
        print("Building " + role, flush=True)
        # BuildKit interprets a bare sha256:ID as a repository named 'sha256'.
        # Pin a local tag to the inspected image ID before resolving FROM.
        base = IMAGE_PREFIX + "-base-" + role + ":20261128"
        run(["docker", "tag", read(STATE / "before.json")["images"][role], base])
        (STATE / "build" / (role + ".Dockerfile")).write_text(
            "FROM "
            + base
            + "\nUSER root\nCOPY --chown=1000:1000 overlay/ "
            + str(ROOT)
            + "/\nUSER 1000:1000\n"
        )
        run(
            [
                "docker",
                "build",
                "--pull=false",
                "--network=none",
                "-f",
                str(STATE / "build" / (role + ".Dockerfile")),
                "-t",
                IMAGE_PREFIX + "-" + role + ":20261128",
                str(STATE / "build"),
            ],
            timeout=240,
        )
        run(
            [
                "docker",
                "run",
                "--rm",
                "--network=none",
                "--entrypoint",
                "/usr/local/bin/scat-python",
                IMAGE_PREFIX + "-" + role + ":20261128",
                "-B",
                "-c",
                "from visualization_runtime.worker import VisualizationWorker; from service.visualization_api import router; from hermes_runtime.visualization_bridge import VisualizationBridge; print('imports_ok')",
            ]
        )
    run(["docker", "compose", "-f", str(STATE / "compose-after.json"), "config", "--quiet"])
    print("Built and import-checked candidate images; compose validated", flush=True)


def quiesced_backup():
    destinations = STATE / "state-before"
    destinations.mkdir(exist_ok=False, mode=0o700)
    paths = list((ROOT / "var/deploy/h7b/shared/state").glob("*.sqlite"))
    paths += list((ROOT / "var/deploy/h7b/shared/homes/main").glob("*.sqlite"))
    paths += list((ROOT / "var/autonomy/memory").glob("*.sqlite"))
    files = {}
    for number, source in enumerate(paths):
        target = destinations / (str(number) + "-" + source.name)
        with (
            sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as db,
            sqlite3.connect(target) as out,
        ):
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("database_integrity_failed")
            db.backup(out)
        target.chmod(0o600)
        files[source.relative_to(ROOT).as_posix()] = {
            "file": target.name,
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        }
    save(destinations / "manifest.json", files)


def rollback():
    before = read(STATE / "before.json")
    subprocess.run(
        ["docker", "stop", "scat-hermes-h7b-visualization-worker-1"],
        capture_output=True,
        check=False,
    )
    subprocess.run(
        ["systemctl", "--user", "disable", "--now", UNIT], capture_output=True, check=False
    )
    for name, existed in read(STATE / "host-files-before.json").items():
        target = ROOT / name
        if existed:
            shutil.copy2(STATE / "host-before" / name, target)
        else:
            target.unlink(missing_ok=True)
    Path(before["portal_unit"]).write_text((STATE / "portal-before.service").read_text())
    run(["systemctl", "--user", "daemon-reload"])
    run(
        ["docker", "compose", "-f", before["compose"], "up", "-d", "--no-deps", *EXISTING],
        timeout=240,
    )
    run(["systemctl", "--user", "restart", "scat-hermes-portal-v2.service"])
    # Preserve new budget charges and history. Never restore old accounting automatically.
    save(
        STATE / "result.json", {"status": "rolled_back", "state_and_budget_charges_preserved": True}
    )
    print(
        "Restored prior code/configuration; retained all current state and budget charges",
        flush=True,
    )


def apply():
    idle()
    if (STATE / "state-before").exists():
        raise RuntimeError("activation_already_attempted; inspect_or_rollback")
    try:
        run(
            ["docker", "compose", "-f", str(STATE / "compose-before.json"), "stop", *EXISTING],
            timeout=120,
        )
        quiesced_backup()
        for name in read(STATE / "host-files-before.json"):
            target = ROOT / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(SOURCE / name, target)
        unit = Path.home() / ".config/systemd/user" / UNIT
        unit.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SOURCE / "infra/systemd/user" / UNIT, unit)
        run(["systemctl", "--user", "daemon-reload"])
        run(["systemctl", "--user", "enable", "--now", UNIT])
        run(
            [
                "docker",
                "compose",
                "-f",
                str(STATE / "compose-after.json"),
                "up",
                "-d",
                "--no-deps",
                *EXISTING,
                "visualization-worker",
            ],
            timeout=240,
        )
        for _ in range(60):
            if all(
                inspect(role)["State"].get("Health", {}).get("Status") == "healthy"
                for role in (*EXISTING, "visualization-worker")
            ):
                break
            time.sleep(2)
        else:
            raise RuntimeError("candidate_health_failed")
        before = read(STATE / "before.json")
        text, count = re.subn(
            r"--release\s+\S+",
            "--release "
            + Path(read(STATE / "frontend.json")["release"]).relative_to(ROOT).as_posix(),
            (STATE / "portal-before.service").read_text(),
        )
        if count != 1:
            raise RuntimeError("portal_unit_unexpected")
        Path(before["portal_unit"]).write_text(text)
        run(["systemctl", "--user", "daemon-reload"])
        run(["systemctl", "--user", "restart", "scat-hermes-portal-v2.service"])
        if Path("/proc/sys/kernel/random/boot_id").read_text().strip() != before["boot_id"]:
            raise RuntimeError("host_restarted")
        save(
            STATE / "result.json",
            {
                "status": "deployed",
                "candidate": before["candidate"],
                "images": {
                    role: inspect(role)["Image"] for role in (*UPDATED, "visualization-worker")
                },
                "frontend": read(STATE / "frontend.json"),
                "production_visualization_enabled": True,
                "host_restarted": False,
                "signed_in_native_smoke": "pending",
            },
        )
        print(
            "Deployed; all eight role containers healthy. Native/user smoke still required.",
            flush=True,
        )
    except Exception:
        rollback()
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "build", "apply", "rollback", "status"])
    action = parser.parse_args().action
    {
        "prepare": prepare,
        "build": build,
        "apply": apply,
        "rollback": rollback,
        "status": lambda: print(json.dumps(read(STATE / "result.json"), ensure_ascii=False)),
    }[action]()
