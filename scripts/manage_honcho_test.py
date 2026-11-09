"""Isolated Honcho candidate services; owns no existing platform containers."""

import argparse
import base64
import hashlib
import hmac
import json
import os
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "var/deploy/honcho"
LOGS = ROOT / "var/validation/20261027-honcho"
OWNER = "scat-honcho-test"
ENV = "/home/h3c/anaconda3/envs/"


def run(*args, timeout=120):
    return subprocess.run(
        list(args), cwd=ROOT, capture_output=True, text=True, check=True, timeout=timeout
    ).stdout


def private(path, value):
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        if path.is_symlink() or path.stat().st_mode & 0o077:
            raise ValueError("unsafe_existing_private_file")
    else:
        with os.fdopen(fd, "w") as stream:
            stream.write(value)
    return path.read_text().strip()


def jwt_token(secret, claims):
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).rstrip(
            b"="
        )

    body = (
        encode({"alg": "HS256", "typ": "JWT"})
        + b"."
        + encode({"t": "", "exp": int(time.time() + 86400), **claims})
    )
    return (
        body
        + b"."
        + base64.urlsafe_b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).rstrip(
            b"="
        )
    ).decode()


def birth(pid):
    try:
        value = Path(f"/proc/{pid}/stat").read_text()
        return value[value.rfind(")") + 2 :].split()[19]
    except (OSError, IndexError):
        return None


