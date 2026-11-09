"""Thirty-minute bounded health sample; explicitly not a long-term SLA claim."""

import json
import statistics
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261108-release-readiness"
TARGETS = {
    "platform": "http://127.0.0.1:18120/health/ready",
    "main": "http://127.0.0.1:18121/health",
    "actions": "http://127.0.0.1:18123/health",
    "native-worker": "http://127.0.0.1:18122/health",
    "worker": "http://127.0.0.1:18126/health",
    "author-api": "http://127.0.0.1:18124/health",
    "author": "http://127.0.0.1:18125/health",
    "portal": "http://127.0.0.1:18159/portal-health",
}


def main(seconds=1800, record_name="health-30m"):
    target = OUT / (record_name + ".jsonl")
    assert not target.exists(), "sample_already_started_do_not_reset"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    start = time.monotonic()
    rounds = 0
    latency = {k: [] for k in TARGETS}
    failures = []
    with target.open("x") as stream:
        while time.monotonic() - start < seconds:
            cycle = time.monotonic()
            at = datetime.now(UTC).isoformat()
            for name, url in TARGETS.items():
                began = time.monotonic()
                try:
                    with opener.open(url, timeout=3) as response:
                        status = response.status
                        response.read(65536)
                except (OSError, urllib.error.URLError) as exc:
                    status = getattr(exc, "code", 0)
                elapsed = round((time.monotonic() - began) * 1000, 3)
                row = {"at": at, "service": name, "status": status, "latency_ms": elapsed}
                stream.write(json.dumps(row) + "\n")
                latency[name].append(elapsed)
                if status != 200:
                    failures.append(row)
            stream.flush()
            rounds += 1
            if rounds == 1 and failures:
                raise RuntimeError("initial_health_targets_not_ready")
            time.sleep(max(0, 10 - (time.monotonic() - cycle)))
    report = {
        "kind": "bounded_health_sample_not_longterm_sla",
        "duration_seconds": round(time.monotonic() - start, 2),
        "rounds": rounds,
        "failures": failures,
        "services": {
            k: {
                "samples": len(v),
                "median_ms": statistics.median(v),
                "p95_ms": sorted(v)[min(len(v) - 1, int(len(v) * 0.95))],
            }
            for k, v in latency.items()
        },
    }
    (OUT / (record_name + ".json")).write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    import sys

    if sys.argv[1:] == ["--post-switch"]:
        gpu = json.loads((ROOT / "var/gpu/production.json").read_text())
        TARGETS["gpu"] = gpu["transport"]["endpoint"] + "/ready"
        TARGETS["rag"] = "http://127.0.0.1:18133/health"
        main(300, "health-postswitch-5m")
    else:
        main()
