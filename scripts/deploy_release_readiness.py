"""Stage an offline, pinned identity/business release; apply only after explicit readiness gates."""

import argparse
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "var/deploy/autonomy"
OUT = ROOT / "var/validation/20261108-release-readiness"
sys.path.insert(0, str(ROOT / "src"))

FILES = """src/core/settings.py
src/service/auth.py
src/service/identity.py
src/service/hermes_api.py
src/service/hermes_app.py
src/service/ownership.py
src/service/skill_authoring_api.py
src/service/model_task_api.py
src/hermes_runtime/registry.py
src/hermes_runtime/skill_budget.py
src/hermes_runtime/portal_identity.py
src/hermes_runtime/validation_campaign.py
src/application/model_output_view.py
src/application/readonly_extensions.py
src/application/readonly_display.py
src/application/skill_jobs.py
src/scat/services/materialization_service.py
src/agents/analysis_results.py
src/workflow_runtime/windows.py
src/workflow_runtime/forecast.py
src/workflow_runtime/gpu.py
src/workflow_runtime/catalog.py
services/hermes/skills.lock.json
services/hermes/legacy-tool-mapping.v1.json
skills/scat-model-comparison/SKILL.md
skills/scat-model-analysis/SKILL.md
skills/scat-trajectory-analysis/SKILL.md""".splitlines()


def run(args, timeout=60):
    return subprocess.check_output(
        args, cwd=ROOT, text=True, stderr=subprocess.STDOUT, timeout=timeout
    )


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, body):
    path.write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n")


def metadata(name):
    template = '{"id":{{json .Id}},"image":{{json .Image}},"health":{{json .State.Health.Status}}}'
    return json.loads(
        run(["docker", "inspect", "--format", template, "scat-hermes-h7b-" + name + "-1"])
    )


def compose(path):
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


def bind(service, relative, readonly):
    path = ROOT / relative
    assert path.exists()
    service["volumes"] = [v for v in service["volumes"] if v["target"] != str(path)]
    service["volumes"].append(
        {
            "type": "bind",
            "source": str(path),
            "target": str(path),
            "read_only": readonly,
            "bind": {"create_host_path": False},
        }
    )


def skill_store():
    from hermes_runtime.release_store import ReleaseStore

    return ReleaseStore(ROOT, "var/deploy/h7b/shared/skills/catalog.sqlite")


def plan_file():
    selector = OUT / "active-plan"
    name = selector.read_text().strip() if selector.exists() else "deployment-plan.json"
    if name not in ["deployment-plan.json", "deployment-plan-r2.json"]:
        raise ValueError("unknown_deployment_plan")
    return OUT / name


