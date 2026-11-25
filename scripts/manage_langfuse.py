"""Prepare and operate the private Langfuse stack without touching business services."""

import argparse
import hashlib
import json
import secrets
import subprocess
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "var/deploy/langfuse"
SOURCE = "https://raw.githubusercontent.com/langfuse/langfuse/main/docker-compose.yml"


def prepare(template=None):
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    if (STATE / "compose.json").exists():
        raise RuntimeError("Langfuse stack is already prepared")
    raw = (
        Path(template).read_bytes()
        if template
        else urllib.request.urlopen(SOURCE, timeout=30).read()
    )
    document = yaml.safe_load(raw)
    services = document["services"]
    for name in list(services):
        if name not in {
            "langfuse-web",
            "langfuse-worker",
            "postgres",
            "redis",
            "minio",
            "clickhouse",
        }:
            del services[name]
    values = {
        "NEXTAUTH_URL": "http://127.0.0.1:18160",
        "NEXTAUTH_SECRET": secrets.token_hex(32),
        "SALT": secrets.token_hex(32),
        "ENCRYPTION_KEY": secrets.token_hex(32),
        "POSTGRES_PASSWORD": secrets.token_hex(24),
        "CLICKHOUSE_PASSWORD": secrets.token_hex(24),
        "REDIS_AUTH": secrets.token_hex(24),
        "MINIO_ROOT_PASSWORD": secrets.token_hex(24),
        "LANGFUSE_INIT_ORG_ID": "scat-platform",
        "LANGFUSE_INIT_ORG_NAME": "SCAT Hermes",
        "LANGFUSE_INIT_PROJECT_ID": "scat-diagnostics",
        "LANGFUSE_INIT_PROJECT_NAME": "SCAT Diagnostics",
        "LANGFUSE_INIT_PROJECT_PUBLIC_KEY": "pk-lf-" + secrets.token_hex(16),
        "LANGFUSE_INIT_PROJECT_SECRET_KEY": "sk-lf-" + secrets.token_hex(32),
        "LANGFUSE_INIT_USER_EMAIL": "diagnostics@scat.internal",
        "LANGFUSE_INIT_USER_PASSWORD": secrets.token_urlsafe(32),
        "LANGFUSE_IMAGE_TAG": "4",
    }
    values.update(
        DATABASE_URL="postgresql://postgres:"
        + values["POSTGRES_PASSWORD"]
        + "@postgres:5432/postgres",
        TELEMETRY_ENABLED="false",
        LANGFUSE_ENABLE_EXPERIMENTAL_FEATURES="false",
        LANGFUSE_S3_EVENT_UPLOAD_SECRET_ACCESS_KEY=values["MINIO_ROOT_PASSWORD"],
        LANGFUSE_S3_MEDIA_UPLOAD_SECRET_ACCESS_KEY=values["MINIO_ROOT_PASSWORD"],
        LANGFUSE_S3_BATCH_EXPORT_SECRET_ACCESS_KEY=values["MINIO_ROOT_PASSWORD"],
    )
    for name, service in services.items():
        service.pop("ports", None)
        service.pop("container_name", None)
        service["restart"] = "unless-stopped"
        service["logging"] = {"driver": "json-file", "options": {"max-size": "4m", "max-file": "3"}}
        if name in {"langfuse-web", "langfuse-worker"}:
            image = "langfuse" if name == "langfuse-web" else name
            service["image"] = "docker.langfuse.com/langfuse/" + image + ":4"
            service["environment"].update(
                {k: v for k, v in values.items() if k != "LANGFUSE_IMAGE_TAG"}
            )
            service["environment"]["AUTH_DISABLE_SIGNUP"] = "true"
            service["depends_on"] = {
                key: entry
                for key, entry in service.get("depends_on", {}).items()
                if key in services
            }
        service["mem_limit"] = "3g" if name == "clickhouse" else "2g"
    services["langfuse-web"]["ports"] = ["127.0.0.1:18160:3000"]
    # Resolve only the vendor template's ${VAR:-default} expressions through Compose.
    environment = STATE / "stack.env"
    environment.write_text("".join(k + "=" + v + "\n" for k, v in values.items()))
    environment.chmod(0o600)
    config = STATE / "compose.template.json"
    config.write_text(
        json.dumps(
            {"name": "scat-langfuse", "services": services, "volumes": document.get("volumes", {})}
        )
    )
    config.chmod(0o600)
    resolved = subprocess.check_output(
        [
            "docker",
            "compose",
            "--env-file",
            str(environment),
            "-f",
            str(config),
            "config",
            "--format",
            "json",
        ]
    )
    target = STATE / "compose.json"
    target.write_bytes(resolved)
    target.chmod(0o600)
    client = STATE / "client.json"
    client.write_text(
        json.dumps(
            {
                "base_url": "http://127.0.0.1:18160",
                "plugin_path": "var/deploy/langfuse/plugin",
                "public_key": values["LANGFUSE_INIT_PROJECT_PUBLIC_KEY"],
                "secret_key": values["LANGFUSE_INIT_PROJECT_SECRET_KEY"],
            }
        )
    )
    client.chmod(0o600)
    (STATE / "provenance.json").write_text(
        json.dumps({"source": SOURCE, "sha256": hashlib.sha256(raw).hexdigest()})
    )
    print("Prepared private Langfuse configuration")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "pull", "up", "status"])
    parser.add_argument("--template", help="Previously downloaded official Compose template")
    args = parser.parse_args()
    if args.action == "prepare":
        prepare(args.template)
    else:
        command = {"pull": ["pull"], "up": ["up", "-d"], "status": ["ps"]}[args.action]
        subprocess.run(
            ["docker", "compose", "-f", str(STATE / "compose.json"), *command], check=True
        )
