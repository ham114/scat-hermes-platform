"""Verify the existing approved GBAR output and freeze its view without inference."""

import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from application.context import ExecutionContext
from application.model_output_view import saved_model_output
from application.task_store import TaskStore


def main():
    out = ROOT / "var/validation/20261108-release-readiness"
    private = out / "private"
    private.mkdir(exist_ok=True)
    target = private / "saved-gbar-output.json"
    assert not target.exists(), "already_frozen_do_not_overwrite"
    path = ROOT / "var/gpu/requests.sqlite"
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    tasks = SimpleNamespace(
        store=TaskStore(ROOT / "var/deploy/h7b/shared/state/tasks.sqlite", workspace_root=ROOT)
    )
    context = ExecutionContext("scat-researcher", "42297b89-54c3-469d-94c0-13bccae5b9e2")
    value = saved_model_output(context, tasks, "7b0dbcfd9cf846898ed3183b44a13e18", root=ROOT)
    assert value == saved_model_output(
        context, tasks, "7b0dbcfd9cf846898ed3183b44a13e18", root=ROOT
    )
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    target.write_text(json.dumps(value, ensure_ascii=False))
    target.chmod(0o600)
    report = {
        "kind": "real_persisted_approved_output",
        "task_id": "7b0dbcfd9cf846898ed3183b44a13e18",
        "artifact_id": value["artifact_id"],
        "payload_sha256": value["payload_sha256"],
        "source_receipts": value["source_receipts"],
        "blocks": [b["type"] for b in value["analysis_result"]["blocks"]],
        "reopen_identical": True,
        "new_model_calls": 0,
        "ledger_unchanged": True,
    }
    (out / "saved-gbar-output.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