def stage(suffix=""):
    plan_path = OUT / ("deployment-plan" + suffix + ".json")
    assert not plan_path.exists(), "plan_already_frozen"
    previous = (DEPLOY / "active-release").read_text().strip()
    old = DEPLOY / "releases" / previous
    original = json.loads((old / "compose.yaml").read_text())
    current = {n: metadata(n) for n in original["services"]}
    assert all(
        d["health"] == "healthy" and d["image"] == original["services"][n]["image"]
        for n, d in current.items()
    )
    roles = {
        n: "native-author"
        if n == "hermes-author"
        else "native"
        if n.startswith("hermes-")
        else "author"
        if n == "author-api"
        else "business"
        for n in current
    }
    bases = {
        "business": current["platform"]["image"],
        "native": current["hermes-main"]["image"],
        "author": current["author-api"]["image"],
        "native-author": current["hermes-author"]["image"],
    }
    assert all(current[n]["image"] == bases[roles[n]] for n in current), "base_role_mismatch"
    payloads = {p: (ROOT / p).read_bytes() for p in FILES}
    import deploy_forecast_contract as utility

    path = "scripts/run_hermes_api.py"
    live = run(["docker", "exec", "scat-hermes-h7b-hermes-main-1", "cat", str(ROOT / path)])
    payloads[path] = utility.replace_function(
        live, (ROOT / path).read_text(), "profile_config"
    ).encode()
    release_store = skill_store()
    active = release_store.current()
    candidate = json.loads((ROOT / "services/hermes/skills.lock.json").read_text())
    files = {name: data for name, data in active.files.items()}
    for name in ["scat-model-comparison", "scat-model-analysis", "scat-trajectory-analysis"]:
        files[name + "/SKILL.md"] = (ROOT / "skills" / name / "SKILL.md").read_bytes()
    from hermes_runtime.release_store import canonical

    skill_sha = hashlib.sha256(canonical(candidate).encode()).hexdigest()
    # Rebuild the complete checked bundle privately before touching the active pointer.
    skill = release_store.install(candidate, files)
    assert skill.bundle_sha256 == skill_sha
    # Baseline loading also verifies every skill body baked into every image.
    # The previous images' root skill trees can predate the active immutable bundle.
    for relative, data in skill.files.items():
        payloads["skills/" + relative] = data
    with release_store.connect() as db:
        old_active = dict(db.execute("SELECT sha,generation FROM active WHERE id=1").fetchone())
    hashes = {p: hashlib.sha256(data).hexdigest() for p, data in payloads.items()}
    identity = hashlib.sha256(
        json.dumps([previous, hashes, skill_sha], sort_keys=True).encode()
    ).hexdigest()[:12]
    release = "20261108-readiness-" + identity
    folder = DEPLOY / "releases" / release
    folder.mkdir(exist_ok=False)
    folder.chmod(0o700)
    shutil.copyfile(old / "compose.yaml", folder / "rollback-compose.json")
    for p, data in payloads.items():
        dest = folder / "patch" / p
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    images = {}
    for role, base in bases.items():
        assert shutil.disk_usage(ROOT).free > 10 * 1024**3
        tag = "scat-readiness-base-" + role + ":" + identity
        run(["docker", "image", "tag", base, tag])
        dockerfile = folder / ("Dockerfile." + role)
        dockerfile.write_text(
            "FROM "
            + tag
            + "\n"
            + "".join(
                "COPY --chown=1000:1000 patch/" + p + " " + str(ROOT / p) + "\n" for p in payloads
            )
        )
        target = "scat-readiness-" + role + ":" + identity
        with (OUT / ("build-" + role + suffix + ".log")).open("w") as log:
            subprocess.run(
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
                cwd=ROOT,
                stdout=log,
                stderr=subprocess.STDOUT,
                check=True,
                timeout=300,
            )
        images[role] = {
            "image_id": run(["docker", "image", "inspect", target, "--format", "{{.Id}}"]).strip(),
            "tag": target,
            "base_image_id": base,
        }
        code = "from pathlib import Path;from hermes_runtime.skills import SkillRelease;r=SkillRelease(Path.cwd(),Path.cwd()/'services/hermes/skills.lock.json');print(r.bundle_sha256)"
        checked = run(
            [
                "docker",
                "run",
                "--rm",
                "--network=none",
                "--entrypoint",
                "/usr/local/bin/scat-python",
                target,
                "-B",
                "-c",
                code,
            ]
        )
        assert checked.strip() == skill_sha, "baked_skill_baseline_mismatch"
    directory = ROOT / "var/deploy/portal"
    keys = directory / "keys"
    keys.mkdir(exist_ok=True)
    keys.chmod(0o700)
    for name in ["signing", "front"]:
        path = keys / (name + ".key")
        if not path.exists():
            with path.open("x") as stream:
                stream.write(secrets.token_urlsafe(48))
            path.chmod(0o600)
        assert not path.is_symlink() and path.stat().st_mode & 0o077 == 0
    identity_file = directory / "identities.json"
    if not identity_file.exists():
        shutil.copyfile(ROOT / "config/portal-identities.example.json", identity_file)
    assert json.loads(identity_file.read_text()) == json.loads(
        (ROOT / "config/portal-identities.example.json").read_text()
    )
    identity_file.chmod(0o600)
    for relative in ["var/autonomy/validation", "var/deploy/validation-campaign"]:
        (ROOT / relative).mkdir(exist_ok=True)
        (ROOT / relative).chmod(0o700)
    selector = ROOT / "var/deploy/validation-campaign/active.json"
    if not selector.exists():
        save(selector, {"schema_version": 1, "status": "retired"})
    candidate_compose = json.loads(json.dumps(original))
    for n, service in candidate_compose["services"].items():
        service["image"] = images[roles[n]]["image_id"]
        service["environment"]["SCAT_VALIDATION_CAMPAIGN_FILE"] = (
            "var/deploy/validation-campaign/active.json"
        )
        bind(service, "var/autonomy/validation", False)
        bind(service, "var/deploy/validation-campaign", True)
        if n in ["platform", "author-api"]:
            service["environment"].update(
                PORTAL_IDENTITIES_FILE="var/deploy/portal/identities.json",
                PORTAL_SIGNING_KEY_FILE="var/deploy/portal/keys/signing.key",
            )
            bind(service, "var/deploy/portal/identities.json", True)
            bind(service, "var/deploy/portal/keys/signing.key", True)
        if n == "platform":
            service["environment"].update(
                ANALYSIS_IDENTITY_MODE="trusted_proxy",
                HERMES_MAX_ACTIVE_PER_USER="1",
                HERMES_MAX_ACTIVE_TOTAL="3",
            )
    save(folder / "compose.yaml", candidate_compose)
    run(compose(folder / "compose.yaml") + ["config", "--quiet"])
    sources = json.loads((old / "source.lock.json").read_text())
    sources.update(hashes)
    save(folder / "source.lock.json", sources)
    save(folder / "images.lock.json", images)
    if (old / "features.json").exists():
        shutil.copyfile(old / "features.json", folder / "features.json")
    plan = {
        "previous": previous,
        "release": release,
        "services": list(current),
        "before": current,
        "images": images,
        "source_hashes": hashes,
        "old_skill": old_active,
        "new_skill_sha256": skill_sha,
        "boot_sha256": sha(Path("/proc/sys/kernel/random/boot_id")),
        "production_gpu_sha256": sha(ROOT / "var/gpu/production.json"),
        "applied": False,
    }
    save(plan_path, plan)
    (OUT / "active-plan").write_text(plan_path.name)
    print(
        json.dumps(
            {
                "status": "staged_only",
                "release": release,
                "files": len(hashes),
                "services": list(current),
                "skill_sha256": skill_sha,
            }
        )
    )


if __name__ == "__main__":
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["stage", "stage-r2"])
    args = parser.parse_args()
    stage("-r2" if args.action == "stage-r2" else "")
