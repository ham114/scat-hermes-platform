"""Candidate evidence must not turn stale reports, unchecked boxes or modified files into Go."""

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.visualization_runtime.test_pipeline import pipeline as pipeline  # noqa: PLC0414
from visualization_runtime import release_acceptance as acceptance
from visualization_runtime.snapshots import digest

ROOT = Path(__file__).parents[2]
CANDIDATE = {"commit": "a" * 40, "source_sha256": "b" * 64, "dirty": False}


def workspace(root):
    for name in (
        "config/visualization-acceptance-v1.json",
        "config/visualization-release-gates-v1.json",
        "services/visualization-renderer/runtime.lock.json",
    ):
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def round_fixture(pipeline):
    app, _context, job, worker, *_ = pipeline
    plan = {"cases": [{"id": "request-1", "intent": job["request"].intent}]}
    session = {
        "candidate": CANDIDATE,
        "runtime_image": worker.launcher.sandbox.image,
        "plan_sha256": digest(plan),
    }
    report = app.jobs.workspace_root / "report.json"
    write(
        report,
        session | {"cases": [{"id": "request-1", "task_id": job["task_id"], "passed": True}]},
    )
    return app, worker, plan, session, report


def test_prepare_starts_all_gates_pending_and_never_reuses_old_acceptance(tmp_path, monkeypatch):
    workspace(tmp_path)
    folder = acceptance.prepare(tmp_path, "fresh", CANDIDATE)
    monkeypatch.setattr(acceptance, "candidate_stamp", lambda _: CANDIDATE)
    result = acceptance.assess(tmp_path, folder)
    assert not result["ready_for_release_review"]
    assert len(result["blockers"]) == 11
    assert not acceptance.read_json(folder / "session.json")["new_paid_calls_authorized"]
    with pytest.raises(FileExistsError):
        acceptance.prepare(tmp_path, "fresh", CANDIDATE)


def test_manual_gate_requires_candidate_all_checks_and_unchanged_attachment(tmp_path):
    workspace(tmp_path)
    folder = acceptance.prepare(tmp_path, "fresh", CANDIDATE)
    session = acceptance.read_json(folder / "session.json")
    gate = acceptance.read_json(tmp_path / "config/visualization-release-gates-v1.json")["gates"][0]
    proof = folder / "evidence/offline.json"
    with pytest.raises(ValueError, match="not_passed"):
        acceptance.gate_evidence(folder, session, gate, proof)
    log = folder / "evidence/tests.txt"
    log.write_text("simulated unit-test evidence", encoding="utf-8")
    record = acceptance.read_json(proof)
    record.update(
        status="pass",
        checks=dict.fromkeys(gate["checks"], True),
        attachments=[{"file": "tests.txt", "sha256": acceptance.file_sha(log)}],
    )
    write(proof, record)
    assert acceptance.gate_evidence(folder, session, gate, proof)["sha256"]
    log.write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="attachment_invalid"):
        acceptance.gate_evidence(folder, session, gate, proof)


def test_round_recomputes_publication_instead_of_trusting_passed_boolean(pipeline):
    app, worker, plan, session, report = round_fixture(pipeline)
    with pytest.raises(ValueError, match="unreconciled"):
        acceptance.round_evidence(app.jobs.workspace_root, session, plan, report)
    asyncio.run(worker.run_once())
    assert acceptance.round_evidence(app.jobs.workspace_root, session, plan, report)["task_ids"]


def test_old_p6_report_without_candidate_stamp_is_rejected(pipeline):
    app, _, plan, session, report = round_fixture(pipeline)
    value = acceptance.read_json(report)
    del value["candidate"]
    write(report, value)
    with pytest.raises(ValueError, match="candidate_mismatch"):
        acceptance.round_evidence(app.jobs.workspace_root, session, plan, report)


def test_changed_review_or_missing_image_cannot_be_imported_as_passed(pipeline):
    app, worker, plan, session, report = round_fixture(pipeline)
    asyncio.run(worker.run_once())
    with app.jobs.transaction() as db:
        db.execute("UPDATE visualization_reviews SET report_sha=?", ("0" * 64,))
    with pytest.raises(ValueError, match="review_changed"):
        acceptance.round_evidence(app.jobs.workspace_root, session, plan, report)


def test_two_rounds_cannot_reuse_same_tasks(pipeline, monkeypatch):
    app, worker, plan, binding, report = round_fixture(pipeline)
    root = app.jobs.workspace_root
    workspace(root)
    write(root / "config/visualization-acceptance-v1.json", plan)
    lock = acceptance.read_json(root / "services/visualization-renderer/runtime.lock.json")
    lock["validated_renderer_image_id"] = binding["runtime_image"]
    write(root / "services/visualization-renderer/runtime.lock.json", lock)
    folder = acceptance.prepare(root, "fresh", CANDIDATE)
    asyncio.run(worker.run_once())
    session = acceptance.read_json(folder / "session.json")
    evidence = acceptance.round_evidence(root, session, plan, report)
    session["rounds"] = {"1": evidence, "2": evidence}
    acceptance.save(folder, session)
    monkeypatch.setattr(acceptance, "candidate_stamp", lambda _: CANDIDATE)
    result = acceptance.assess(root, folder)
    assert "round_2_missing_or_invalid" in result["blockers"]
    assert not result["ready_for_release_review"]


