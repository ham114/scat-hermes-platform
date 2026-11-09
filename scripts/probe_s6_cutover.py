"""Actual private default/rollback process boot, without model calls or business effects."""

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
NATIVE = "/home/h3c/anaconda3/envs/scat-hermes/bin/python"


async def main():
    identity = uuid4().hex[:10]
    work = ROOT / ".tmp/s6" / ("cutover-" + identity)
    work.mkdir(parents=True)
    report = {
        "status": "running",
        "identity": identity,
        "llm_calls": 0,
        "business_effects": 0,
        "production_ports_changed": False,
        "profiles": [],
    }
    env = os.environ | {
        "TMPDIR": str(work),
        "TMP": str(work),
        "TEMP": str(work),
        "PYTHONDONTWRITEBYTECODE": "1",
        "HERMES_TASK_STATE_FILE": str(work / "tasks.sqlite"),
        "HERMES_RESULT_STATE_FILE": str(work / "results.sqlite"),
        "HERMES_STATE_FILE": str(work / "platform.sqlite"),
        "SCAT_GBAR_V23_ENABLED": "false",
        "SCAT_GBAR_V23_COMPARISON_SUBPROCESS_ENABLED": "false",
    }
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 18103))
    try:
        for name, options in [
            ("skills-default", []),
            ("historical-h5", ["--legacy-h5", "--with-mcp", "--with-model-tasks"]),
        ]:
            home = work / name
            log_path = (
                ROOT / "var/validation" / ("20261023-S6-cutover-" + identity + "-" + name + ".log")
            )
            with log_path.open("x") as log:
                log_path.chmod(0o600)
                child = await asyncio.to_thread(
                    subprocess.Popen,
                    [
                        NATIVE,
                        "-B",
                        str(ROOT / "scripts/run_hermes_api.py"),
                        "--home",
                        str(home.relative_to(ROOT)),
                        "--port",
                        "18103",
                        "--synthetic-mcp",
                        *options,
                    ],
                    cwd=ROOT,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                )
            try:
                async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
                    for _ in range(140):
                        if child.poll() is not None:
                            raise RuntimeError(name + "_exited")
                        try:
                            if (
                                await client.get("http://127.0.0.1:18103/health")
                            ).status_code == 200:
                                break
                        except httpx.TransportError:
                            pass
                        await asyncio.sleep(0.5)
                    else:
                        raise RuntimeError(name + "_unavailable")
                    assert (
                        await client.get(
                            "http://127.0.0.1:18103/v1/skills/publish",
                            headers={
                                "Authorization": "Bearer "
                                + (ROOT / "var/secrets/hermes-api.key").read_text().strip()
                            },
                        )
                    ).status_code in {404, 405}, "unapproved_author_endpoint"
                config = json.loads((home / "config.yaml").read_text())
                tools = config["platform_toolsets"]["api_server"]
                assert ("scat_skills" in tools and "scat_workflows" in tools) == (
                    name == "skills-default"
                )
                assert len(list((home / "skills").glob("*/SKILL.md"))) == (
                    12 if name == "skills-default" else 0
                )
                report["profiles"].append(
                    {
                        "profile": name,
                        "health": "passed",
                        "toolsets": tools,
                        "explicit_legacy": bool(options),
                        "new_runtime_state": True,
                    }
                )
            finally:
                if child.poll() is None:
                    child.terminate()
                    try:
                        await asyncio.to_thread(child.wait, timeout=15)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        await asyncio.to_thread(child.wait, timeout=5)
        report["status"] = "passed"
    finally:
        assert work.resolve().parent == (ROOT / ".tmp/s6").resolve() and not work.is_symlink()
        shutil.rmtree(work)
        report["temporary_workspace_removed"] = True
        path = ROOT / "docs/validation" / ("20261023-S6-cutover-" + identity + ".json")
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({"report": path.relative_to(ROOT).as_posix(), "status": report["status"]}))


if __name__ == "__main__":
    if not (Path(sys.prefix) / "conda-meta").is_dir():
        raise RuntimeError("Existing Anaconda Python required")
    asyncio.run(main())
