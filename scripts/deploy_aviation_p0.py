"""Guarded P0 corpus switch with pinned config backups and v5 rollback."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import time
from contextlib import closing
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "var/rag/corpora/kg_20261124_aviation_v6_p0"
STATE = ROOT / "var/rag/deployments/aviation-v6-p0"
OUT = ROOT / "output/rag-p0-repair-20261124"
QWEN = ROOT / "services/rag/qwen3-production-v1.json"
NATIVE = ROOT / "services/rag/real-agent-production-v1.json"
CONTAINER = "scat-hermes-h7b-hermes-main-1"
OPENER = build_opener(ProxyHandler({}))


def read(path):
    return json.loads(path.read_text(encoding="utf8"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf8")


def run(args, timeout=180):
    p = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    if p.returncode:
        raise RuntimeError(str(args[:3]) + ": " + p.stderr[-1000:])
    return p.stdout.strip()


def health(port):
    try:
        endpoint = "/health/ready" if port in [18133, 18134] else "/health"
        with OPENER.open(f"http://127.0.0.1:{port}" + endpoint, timeout=3) as response:
            return json.load(response)
    except (OSError, ValueError):
        return None


def wait(port, digest=None, seconds=180):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        h = health(port)
        if h and (digest is None or h.get("corpus_sha256") == digest):
            return h
        time.sleep(1)
    raise TimeoutError(f"service_not_ready:{port}")


def inspect():
    return json.loads(run(["docker", "inspect", CONTAINER]))[0]


def prepare():
    assert read(OUT / "structural-audit.json")["status"] == "passed"
    assert read(TARGET / "ready.json")["corpus_sha256"] == sha(TARGET / "rag_chunks.v1.jsonl")
    inputs = read(TARGET / "staging-inputs.json")
    assert sha(QWEN) == inputs[str(QWEN.relative_to(ROOT))]
    assert sha(NATIVE) == inputs[str(NATIVE.relative_to(ROOT))]
    if (STATE / "before.json").exists():
        raise ValueError("deployment_already_prepared")
    info = inspect()
    compose = Path(info["Config"]["Labels"]["com.docker.compose.project.config_files"])
    original = read(compose)
    candidate = copy.deepcopy(original)
    mounts = candidate["services"]["hermes-main"]["volumes"]
    match = [v for v in mounts if v.get("target") == str(NATIVE)]
    assert len(match) == 1 and match[0]["read_only"] and match[0]["source"].endswith("kg_20261123_aviation_v5/real-agent-config.json")
    match[0]["source"] = str(TARGET / "real-agent-config.json")
    check = copy.deepcopy(candidate)
    next(v for v in check["services"]["hermes-main"]["volumes"] if v.get("target") == str(NATIVE))["source"] = next(v for v in original["services"]["hermes-main"]["volumes"] if v.get("target") == str(NATIVE))["source"]
    assert check == original
    STATE.mkdir(parents=True)
    for name, path in [("qwen3-before.json", QWEN), ("native-before.json", NATIVE)]:
        (STATE / name).write_bytes(path.read_bytes())
    save(STATE / "compose.aviation-v6-p0.json", candidate)
    run(["docker", "compose", "-f", str(STATE / "compose.aviation-v6-p0.json"), "config", "--quiet"])
    save(STATE / "before.json", {"container_id": info["Id"], "compose": str(compose), "compose_sha256": sha(compose),
                               "qwen_sha256": sha(QWEN), "native_sha256": sha(NATIVE), "retrieval_health": health(18133)})
    save(STATE / "plan.json", {"change": "v5 to source-repaired v6 P0 corpus; one read-only config mount source",
                              "candidate_sha256": sha(TARGET / "rag_chunks.v1.jsonl"), "external_model_calls": 0,
                              "required_gate": "retrieval-summary passed and no active native knowledge run",
                              "rollback": "/home/h3c/anaconda3/envs/scat-agent/bin/python scripts/deploy_aviation_p0.py rollback"})
    print(json.dumps({"status": "prepared", "production_changed": False}))


def stop_candidate():
    info = read(OUT / "candidate-process.json")
    p = Path(f"/proc/{info['pid']}/cmdline")
    if p.exists():
        args = [a.decode() for a in p.read_bytes().split(b"\0") if a]
        assert args == info["command"]
        os.kill(info["pid"], signal.SIGTERM)


def rollback():
    before = read(STATE / "before.json")
    assert sha(Path(before["compose"])) == before["compose_sha256"]
    QWEN.write_bytes((STATE / "qwen3-before.json").read_bytes())
    NATIVE.write_bytes((STATE / "native-before.json").read_bytes())
    run(["systemctl", "--user", "restart", "scat-hermes-rag.service"])
    retrieval = wait(18133, before["retrieval_health"]["corpus_sha256"])
    run(["docker", "compose", "-f", before["compose"], "up", "-d", "--no-deps", "hermes-main"])
    main = wait(18121)
    save(OUT / "production-rollback.json", {"status": "rolled_back", "retrieval": retrieval, "main": main})


def activate():
    before = read(STATE / "before.json")
    assert read(OUT / "retrieval-summary.json")["status"] == "passed"
    assert read(OUT / "structural-audit.json")["status"] == "passed"
    assert read(OUT / "label-audit.json")["status"] == "passed"
    assert sha(QWEN) == before["qwen_sha256"] and sha(NATIVE) == before["native_sha256"]
    assert inspect()["Id"] == before["container_id"]
    for dbpath in (ROOT / "var/deploy/h7b/rag-operations").glob("*/invocations.sqlite"):
        with closing(sqlite3.connect(f"file:{dbpath}?mode=ro", uri=True)) as db:
            active = db.execute("select count(*) from rag_invocations where status in ('starting','running')").fetchone()[0]
        if active:
            raise RuntimeError("active_native_knowledge_run_defer_switch")
    try:
        QWEN.write_bytes((TARGET / "retrieval-config.json").read_bytes())
        NATIVE.write_bytes((TARGET / "real-agent-config.json").read_bytes())
        run(["systemctl", "--user", "restart", "scat-hermes-rag.service"])
        retrieval = wait(18133, sha(TARGET / "rag_chunks.v1.jsonl"))
        run(["docker", "compose", "-f", str(STATE / "compose.aviation-v6-p0.json"), "up", "-d", "--no-deps", "hermes-main"])
        main = wait(18121)
        code = "import json,sys;from pathlib import Path;sys.path.insert(0,'src');from hermes_runtime.real_rag import load_config,load_corpus;c=load_config(Path('services/rag/real-agent-production-v1.json'));r=load_corpus(c);print(json.dumps({'generation_id':c['generation_id'],'chunks':len(r),'corpus_sha256':c['corpus_sha256']}))"
        native = json.loads(run(["docker", "exec", CONTAINER, "/usr/local/bin/scat-python", "-B", "-c", code]))
        assert native["corpus_sha256"] == sha(TARGET / "rag_chunks.v1.jsonl") and native["chunks"] == 9001
        stop_candidate()
        save(OUT / "production-activation.json", {"status": "activated", "retrieval_health": retrieval,
             "main_health": main, "native_read_contract": native, "active_native_runs_before_switch": 0,
             "external_model_calls": 0, "qa_accuracy_remeasured": False, "rollback_command": read(STATE / "plan.json")["rollback"]})
        print(json.dumps({"status": "activated", "generation": native["generation_id"], "chunks": native["chunks"]}))
    except Exception:
        rollback()
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["prepare", "activate", "rollback"])
    args = parser.parse_args()
    {"prepare": prepare, "activate": activate, "rollback": rollback}[args.action]()
