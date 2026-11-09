"""Create only this deployment's private credentials, profiles and persistent directories."""

import json
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/deploy/h7b"


def private(path, value):
    if not path.exists():
        with path.open("x") as stream:
            path.chmod(0o600)
            stream.write(value)
    elif path.is_symlink() or path.stat().st_mode & 0o077:
        raise ValueError("unsafe_existing_private_file")


def main():
    if sys.executable != "/home/h3c/anaconda3/envs/scat-agent/bin/python":
        raise RuntimeError("existing APP Conda required")
    os.umask(0o077)
    sys.path.insert(0, str(ROOT / "src"))
    from dotenv import dotenv_values

    for relative in [
        "secrets",
        "shared/state",
        "shared/homes/main",
        "shared/homes/jobs",
        "shared/homes/author",
        "shared/skills",
        "runtime-validation",
        "workflow-inputs",
        "rag-operations",
    ]:
        (OUT / relative).mkdir(parents=True, exist_ok=True)
    for key in [
        "hermes",
        "rpc-main",
        "rpc-worker",
        "rpc-platform",
        "job-control",
        "author",
        "release",
        "platform-auth",
    ]:
        private(OUT / "secrets" / (key + ".key"), secrets.token_urlsafe(48))
    values = dotenv_values(ROOT / ".env")
    backend = {
        key: value
        for key, value in values.items()
        if key
        in {
            "FLIGHT_MONGO_URI",
            "FLIGHT_MONGO_DB",
            "SCAT_FLIGHTS_COLLECTION",
            "SCAT_AIRSPACE_COLLECTION",
            "UAV_FLIGHTS_COLLECTION",
        }
        and value is not None
    }
    backend.update(
        USE_FAKE_MODEL="true",
        ANALYSIS_IDENTITY_MODE="single_user",
        ANALYSIS_SINGLE_USER_ID="scat-researcher",
        ANALYSIS_RUNS_COLLECTION="analysis_runs_hermes_h7b",
        ANALYSIS_ARTIFACTS_COLLECTION="analysis_artifacts_hermes_h7b",
    )

    def render(mapping):
        return (
            "\n".join(
                key + "=" + json.dumps(value, ensure_ascii=False) for key, value in mapping.items()
            )
            + "\n"
        )

    private(OUT / "secrets/backend.env", render(backend))
    private(
        OUT / "secrets/platform.env",
        render({"AUTH_SECRET": (OUT / "secrets/platform-auth.key").read_text().strip()}),
    )
    provider = values.get("DEEPSEEK_API_KEY")
    if not provider:
        raise ValueError("existing_provider_key_required")
    private(OUT / "secrets/author.env", render({**backend, "DEEPSEEK_API_KEY": provider}))
    for name in [
        "actions",
        "platform",
        "hermes-main",
        "worker",
        "hermes-worker",
        "author-api",
        "hermes-author",
    ]:
        (OUT / "runtime-tmp" / name).mkdir(parents=True, exist_ok=True)
    for name, role in [
        ("hermes-main", "scat-main"),
        ("hermes-worker", "scat-worker"),
        ("hermes-author", "scat-author"),
        ("author-api", "scat-author"),
    ]:
        relative = "var/deploy/h7b/profile-roots/" + name
        code = (
            "import sys;sys.path.insert(0,'src');from hermes_runtime.native_profiles import initialize;initialize(__import__('pathlib').Path.cwd(),base="
            + repr(relative)
            + ",reuse_key=False)"
        )
        subprocess.run(
            ["/home/h3c/anaconda3/envs/scat-hermes/bin/python", "-B", "-c", code],
            cwd=ROOT,
            check=True,
            stdout=subprocess.DEVNULL,
            env=os.environ | {"SCAT_HERMES_PROFILE_ROOT": relative},
        )
        source = ROOT / "var/hermes/managed/profiles" / role / ".env"
        destination = OUT / "profile-roots" / name / "profiles" / role / ".env"
        if not dotenv_values(destination).get("DEEPSEEK_API_KEY"):
            content = (
                source.read_text()
                if source.exists() and dotenv_values(source).get("DEEPSEEK_API_KEY")
                else "DEEPSEEK_API_KEY=" + json.dumps(provider) + "\n"
            )
            destination.write_text(content)
            destination.chmod(0o600)
    old_cache = ROOT / "var/hermes/rag-production/models_dev_cache.json"
    if old_cache.exists():
        for role in ["main", "author"]:
            target = OUT / "shared/homes" / role / old_cache.name
            if not target.exists():
                shutil.copyfile(old_cache, target)
    print(
        json.dumps(
            {
                "private_credentials_configured": True,
                "new_provider_keys": 0,
                "state_isolated_from_production": True,
                "profiles": 4,
            }
        )
    )


if __name__ == "__main__":
    main()
