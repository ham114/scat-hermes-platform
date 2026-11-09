"""Explicit project-only rollout/rollback and user-systemd management for knowledge RAG."""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "var/rag/deployments/live-v1"
UNITS = ("scat-hermes-rag.service", "scat-hermes-api.service")
OPTIONS = {
    "SCAT_RAG_BASELINE_ENABLED": "true",
    "SCAT_RAG_BASELINE_URL": "http://127.0.0.1:18133",
    "SCAT_RAG_BASELINE_TIMEOUT_SECONDS": "10",
}


def command(*args, check=True):
    result = subprocess.run(
        ["systemctl", "--user", *args], capture_output=True, text=True, timeout=125, check=False
    )
    if check and result.returncode:
        raise RuntimeError("user_systemd_command_failed:" + args[0])
    return result.stdout.strip()


def health(port):
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/health", timeout=4) as response:
            return json.loads(response.read(65536))
    except (OSError, ValueError):
        return None


def ready():
    expected = json.loads((ROOT / "services/rag/qwen3-production-v1.json").read_text())
    value = health(18133)
    return bool(
        value
        and value.get("status") == "ready"
        and all(
            value.get(k) == expected[k] for k in ("profile_id", "generation_id", "corpus_sha256")
        )
    )


def wait():
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if ready():
            return
        time.sleep(0.5)
    raise TimeoutError("production_rag_readiness_deadline")


def set_options(restore=False):
    path = ROOT / ".env"
    backup = STATE / "previous-rag-options.json"
    lines = path.read_text().splitlines()
    if not backup.exists():
        originals = {
            k: [line for line in lines if re.match(r"^(?:export\s+)?" + k + r"\s*=", line)]
            for k in OPTIONS
        }
        backup.write_text(json.dumps(originals, indent=2))
        backup.chmod(0o600)
    originals = json.loads(backup.read_text())
    filtered = [
        line
        for line in lines
        if not any(re.match(r"^(?:export\s+)?" + k + r"\s*=", line) for k in OPTIONS)
    ]
    additions = (
        [line for k in OPTIONS for line in originals[k]]
        if restore
        else [k + "=" + v for k, v in OPTIONS.items()]
    )
    staged = ROOT / ".env.rag-live-writing"
    staged.write_text("\n".join(filtered + additions) + "\n")
    staged.chmod(0o600)
    os.replace(staged, path)


def install():
    directory = Path("/home/h3c/.config/systemd/user")
    directory.mkdir(parents=True, exist_ok=True)
    for name in UNITS:
        source = ROOT / "infra/systemd/user" / name
        target = directory / name
        if target.is_symlink() and target.resolve() == source:
            continue
        if target.exists() or target.is_symlink():
            raise ValueError("refuse_overwriting_unknown_unit:" + name)
        target.symlink_to(source)
    command("daemon-reload")


def disable_owned():
    # This host refuses disable for externally linked user units. Only unlink
    # our verified enablement links; keep the unit definitions available.
    directory = Path("/home/h3c/.config/systemd/user/default.target.wants")
    for name in UNITS:
        target = directory / name
        if target.exists() or target.is_symlink():
            if not target.is_symlink() or target.resolve() != ROOT / "infra/systemd/user" / name:
                raise ValueError("refuse_unlinking_unknown_enablement:" + name)
            target.unlink()
    command("daemon-reload")
    for name in UNITS:
        if command("is-enabled", name, check=False) not in {"disabled", "linked"}:
            raise ValueError("owned_unit_still_enabled:" + name)


def status():
    return {
        "units": {
            name: {
                "active": command("is-active", name, check=False),
                "enabled": command("is-enabled", name, check=False),
            }
            for name in UNITS
        },
        "rag_ready": ready(),
        "rag_health": health(18133),
        "hermes_health": health(18081),
    }


def main():
    if sys.executable != "/home/h3c/anaconda3/envs/scat-agent/bin/python":
        raise ValueError("use_existing_scat_agent")
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "status", "stop", "rollback", "wait"])
    args = parser.parse_args()
    if args.action == "wait":
        wait()
        return
    STATE.mkdir(parents=True, exist_ok=True)
    import fcntl

    lock = (STATE / "control.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if args.action == "start":
        for name, port in zip(UNITS, (18133, 18081), strict=True):
            if command("is-active", name, check=False) != "active":
                with socket.socket() as probe:
                    if probe.connect_ex(("127.0.0.1", port)) == 0:
                        raise ValueError("refuse_unowned_listener:" + str(port))
        install()
        try:
            command("enable", UNITS[0])
            command("start", UNITS[0])
            wait()
            set_options()
            command("enable", UNITS[1])
            command("start", UNITS[1])
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                if health(18081):
                    break
                time.sleep(0.5)
            else:
                raise TimeoutError("production_hermes_readiness_deadline")
        except (OSError, RuntimeError, TimeoutError):
            command("stop", *reversed(UNITS), check=False)
            if (STATE / "previous-rag-options.json").exists():
                set_options(restore=True)
            raise
    elif args.action in {"stop", "rollback"}:
        for name in UNITS:
            unit = Path("/home/h3c/.config/systemd/user") / name
            if not unit.is_symlink() or unit.resolve() != ROOT / "infra/systemd/user" / name:
                raise ValueError("refuse_controlling_unknown_unit:" + name)
        command("stop", *reversed(UNITS))
        if args.action == "rollback":
            disable_owned()
            set_options(restore=True)
    report = status()
    (STATE / "last-status.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report))


if __name__ == "__main__":
    main()
