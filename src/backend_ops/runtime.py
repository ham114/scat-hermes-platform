"""Finite loopback candidate stack with durable state and process identity checks."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from backend_ops.environment import dump

APP = "/home/h3c/anaconda3/envs/scat-agent/bin/python"
NATIVE = "/home/h3c/anaconda3/envs/scat-hermes/bin/python"
DEFAULTS = {
    "instance": "h7a",
    "platform_port": 18120,
    "hermes_port": 18121,
    "background_port": 18122,
    "startup_seconds": 120,
    "shutdown_seconds": 25,
    "health_seconds": 3,
    "max_hours": 8,
    "min_free_bytes": 2147483648,
    "max_used_percent": 98,
    "log_bytes": 4194304,
    "log_backups": 3,
}


def inside(root, path):
    root = Path(root).resolve()
    path = Path(path)
    absolute = path if path.is_absolute() else root / path
    if ".." in absolute.parts or not absolute.is_relative_to(root) or absolute == root:
        raise ValueError("project_path_required")
    for part in [absolute, *absolute.parents]:
        if part == root:
            break
        if part.is_symlink():
            raise ValueError("symlink_refused")
    if not absolute.resolve().is_relative_to(root):
        raise ValueError("project_path_required")
    return absolute


def configuration(root, path):
    path = inside(root, path)
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or set(value) != set(DEFAULTS):
        raise ValueError("invalid_operations_configuration")
    if not isinstance(value["instance"], str) or not re.fullmatch(
        r"h7a(?:-[a-z0-9]{1,20})?", value["instance"]
    ):
        raise ValueError("invalid_instance")
    ranges = {
        "platform_port": (18120, 18139),
        "hermes_port": (18120, 18139),
        "background_port": (18120, 18139),
        "startup_seconds": (20, 180),
        "shutdown_seconds": (5, 300),
        "health_seconds": (1, 30),
        "max_hours": (1, 24),
        "min_free_bytes": (268435456, 107374182400),
        "max_used_percent": (80, 99),
        "log_bytes": (4096, 16777216),
        "log_backups": (1, 5),
    }
    for key, (low, high) in ranges.items():
        if type(value[key]) is not int or not low <= value[key] <= high:
            raise ValueError("invalid_operations_limit")
    if len({value[k] for k in ("platform_port", "hermes_port", "background_port")}) != 3:
        raise ValueError("duplicate_port")
    return value


def process_identity(pid):
    try:
        process = Path("/proc") / str(pid)
        if process.stat().st_uid != os.getuid():
            return None
        state = (process / "stat").read_text().rsplit(")", 1)[1].split()
        if state[0] == "Z":
            return None
        return {
            "pid": pid,
            "birth": state[19],
            "boot": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        }
    except (OSError, IndexError):
        return None


def matches(identity):
    return bool(identity and process_identity(identity["pid"]) == identity)


def disk_status(root, config):
    disk = shutil.disk_usage(root)
    # Match df: reserved filesystem blocks are not available to these unprivileged services.
    percent = round(disk.used * 100 / (disk.used + disk.free), 2)
    return {
        "free_bytes": disk.free,
        "used_percent": percent,
        "warning": percent >= 90,
        "ok": disk.free >= config["min_free_bytes"] and percent < config["max_used_percent"],
    }


def http_ok(port, route):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}{route}", timeout=2) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def private_key(root, path):
    path = inside(root, path)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "w") as stream:
            stream.write(secrets.token_urlsafe(48))
    if path.stat().st_mode & 0o077:
        raise ValueError("private_service_key_required")
    key = path.read_text().strip()
    if not 32 <= len(key) <= 128 or not re.fullmatch(r"[A-Za-z0-9_-]+", key):
        raise ValueError("invalid_service_key")
    return key


def preflight(root, config, *, ports=True):
    from dotenv import dotenv_values

    root = Path(root).resolve()
    for python in (APP, NATIVE):
        if not Path(python).is_file() or not (Path(python).parents[1] / "conda-meta").is_dir():
            raise ValueError("existing_conda_required")
    if not (root / "vendor/hermes-agent/gateway/platforms/api_server.py").is_file():
        raise ValueError("pinned_native_source_required")
    from hermes_runtime.native_profiles import verify_native
    from hermes_runtime.profiles import settings

    verify_native(root)
    settings(root, "scat-main")
    dotenv = inside(root, ".env")
    if dotenv.stat().st_mode & 0o077:
        raise ValueError("private_root_environment_required")
    credential = inside(root, "var/hermes/managed/profiles/scat-main/.env")
    if credential.stat().st_mode & 0o077 or not dotenv_values(credential).get("DEEPSEEK_API_KEY"):
        raise ValueError("private_profile_key_required")
    disk = disk_status(root, config)
    if not disk["ok"]:
        raise ValueError("disk_admission_refused")
    if ports:
        for key in ("platform_port", "hermes_port", "background_port"):
            with socket.socket() as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(("127.0.0.1", config[key]))
    return {"disk": disk, "existing_conda": True, "synthetic_only": True, "production_gpu": False}


class RotatingSink:
    def __init__(self, root, path, limit, backups):
        self.root, self.path = Path(root), Path(path)
        self.limit, self.backups = limit, backups
        self.lock = threading.Lock()
        self.failed = threading.Event()

    def write(self, data):
        with self.lock:
            inside(self.root, self.path)
            for offset in range(0, len(data), self.limit):
                chunk = data[offset : offset + self.limit]
                if self.path.exists() and self.path.stat().st_size + len(chunk) > self.limit:
                    for n in range(self.backups, 0, -1):
                        target = inside(self.root, str(self.path) + "." + str(n))
                        source = (
                            self.path
                            if n == 1
                            else inside(self.root, str(self.path) + "." + str(n - 1))
                        )
                        if source.exists():
                            source.replace(target)
                fd = os.open(
                    self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600
                )
                with os.fdopen(fd, "ab") as stream:
                    stream.write(chunk)

    def drain(self, stream):
        try:
            while data := stream.read(4096):
                self.write(data)
        except (OSError, ValueError):
            self.failed.set()
        finally:
            stream.close()


class Stack:
    def __init__(self, root, path):
        self.root = Path(root).resolve()
        self.path = inside(self.root, path)
        self.config = configuration(self.root, self.path)
        self.home = inside(self.root, "var/backend/" + self.config["instance"])
        self.temporary = inside(self.root, ".tmp/h7a/" + self.config["instance"])
        self.state_path = self.home / "services.json"
        for name in (
            "main",
            "jobs",
            "platform.sqlite",
            "jobs.sqlite",
            "results.sqlite",
            "platform-auth.key",
        ):
            inside(self.root, self.home / name)

    def record(self):
        path = inside(self.root, self.state_path)
        return json.loads(path.read_text()) if path.exists() else None

    def controller_matches(self, row):
        if not row or not matches(row.get("controller")):
            return False
        try:
            args = (
                (Path("/proc") / str(row["controller"]["pid"]) / "cmdline")
                .read_bytes()
                .split(b"\0")
            )
            return (
                str(self.root / "scripts/manage_backend.py").encode() in args
                and row["token"].encode() in args
                and str(self.path.relative_to(self.root)).encode() in args
            )
        except OSError:
            return False

    def status(self):
        row = self.record()
        if not row:
            return {"status": "stopped", "controller_alive": False}
        alive = self.controller_matches(row)
        result = {k: row[k] for k in ("status", "checks", "disk", "reason", "children") if k in row}
        result["controller_alive"] = alive
        if row["status"] not in {"stopped", "failed"} and not alive:
            result["status"] = "stale"
        return result

    def environment(self):
        state = self.home
        env = os.environ.copy()
        # Do not allow inherited optional experiments/GPU switches into this candidate stack.
        for name in list(env):
            if name.startswith(("SCAT_", "HERMES_")):
                env.pop(name)
        env.update(
            {
                "PYTHONNOUSERSITE": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "TMPDIR": str(self.temporary),
                "TMP": str(self.temporary),
                "TEMP": str(self.temporary),
                "HERMES_DISABLE_LAZY_INSTALLS": "1",
                "HERMES_BASE_URL": f"http://127.0.0.1:{self.config['hermes_port']}",
                "HERMES_STATE_FILE": str(state / "platform.sqlite"),
                "HERMES_TASK_STATE_FILE": str(state / "jobs.sqlite"),
                "HERMES_RESULT_STATE_FILE": str(state / "results.sqlite"),
                "HERMES_SKILL_JOBS_ENABLED": "true",
                "HERMES_WORKFLOWS_ENABLED": "true",
                "SCAT_S4_CPU_FIXTURE": "1",
                "SCAT_S5_SYNTHETIC": "1",
                "ANALYSIS_IDENTITY_MODE": "single_user",
                "AUTH_SECRET": private_key(self.root, state / "platform-auth.key"),
                "ANALYSIS_SINGLE_USER_ID": "scat-researcher",
                "ANALYSIS_RUNS_COLLECTION": "analysis_runs_"
                + self.config["instance"].replace("-", "_"),
                "ANALYSIS_ARTIFACTS_COLLECTION": "analysis_artifacts_"
                + self.config["instance"].replace("-", "_"),
                "SCAT_GBAR_V23_ENABLED": "false",
                "SCAT_GBAR_V23_COMPARISON_SUBPROCESS_ENABLED": "false",
                "SCAT_JOB_TEMP_DIR": str(self.temporary / "worker"),
            }
        )
        return env

    def commands(self):
        script = self.root / "scripts"
        return [
            (
                "hermes",
                [
                    NATIVE,
                    "-B",
                    str(script / "run_hermes_api.py"),
                    "--profile",
                    "scat-main",
                    "--home",
                    str((self.home / "main").relative_to(self.root)),
                    "--port",
                    str(self.config["hermes_port"]),
                    "--synthetic-mcp",
                    "--with-mcp",
                    "--with-skills",
                    "--with-model-tasks",
                    "--with-workflows",
                    "--s4-cpu-fixture",
                ],
            ),
            (
                "platform",
                [
                    APP,
                    "-B",
                    str(script / "run_platform_api.py"),
                    "--port",
                    str(self.config["platform_port"]),
                ],
            ),
            (
                "worker",
                [
                    APP,
                    "-B",
                    str(script / "run_model_worker.py"),
                    "--job-home",
                    str((self.home / "jobs").relative_to(self.root)),
                    "--background-port",
                    str(self.config["background_port"]),
                ],
            ),
        ]

    def serve(self, token):
        import fcntl

        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = inside(self.root, self.home / ".lock")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # A SIGKILLed supervisor leaves children. Require explicit verified stop before reuse.
            old = self.record()
            if old and any(matches(child.get("identity")) for child in old.get("children", [])):
                raise ValueError("orphan_services_require_stop")
            info = preflight(self.root, self.config)
            self.temporary.mkdir(parents=True, exist_ok=True, mode=0o700)
            row = {
                "status": "starting",
                "token": token,
                "controller": process_identity(os.getpid()),
                "config_sha256": hashlib.sha256(self.path.read_bytes()).hexdigest(),
                "started_at": time.time(),
                "disk": info["disk"],
                "checks": {},
                "children": [],
            }
            children, threads, sinks = [], [], []
            stop = threading.Event()
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda *_: stop.set())

            def save():
                dump(self.state_path, json.dumps(row, indent=2) + "\n")

            save()
            try:
                for name, command in self.commands():
                    child = subprocess.Popen(
                        command,
                        cwd=self.root,
                        env=self.environment(),
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                    children.append((name, child))
                    row["children"].append({"name": name, "identity": process_identity(child.pid)})
                    sink = RotatingSink(
                        self.root,
                        self.home / (name + ".log"),
                        self.config["log_bytes"],
                        self.config["log_backups"],
                    )
                    thread = threading.Thread(target=sink.drain, args=(child.stdout,), daemon=True)
                    thread.start()
                    threads.append(thread)
                    sinks.append(sink)
                    save()
                started = time.monotonic()
                readiness_seen = False
                while not stop.is_set():
                    row["disk"] = disk_status(self.root, self.config)
                    row["checks"] = {
                        "platform_live": http_ok(self.config["platform_port"], "/health/live"),
                        "platform_ready": http_ok(self.config["platform_port"], "/health/ready"),
                        "hermes_live": http_ok(self.config["hermes_port"], "/health"),
                        "worker_alive": children[-1][1].poll() is None,
                    }
                    if any(child.poll() is not None for _, child in children):
                        row.update(status="failed", reason="child_exited")
                        break
                    if any(sink.failed.is_set() for sink in sinks):
                        row.update(status="failed", reason="log_write_failed")
                        break
                    if not row["disk"]["ok"]:
                        row.update(status="failed", reason="disk_admission_refused")
                        break
                    ready = all(row["checks"].values())
                    readiness_seen |= ready
                    row["status"] = "ready" if ready else "degraded"
                    if (
                        not ready
                        and not readiness_seen
                        and time.monotonic() - started < self.config["startup_seconds"]
                    ):
                        row["status"] = "starting"
                    if not ready and row["status"] == "starting" and stop.is_set():
                        break
                    if time.monotonic() - started >= self.config["max_hours"] * 3600:
                        row["reason"] = "lifetime_elapsed"
                        break
                    save()
                    stop.wait(self.config["health_seconds"])
            finally:
                failed = row["status"] == "failed"
                row["status"] = "stopping"
                save()
                # Stop admission first; existing detached CPU effects retain their durable execution IDs.
                for name in ("platform", "worker", "hermes"):
                    for child_name, child in children:
                        if name == child_name and child.poll() is None:
                            with contextlib.suppress(ProcessLookupError):
                                os.killpg(child.pid, signal.SIGTERM)
                deadline = time.monotonic() + self.config["shutdown_seconds"]
                for _, child in children:
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        child.wait(timeout=max(0.1, deadline - time.monotonic()))
                    # The group leader may have exited before its MCP/background descendants.
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(child.pid, signal.SIGKILL)
                    child.wait(timeout=5)
                for thread in threads:
                    thread.join(timeout=5)
                row["status"] = "failed" if failed else "stopped"
                row["stopped_at"] = time.time()
                row["checks"] = {key: False for key in row["checks"]}
                save()
                # Do not delete job temporary files: detached effects can outlive this controller.

    def stop(self):
        row = self.record()
        if not row:
            return self.status()
        if self.controller_matches(row):
            os.kill(row["controller"]["pid"], signal.SIGTERM)
            until = time.monotonic() + self.config["shutdown_seconds"] + 15
            while self.controller_matches(row) and time.monotonic() < until:
                time.sleep(0.2)
            if self.controller_matches(row):
                raise ValueError("controller_stop_timed_out")
        elif row["status"] not in {"stopped", "failed"}:
            # No raw PID kills: only matching, recorded group leaders with project entrypoints.
            for child in row.get("children", []):
                identity = child.get("identity")
                if matches(identity):
                    args = (
                        (Path("/proc") / str(identity["pid"]) / "cmdline").read_bytes().split(b"\0")
                    )
                    if not any(
                        str(self.root / "scripts" / name).encode() in args
                        for name in (
                            "run_hermes_api.py",
                            "run_platform_api.py",
                            "run_model_worker.py",
                        )
                    ):
                        raise ValueError("unverified_orphan_process")
                    os.killpg(identity["pid"], signal.SIGTERM)
            time.sleep(1)
            for child in row.get("children", []):
                if matches(child.get("identity")):
                    os.killpg(child["identity"]["pid"], signal.SIGKILL)
            row.update(status="stopped", reason="verified_orphans_stopped", stopped_at=time.time())
            dump(self.state_path, json.dumps(row, indent=2) + "\n")
        return self.status()