def main():
    global DEPLOY, LOGS, OWNER
    if sys.executable != ENV + "scat-agent/bin/python":
        raise ValueError("existing_APP_Conda_required")
    import httpx
    from dotenv import dotenv_values

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "stop", "status"])
    parser.add_argument(
        "--production",
        action="store_true",
        help="Separate persistent production memory data and services",
    )
    args = parser.parse_args()
    candidate = ROOT / "var/deploy/honcho"
    if args.production:
        DEPLOY, LOGS, OWNER = (
            ROOT / "var/deploy/honcho-production",
            ROOT / "var/validation/20261028-autonomy",
            "scat-honcho-production",
        )
    os.umask(0o077)
    LOGS.mkdir(parents=True, exist_ok=True)
    for name in ["runtime", "testing", "database", "database/tmp", "secrets"]:
        (DEPLOY / name).mkdir(parents=True, exist_ok=True)
    processes_file = DEPLOY / "testing/processes.json"
    processes = json.loads(processes_file.read_text()) if processes_file.exists() else {}
    if args.action == "stop":
        ids = run("docker", "ps", "-aq", "--filter", "label=scat.owner=" + OWNER).splitlines()
        for identity in ids:
            run("docker", "stop", "--time", "25", identity, timeout=60)
        for row in processes.values():
            if birth(row["pid"]) == row["birth"]:
                os.killpg(row["pid"], signal.SIGTERM)
        if args.production:
            run(
                "systemctl",
                "--user",
                "stop",
                "scat-memory-model-proxy.service",
                "scat-memory-embedding.service",
            )
        print(
            json.dumps(
                {
                    "owned_containers_stopped": len(ids),
                    "owned_host_processes_signaled": list(processes),
                    "existing_services_touched": False,
                }
            )
        )
        return
    if args.action == "status":
        print(
            run(
                "docker",
                "ps",
                "-a",
                "--filter",
                "label=scat.owner=" + OWNER,
                "--format",
                "{{.Names}} {{.Status}}",
            )
        )
        print(
            json.dumps({name: birth(row["pid"]) == row["birth"] for name, row in processes.items()})
        )
        return
    for port in [18140, 18141, 18142]:
        import socket

        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                raise ValueError("refuse_existing_listener_" + str(port))
    for name in [OWNER + "-db", OWNER + "-api", OWNER + "-deriver"]:
        previous = subprocess.run(
            ["docker", "inspect", name], capture_output=True, text=True, check=False
        )
        if previous.returncode == 0:
            old = json.loads(previous.stdout)[0]
            if old["Config"]["Labels"].get("scat.owner") != OWNER or old["State"]["Running"]:
                raise ValueError("refuse_replacing_active_or_unowned_candidate")
            run("docker", "rm", name)
    secret_dir = DEPLOY / "secrets"
    proxy = private(secret_dir / "proxy.key", secrets.token_urlsafe(48))
    if args.production:
        proxy = (ROOT / "var/deploy/autonomy/secrets/memory.key").read_text().strip()
    embedding = private(secret_dir / "embedding.key", secrets.token_urlsafe(48))
    database = private(secret_dir / "database.key", secrets.token_urlsafe(36))
    signing = private(secret_dir / "auth-signing.key", secrets.token_hex(32))
    private(secret_dir / "admin.key", jwt_token(signing, {"ad": True}))
    private(secret_dir / "workspace.key", jwt_token(signing, {"w": "scat-honcho-test-20261027"}))
    private(
        secret_dir / "other-workspace.key", jwt_token(signing, {"w": "scat-honcho-other-20261027"})
    )
    images = json.loads((candidate / "image.lock.json").read_text())
    image_id = images["image_id"]
    if (
        run("docker", "image", "inspect", images["image"], "--format", "{{.Id}}").strip()
        != image_id
    ):
        raise ValueError("candidate_image_identity_changed")
    net = OWNER + "-private"
    inspected = subprocess.run(
        ["docker", "network", "inspect", net], capture_output=True, text=True, check=False
    )
    if inspected.returncode:
        run("docker", "network", "create", "--internal", "--label", "scat.owner=" + OWNER, net)
    elif json.loads(inspected.stdout)[0]["Labels"].get("scat.owner") != OWNER:
        raise ValueError("refuse_unowned_network")
    db_image = json.loads(
        run("docker", "image", "inspect", "docker.1ms.run/pgvector/pgvector:pg16")
    )[0]["Id"]
    postgres_env = secret_dir / "postgres.env"
    postgres_env.write_text(
        "POSTGRES_USER=honcho\nPOSTGRES_DB=honcho\nPOSTGRES_PASSWORD_FILE="
        + str(secret_dir / "database.key")
        + "\nPGDATA="
        + str(DEPLOY / "database/data")
        + "\nTMPDIR="
        + str(DEPLOY / "database/tmp")
        + "\nPGHOST="
        + str(DEPLOY / "database/tmp")
        + "\n"
    )
    postgres_env.chmod(0o600)
    common = [
        "--label",
        "scat.owner=" + OWNER,
        "--security-opt",
        "no-new-privileges:true",
        "--cap-drop",
        "ALL",
        "--log-opt",
        "max-size=4m",
        "--log-opt",
        "max-file=3",
        "--pids-limit",
        "256",
    ]
    run(
        "docker",
        "run",
        "-d",
        "--name",
        OWNER + "-db",
        *common,
        "--network",
        net,
        "--user",
        "1000:1000",
        "--cpus",
        "1",
        "--memory",
        "2g",
        "--env-file",
        str(postgres_env),
        "--mount",
        "type=bind,source=" + str(DEPLOY / "database") + ",target=" + str(DEPLOY / "database"),
        "--mount",
        "type=bind,source=" + str(DEPLOY / "database/tmp") + ",target=/var/run/postgresql",
        "--mount",
        "type=bind,source="
        + str(secret_dir / "database.key")
        + ",target="
        + str(secret_dir / "database.key")
        + ",readonly",
        db_image,
        "postgres",
        "-c",
        "unix_socket_directories=" + str(DEPLOY / "database/tmp"),
    )
    for _ in range(80):
        ready = subprocess.run(
            [
                "docker",
                "exec",
                OWNER + "-db",
                "pg_isready",
                "-h",
                "127.0.0.1",
                "-U",
                "honcho",
                "-d",
                "honcho",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if ready.returncode == 0:
            break
        time.sleep(0.5)
    else:
        raise ValueError("candidate_database_not_ready")
    # Complete an interrupted first init without deleting the candidate data.
    exists = run(
        "docker",
        "exec",
        OWNER + "-db",
        "psql",
        "-U",
        "honcho",
        "-d",
        "postgres",
        "-tAc",
        "SELECT 1 FROM pg_database WHERE datname='honcho'",
    ).strip()
    if exists != "1":
        run(
            "docker",
            "exec",
            OWNER + "-db",
            "psql",
            "-U",
            "honcho",
            "-d",
            "postgres",
            "-c",
            "CREATE DATABASE honcho",
        )
    db_info = json.loads(run("docker", "inspect", OWNER + "-db"))[0]
    db_ip = db_info["NetworkSettings"]["Networks"][net]["IPAddress"]
    values = {
        key: value
        for key, value in dotenv_values(ROOT / "config/honcho/server.env.example").items()
        if value is not None
    }
    values.update(
        LLM_OPENAI_API_KEY=proxy,
        HONCHO_EMBEDDING_API_KEY=embedding,
        AUTH_JWT_SECRET=signing,
        DB_CONNECTION_URI="postgresql+psycopg://honcho:" + database + "@" + db_ip + ":5432/honcho",
        DERIVER_POLLING_STARTUP_JITTER_SECONDS="0",
        DERIVER_POLLING_JITTER_RATIO="0",
        DERIVER_FLUSH_ENABLED="true",
        DERIVER_REPRESENTATION_BATCH_WORK_UNIT_TARGET_TOKENS="0",
        DERIVER_REPRESENTATION_BATCH_MAX_AGE_SECONDS="1",
        DERIVER_STALE_SESSION_TIMEOUT_MINUTES="1",
        PYTHONPATH=str(candidate / "source"),
        HOME=str(DEPLOY / "runtime"),
        TMPDIR=str(DEPLOY / "runtime"),
        TMP=str(DEPLOY / "runtime"),
        TEMP=str(DEPLOY / "runtime"),
    )
    values.pop("HONCHO_API_KEY", None)
    values.pop("REASONING_TRACES_FILE", None)
    if args.production:
        values["DB_CONNECTION_URI"] = (
            "postgresql+psycopg://honcho:"
            + database
            + "@/honcho?host="
            + str(DEPLOY / "database/tmp")
        )
    for key in list(values):
        if (
            key.endswith("__OVERRIDES__BASE_URL")
            and key != "EMBEDDING_MODEL_CONFIG__OVERRIDES__BASE_URL"
        ):
            values[key] = "http://127.0.0.1:18142/v1"
        if (
            key.endswith(("__MODEL_CONFIG__MODEL", "_MODEL_CONFIG__MODEL"))
            and "EMBEDDING" not in key
        ):
            values[key] = "deepseek-flash"
    runtime_env = secret_dir / "candidate.env"
    runtime_env.write_text("\n".join(key + "=" + value for key, value in values.items()) + "\n")
    runtime_env.chmod(0o600)
    if args.production:
        units = ROOT / "var/deploy/autonomy/units"
        units.mkdir(parents=True, exist_ok=True)
        for unit, python, script, extra in [
            ("scat-memory-model-proxy", "scat-agent", "run_autonomy_proxy.py", ""),
            (
                "scat-memory-embedding",
                "scat-rag-train",
                "serve_honcho_embeddings.py",
                "Environment=SCAT_EMBEDDING_SECRET_FILE=var/deploy/honcho-production/secrets/embedding.key\nEnvironment=SCAT_EMBEDDING_TEMP=var/autonomy/embedding-runtime\n",
            ),
        ]:
            path = units / (unit + ".service")
            path.write_text(
                "[Unit]\nDescription=SCAT persistent memory dependency\nAfter=network-online.target\n[Service]\nType=simple\nWorkingDirectory="
                + str(ROOT)
                + "\nEnvironment=PYTHONDONTWRITEBYTECODE=1\n"
                + extra
                + "ExecStart="
                + ENV
                + python
                + "/bin/python -B scripts/"
                + script
                + "\nRestart=on-failure\nRestartSec=10\nUMask=0077\nStandardOutput=append:"
                + str(LOGS / (unit + ".log"))
                + "\nStandardError=inherit\n[Install]\nWantedBy=default.target\n"
            )
            run("systemctl", "--user", "link", str(path))
            run("systemctl", "--user", "daemon-reload")
            run("systemctl", "--user", "enable", "--now", unit + ".service", timeout=90)
    for name, python, script in (
        []
        if args.production
        else [
            ("proxy", "scat-agent", "run_honcho_test_proxy.py"),
            ("embedding", "scat-rag-train", "serve_honcho_embeddings.py"),
        ]
    ):
        with (LOGS / (name + ".log")).open("a") as stream:
            process = subprocess.Popen(
                [ENV + python + "/bin/python", "-B", "scripts/" + script],
                cwd=ROOT,
                env=os.environ | {"PYTHONDONTWRITEBYTECODE": "1"},
                stdout=stream,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        processes[name] = {"pid": process.pid, "birth": birth(process.pid)}
        processes_file.write_text(json.dumps(processes, indent=2) + "\n")
    with httpx.Client(trust_env=False, timeout=2) as http:
        for port in [18141, 18142]:
            for _ in range(120):
                try:
                    if http.get(f"http://127.0.0.1:{port}/health").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.5)
            else:
                raise ValueError("candidate_dependency_not_ready_" + str(port))
    api_common = [
        *common,
        "--network",
        "host",
        "--read-only",
        "--cpus",
        "2",
        "--memory",
        "3g",
        "--env-file",
        str(runtime_env),
        "--mount",
        "type=bind,source=" + str(DEPLOY / "runtime") + ",target=" + str(DEPLOY / "runtime"),
        "--mount",
        "type=bind,source="
        + str(candidate / "source/scripts")
        + ",target="
        + str(candidate / "source/scripts")
        + ",readonly",
    ]
    if args.production:
        api_common += [
            "--mount",
            "type=bind,source="
            + str(DEPLOY / "database/tmp")
            + ",target="
            + str(DEPLOY / "database/tmp")
            + ",readonly",
        ]
    for command in [
        ["-m", "alembic", "upgrade", "head"],
        [str(candidate / "source/scripts/configure_embeddings.py"), "--yes"],
    ]:
        with (LOGS / "database-migration.log").open("a") as stream:
            subprocess.run(
                ["docker", "run", "--rm", *api_common, image_id, *command],
                cwd=ROOT,
                stdout=stream,
                stderr=subprocess.STDOUT,
                check=True,
                timeout=180,
            )
    run(
        "docker",
        "run",
        "-d",
        "--name",
        OWNER + "-api",
        *api_common,
        image_id,
        "-m",
        "uvicorn",
        "src.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        "18140",
        "--no-access-log",
    )
    with httpx.Client(trust_env=False, timeout=2) as http:
        for _ in range(120):
            try:
                if http.get("http://127.0.0.1:18140/health").status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        else:
            raise ValueError("candidate_honcho_api_not_ready")
    run(
        "docker",
        "run",
        "-d",
        "--name",
        OWNER + "-deriver",
        *api_common,
        image_id,
        "-m",
        "src.deriver",
    )
    if args.production:
        for name in ("db", "api", "deriver"):
            run("docker", "update", "--restart", "unless-stopped", OWNER + "-" + name)
    report = {
        "candidate_ready": True,
        "image_id": image_id,
        "database_image_id": db_image,
        "network": net,
        "auth_enabled": True,
        "embedding_dimensions": 1024,
        "embedding_device": "cpu",
        "provider_budget_proxy": True,
        "formal_platform_memory_enabled": False,
        "production_dependencies": args.production,
    }
    (DEPLOY / "testing/services.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