def test_code_change_invalidates_session(tmp_path, monkeypatch):
    workspace(tmp_path)
    folder = acceptance.prepare(tmp_path, "fresh", CANDIDATE)
    monkeypatch.setattr(
        acceptance, "candidate_stamp", lambda _: CANDIDATE | {"source_sha256": "c" * 64}
    )
    assert "candidate_changed_or_dirty" in acceptance.assess(tmp_path, folder)["blockers"]


def test_document_only_commit_preserves_code_candidate_but_source_change_does_not(tmp_path):
    def git(*args):
        return subprocess.check_output(
            [
                "git",
                "-c",
                "user.name=Acceptance Test",
                "-c",
                "user.email=acceptance@example.invalid",
                *args,
            ],
            cwd=tmp_path,
            text=True,
        )

    git("init", "--quiet")
    (tmp_path / "src").mkdir()
    (tmp_path / "src/example.py").write_text("value=1\n")
    git("add", "src")
    git("commit", "--quiet", "-m", "candidate")
    first = acceptance.candidate_stamp(tmp_path)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/report.md").write_text("evidence only\n")
    git("add", "docs")
    git("commit", "--quiet", "-m", "record evidence")
    assert acceptance.candidate_stamp(tmp_path) == first
    (tmp_path / "src/example.py").write_text("value=2\n")
    assert acceptance.candidate_stamp(tmp_path)["dirty"] is True
    git("add", "src")
    git("commit", "--quiet", "-m", "changed candidate")
    assert acceptance.candidate_stamp(tmp_path)["commit"] != first["commit"]


def test_missing_published_image_blocks_round(pipeline):
    app, worker, plan, session, report = round_fixture(pipeline)
    asyncio.run(worker.run_once())
    image = next((app.snapshots.root / "published").glob("*/mobile.png"))
    image.chmod(0o600)
    image.unlink()
    with pytest.raises(ValueError, match="artifact_reference_missing"):
        acceptance.round_evidence(app.jobs.workspace_root, session, plan, report)


def test_negative_case_that_was_approved_is_not_accepted(pipeline):
    app, worker, plan, session, report = round_fixture(pipeline)
    asyncio.run(worker.run_once())
    plan["cases"][0]["expected_first"] = "not_pass"
    session["plan_sha256"] = digest(plan)
    value = acceptance.read_json(report)
    value["plan_sha256"] = digest(plan)
    write(report, value)
    with pytest.raises(ValueError, match="negative_case_not_rejected"):
        acceptance.round_evidence(app.jobs.workspace_root, session, plan, report)


def test_report_cannot_hide_different_render_image(pipeline):
    app, worker, plan, session, report = round_fixture(pipeline)
    asyncio.run(worker.run_once())
    session["runtime_image"] = "sha256:" + "2" * 64
    value = acceptance.read_json(report)
    value["runtime_image"] = session["runtime_image"]
    write(report, value)
    with pytest.raises(ValueError, match="runtime_or_render_changed"):
        acceptance.round_evidence(app.jobs.workspace_root, session, plan, report)


def test_two_distinct_complete_rounds_and_all_evidence_allow_release_review(pipeline, monkeypatch):
    app, worker, plan, binding, report = round_fixture(pipeline)
    root = app.jobs.workspace_root
    workspace(root)
    write(root / "config/visualization-acceptance-v1.json", plan)
    lock_path = root / "services/visualization-renderer/runtime.lock.json"
    lock = acceptance.read_json(lock_path)
    lock["validated_renderer_image_id"] = binding["runtime_image"]
    write(lock_path, lock)
    folder = acceptance.prepare(root, "fresh", CANDIDATE)
    session = acceptance.read_json(folder / "session.json")
    asyncio.run(worker.run_once())
    session["rounds"]["1"] = acceptance.round_evidence(root, session, plan, report)
    second = pipeline_factory(root / "second")
    _, worker2, _, binding2, report2 = round_fixture(second)
    assert binding2 == binding
    asyncio.run(worker2.run_once())
    session["rounds"]["2"] = acceptance.round_evidence(root, session, plan, report2)
    attachment = folder / "evidence/unit-evidence.txt"
    attachment.write_text("Simulated evidence for evaluator unit test only", encoding="utf-8")
    for gate in acceptance.read_json(root / "config/visualization-release-gates-v1.json")["gates"]:
        proof = folder / "evidence" / (gate["id"] + ".json")
        value = acceptance.read_json(proof)
        value.update(
            status="pass",
            checks=dict.fromkeys(gate["checks"], True),
            attachments=[{"file": attachment.name, "sha256": acceptance.file_sha(attachment)}],
        )
        write(proof, value)
        session["gates"][gate["id"]] = acceptance.gate_evidence(folder, session, gate, proof)
    acceptance.save(folder, session)
    monkeypatch.setattr(acceptance, "candidate_stamp", lambda _: CANDIDATE)
    result = acceptance.assess(root, folder)
    assert result["ready_for_release_review"] is True
    assert result["production_changed"] is False


def pipeline_factory(root):
    return pipeline.__wrapped__(root)
