"""Offline, candidate-bound release evidence. No service changes or model calls."""

import hashlib
import json
import re
import sqlite3
import subprocess
from contextlib import closing

from visualization_runtime.paths import confined_path
from visualization_runtime.release import readonly, verify_artifact_references
from visualization_runtime.snapshots import digest


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def file_sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def candidate_stamp(root):
    scope = (
        "src",
        "services",
        "frontend/src",
        "scripts",
        "config",
        "infra",
        "tests",
        "pyproject.toml",
    )
    names = (
        subprocess.check_output(["git", "ls-files", "-z", "--", *scope], cwd=root)
        .decode("utf-8")
        .split("\0")
    )
    files = [confined_path(root, root / name) for name in names if name]
    hashes = {
        path.relative_to(root).as_posix(): hashlib.sha256(
            path.read_bytes().replace(b"\r\n", b"\n")
        ).hexdigest()
        for path in sorted(set(files))
    }
    # Appending an evidence report under docs/ must not invalidate identical code.
    commit = subprocess.check_output(
        ["git", "log", "-1", "--format=%H", "--", *scope], cwd=root, text=True
    ).strip()
    dirty = subprocess.check_output(
        ["git", "status", "--porcelain", "--", *scope], cwd=root, text=True
    ).strip()
    return {"commit": commit, "source_sha256": digest(hashes), "dirty": bool(dirty)}


def prepare(root, name, candidate):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
        raise ValueError("invalid_acceptance_session")
    folder = confined_path(root, root / ".tmp/visualization-release" / name)
    folder.mkdir(parents=True, exist_ok=False)
    folder.chmod(0o700)
    plan = read_json(root / "config/visualization-acceptance-v1.json")
    gates = read_json(root / "config/visualization-release-gates-v1.json")
    image = read_json(root / "services/visualization-renderer/runtime.lock.json")[
        "validated_renderer_image_id"
    ]
    session = {
        "schema_version": 1,
        "name": name,
        "candidate": candidate,
        "plan_sha256": digest(plan),
        "gates_sha256": digest(gates),
        "runtime_image": image,
        "new_paid_calls_authorized": False,
        "rounds": {},
        "gates": {},
    }
    save(folder, session)
    (folder / "frozen-plan.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    evidence = folder / "evidence"
    evidence.mkdir(mode=0o700)
    for gate in gates["gates"]:
        template = {
            "schema_version": 1,
            "gate_id": gate["id"],
            "candidate": candidate,
            "status": "pending",
            "checks": dict.fromkeys(gate["checks"], False),
            "attachments": [],
        }
        (evidence / (gate["id"] + ".json")).write_text(
            json.dumps(template, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return folder


def save(folder, session):
    path = folder / "session.json"
    path.write_text(json.dumps(session, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)


def load_session(root, folder):
    folder = confined_path(root, folder)
    session = read_json(folder / "session.json")
    if session["schema_version"] != 1:
        raise ValueError("unsupported_acceptance_session")
    return folder, session


def gate_evidence(folder, session, gate, proof):
    proof = confined_path(folder / "evidence", proof)
    record = read_json(proof)
    if record.get("candidate") != session["candidate"] or record.get("gate_id") != gate["id"]:
        raise ValueError("acceptance_evidence_candidate_mismatch")
    if (
        record.get("status") != "pass"
        or set(record.get("checks", {})) != set(gate["checks"])
        or not all(value is True for value in record["checks"].values())
    ):
        raise ValueError("acceptance_gate_not_passed")
    if not record.get("attachments"):
        raise ValueError("acceptance_gate_evidence_missing")
    for item in record["attachments"]:
        path = confined_path(folder / "evidence", folder / "evidence" / item["file"])
        if (
            path == proof
            or not path.is_file()
            or not path.stat().st_size
            or file_sha(path) != item["sha256"]
        ):
            raise ValueError("acceptance_attachment_invalid")
    return {"file": proof.relative_to(folder).as_posix(), "sha256": file_sha(proof)}


def round_evidence(root, session, plan, report_path):
    report_path = confined_path(root, report_path)
    report = read_json(report_path)
    if (
        report.get("candidate") != session["candidate"]
        or report.get("plan_sha256") != session["plan_sha256"]
        or report.get("runtime_image") != session["runtime_image"]
    ):
        raise ValueError("acceptance_round_candidate_mismatch")
    cases = report.get("cases", [])
    if len(cases) != len(plan["cases"]) or {c["id"] for c in cases} != {
        c["id"] for c in plan["cases"]
    }:
        raise ValueError("acceptance_round_incomplete")
    database = confined_path(root, report_path.parent / "tasks.sqlite")
    task_ids = []
    with closing(readonly(database)) as db:
        if (
            db.execute(
                "SELECT 1 FROM visualization_jobs WHERE state NOT IN ('succeeded','failed','cancelled') LIMIT 1"
            ).fetchone()
            or db.execute(
                "SELECT 1 FROM visualization_provider_calls WHERE state IN ('reserved','sending','unknown') LIMIT 1"
            ).fetchone()
            or db.execute(
                "SELECT 1 FROM visualization_resources WHERE state='active' LIMIT 1"
            ).fetchone()
        ):
            raise ValueError("acceptance_round_unreconciled")
        for case in plan["cases"]:
            row = next(item for item in cases if item["id"] == case["id"])
            job = db.execute(
                "SELECT request_id,state,request FROM visualization_jobs WHERE task_id=?",
                (row.get("task_id"),),
            ).fetchone()
            if (
                job is None
                or job[0] != case["id"]
                or json.loads(job[2])["intent"] != case["intent"]
            ):
                raise ValueError("acceptance_round_task_mismatch")
            reviews = []
            for raw, sha, render_raw, render_sha in db.execute(
                "SELECT v.report,v.report_sha,r.manifest,r.manifest_sha FROM visualization_reviews v JOIN visualization_renders r USING(render_id) WHERE r.task_id=? ORDER BY v.created",
                (row["task_id"],),
            ):
                from visualization_runtime.contracts import ReviewResult

                value = json.loads(raw)
                if digest(value) != sha:
                    raise ValueError("acceptance_review_changed")
                render = json.loads(render_raw)
                if (
                    digest(render) != render_sha
                    or render.get("runtime_image") != session["runtime_image"]
                    or value.get("render_sha256") != render_sha
                ):
                    raise ValueError("acceptance_runtime_or_render_changed")
                parsed = ReviewResult.model_validate(
                    {key: value[key] for key in ("decision", "issues", "checked_viewports")}
                )
                if set(parsed.checked_viewports) != {"desktop", "mobile"}:
                    raise ValueError("acceptance_viewport_missing")
                reviews.append(parsed.decision)
            if case.get("expected_first") == "not_pass":
                if not reviews or reviews[0] not in {"revise", "reject"}:
                    raise ValueError("acceptance_negative_case_not_rejected")
            elif (
                job[1] != "succeeded"
                or not reviews
                or reviews[-1] != "pass"
                or not db.execute(
                    "SELECT 1 FROM visualization_publications WHERE task_id=?", (row["task_id"],)
                ).fetchone()
            ):
                raise ValueError("acceptance_positive_case_not_delivered")
            task_ids.append(row["task_id"])
    verify_artifact_references(root, database, report_path.parent / "artifacts")
    return {
        "file": report_path.relative_to(root).as_posix(),
        "sha256": file_sha(report_path),
        "database_sha256": file_sha(database),
        "task_ids": task_ids,
    }


def assess(root, folder):
    folder, session = load_session(root, folder)
    plan = read_json(root / "config/visualization-acceptance-v1.json")
    gates = read_json(root / "config/visualization-release-gates-v1.json")
    blockers = []
    current = candidate_stamp(root)
    if current != session["candidate"] or current["dirty"]:
        blockers.append("candidate_changed_or_dirty")
    if digest(plan) != session["plan_sha256"] or digest(gates) != session["gates_sha256"]:
        blockers.append("acceptance_plan_changed")
    tasks = set()
    for index in range(1, gates["required_rounds"] + 1):
        try:
            recorded = session["rounds"][str(index)]
            fresh = round_evidence(root, session, plan, root / recorded["file"])
            if fresh != recorded or tasks.intersection(fresh["task_ids"]):
                raise ValueError("acceptance_round_changed_or_reused")
            tasks.update(fresh["task_ids"])
        except (KeyError, ValueError, OSError, sqlite3.Error):
            blockers.append("round_" + str(index) + "_missing_or_invalid")
    for gate in gates["gates"]:
        try:
            recorded = session["gates"][gate["id"]]
            if gate_evidence(folder, session, gate, folder / recorded["file"]) != recorded:
                raise ValueError("acceptance_gate_changed")
        except (KeyError, ValueError, OSError):
            blockers.append("gate_" + gate["id"] + "_missing_or_invalid")
    return {
        "ready_for_release_review": not blockers,
        "production_changed": False,
        "blockers": blockers,
        "manual_evidence_is_operator_attestation": True,
    }
