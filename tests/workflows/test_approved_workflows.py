"""S5 approved reference flow against real detached CPU units, no LLM or Mongo."""

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path
from uuid import uuid4

import pytest

from application.context import ExecutionContext
from application.model_step_store import StepError
from application.skill_job_factory import create_skill_tasks
from application.skill_worker import SkillWorker, completion_reference
from application.task_store import TaskConflict, TaskNotFound, digest
from hermes_runtime.job_budget import ensure_account
from tests.hermes_runtime.test_skill_jobs_s4 import Artifacts, Runs, bound
from workflow_runtime.contracts import WorkflowProposal

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def app(tmp_path):
    value = create_skill_tasks(
        ROOT,
        tmp_path / "s5.sqlite",
        runs=Runs(),
        artifacts=Artifacts(),
        workflows_enabled=True,
        workflow_synthetic=True,
    )
    yield value
    # Each test owns unique scoped input files; never delete another task's snapshot.
    with closing(value.store.connect()) as db:
        snapshots = db.execute("SELECT snapshot FROM model_tasks").fetchall()
    for raw in snapshots:
        plan = json.loads(raw[0]).get("workflow")
        if not plan:
            continue
        identity = plan["input_sha256"]
        path = ROOT / "var/workflow-inputs" / (identity + ".json")
        if path.exists() and not path.is_symlink():
            path.unlink()


def proposal(**updates):
    return WorkflowProposal.model_validate(
        {
            "kind": "comparison",
            "external_ids": ["SYNTH-S5-001", "SYNTH-S5-002"],
            "models": [{"model": "constant-velocity-heading"}, {"model": "kalman-cv"}],
            **updates,
        }
    )


async def prepare(app, plan=None, *, context=None):
    context = context or ExecutionContext("scat-researcher", uuid4().hex)
    view = await app.prepare_workflow(context, plan or proposal(), proposal_key=uuid4().hex)
    return context, view


async def approve(app, plan=None):
    context, view = await prepare(app, plan)
    app.confirm(context, view["task_id"], view["snapshot_sha256"])
    return context, app.skills.store.get(context.scope_key, view["task_id"])


@pytest.mark.asyncio
async def test_forecast_checks_legacy_artifact_index_before_sampling_or_model_effects(app):
    from unittest.mock import AsyncMock

    app.artifacts.require_multipage = AsyncMock(side_effect=ValueError("legacy unique run/kind"))
    app.workflow_source.forecast_input = AsyncMock(
        side_effect=AssertionError("No sampling before storage admission")
    )
    with pytest.raises(TaskConflict, match="multipage artifact collection before approval"):
        await prepare(app, proposal(kind="forecast", observation_last_n=20))
    app.artifacts.require_multipage.assert_awaited_once()
    app.workflow_source.forecast_input.assert_not_awaited()
    with closing(app.store.connect()) as db:
        assert db.execute("SELECT count(*) FROM model_tasks").fetchone()[0] == 0


async def execute_flow(app, row, worker, generation):
    capability, session, _ = bound(app, row, worker=worker)

    async def call(name, args):
        return await app.skills.invoke(
            capability, name, args, scope=session["scope"], native_run=session["native_run"]
        )

    sample = await call("scat_sample_prepare", {})
    again = await call("scat_sample_prepare", {})
    assert sample["reference"] == again["reference"] and again["plan"] == sample["plan"]
    refs, phase = [], None
    kinds = row["snapshot"]["workflow"]["proposal"]["materializations"]
    for kind in sorted(kinds, key=lambda k: k == "prediction_metric_summaries"):
        result = await call("scat_materialize", {"reference": sample["reference"], "kind": kind})
        read = await call("scat_materialization_read", {"reference": result["reference"]})
        assert read["coverage"]["scope"] == "approved_frozen_sample"
        refs.append(result["reference"])
        if kind == "phase_segments":
            phase = result["reference"]
    trials = []
    for trial in sample["plan"]["trial_ids"]:
        args = {"reference": sample["reference"], "trial_id": trial}
        result = await call("scat_trial_run", args)
        duplicate = await call("scat_trial_run", args)
        assert duplicate["reused"] and duplicate["reference"] == result["reference"]
        trials.append(result["reference"])
    if trials:
        evaluation = await call(
            "scat_evaluation_summarize", {"references": trials + ([phase] if phase else [])}
        )
        diagnosis = await call("scat_evaluation_diagnose", {"reference": evaluation["reference"]})
        refs += [evaluation["reference"], diagnosis["reference"]]
    if row["snapshot"]["workflow"]["proposal"]["kind"] == "report":
        refs = [sample["reference"]]
    report = await call("scat_report_build", {"references": refs})
    assert completion_reference(app, row) is None
    final = await call("scat_report_read", {"reference": report["reference"]})
    assert final["analysis_result"]["schema_version"] == "1.4"
    assert completion_reference(app, row) == report["reference"]
    return final


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["forecast", "comparison", "experiment", "materialization"])
async def test_complete_workflows_publish_real_standard_results(app, kind):
    options = {"kind": kind}
    if kind == "forecast":
        options.update(observation_last_n=20, materializations=[])
    if kind == "experiment":
        options["repeats"] = 2
    if kind == "materialization":
        options.update(
            models=[],
            materializations=[
                "batch_statistics",
                "flight_features",
                "phase_segments",
                "prediction_metrics",
                "prediction_metric_summaries",
            ],
        )
    elif kind != "forecast":
        options.update(materializations=["phase_segments"], missing_phase="build")
    context, original = await approve(app, proposal(**options))
    assert await SkillWorker(app, execute_flow).once()
    view = await app.get(context, original["task_id"])
    assert view["skill_job"]["state"] == view["run"]["status"] == "succeeded"
    executions = view["skill_job"]["backend_executions"]
    assert len(executions) == len(original["snapshot"]["workflow"]["trials"]) + len(
        options["materializations"]
    )
    assert all(e["calls"] == 1 for e in executions)
    assert view["skill_job"]["budget"]["finished_at"] is not None
    if kind == "forecast":
        assert view["evaluator"] is None
        contract = view["workflow"]["prediction_contract"]
        assert contract["mode"] == "future_forecast" and contract["observation_last_n"] == 20
        assert all(w["point_count"] == 20 for w in contract["windows"])
        assert all(ref["limit"] == 20 for ref in view["input_refs"])
        assert all(ref["start_time"] and ref["end_time"] for ref in view["input_refs"])
    artifact = await app.artifact(
        context, original["task_id"], view["run"]["result_artifact_ids"][0]
    )
    assert artifact["analysis_result"]["context"]["synthetic"]
    with pytest.raises(TaskNotFound):
        await app.artifact(
            ExecutionContext(context.user_id, uuid4().hex),
            original["task_id"],
            artifact["artifact_id"],
        )


@pytest.mark.asyncio
async def test_report_reuse_does_not_execute_models(app):
    context, original = await approve(app)
    await SkillWorker(app, execute_flow).once()
    _, view = await prepare(
        app,
        proposal(
            kind="report",
            external_ids=[],
            models=[],
            source_task_ids=[original["task_id"]],
            title="复用报告",
        ),
        context=context,
    )
    app.confirm(context, view["task_id"], view["snapshot_sha256"])
    await SkillWorker(app, execute_flow).once()
    final = await app.get(context, view["task_id"])
    assert final["skill_job"]["state"] == "succeeded"
    assert final["skill_job"]["backend_executions"] == []
    artifact = await app.artifact(context, view["task_id"], final["run"]["result_artifact_ids"][0])
    assert artifact["analysis_result"]["title"] == "复用报告"


@pytest.mark.asyncio
async def test_report_reuse_rejects_incompatible_legacy_task_before_effects(app):
    context = ExecutionContext("scat-researcher", uuid4().hex)
    legacy = app.store.prepare(
        context.scope_key, uuid4().hex, {"proposal": {}, "run": {"run_id": uuid4().hex}}
    )
    plan = proposal(kind="report", external_ids=[], models=[], source_task_ids=[legacy["task_id"]])
    with pytest.raises(TaskConflict, match="verified S5"):
        await prepare(app, plan, context=context)
    assert app.skills.store.claim("premature") is None


@pytest.mark.asyncio
async def test_all_materializations_failed_publish_a_failed_report(app):
    from workflow_runtime.data import SyntheticSource

    class MissingAllPhase(SyntheticSource):
        async def get(self, batch, external_id):
            raw = await super().get(batch, external_id)
            raw["phase_points"] = []
            return raw

    app.workflow_source = MissingAllPhase()
    context, row = await approve(
        app, proposal(kind="materialization", models=[], materializations=["phase_segments"])
    )
    await SkillWorker(app, execute_flow).once()
    final = await app.get(context, row["task_id"])
    assert final["skill_job"]["state"] == final["run"]["status"] == "failed"
    assert final["run"]["error"] and len(final["run"]["result_artifact_ids"]) == 1
    saved = await app.artifact(context, row["task_id"], final["run"]["result_artifact_ids"][0])
    assert saved["analysis_result"]["context"]["outcome"] == "failed"


@pytest.mark.asyncio
async def test_approval_scope_and_snapshot_integrity(app):
    context, view = await prepare(app)
    assert app.skills.store.claim("premature") is None
    with pytest.raises(TaskConflict):
        app.confirm(context, view["task_id"], "0" * 64)
    app.confirm(context, view["task_id"], view["snapshot_sha256"])
    app.confirm(context, view["task_id"], view["snapshot_sha256"])
    row = app.skills.store.claim("worker-a")
    capability, session, _ = bound(app, row)

    async def call(name, args):
        return await app.skills.invoke(
            capability, name, args, scope=session["scope"], native_run=session["native_run"]
        )

    with pytest.raises(TaskConflict):
        await app.skills.invoke(
            capability, "scat_sample_prepare", {}, scope="forged", native_run=session["native_run"]
        )
    sample = await call("scat_sample_prepare", {})
    bad = {**sample["reference"], "content_sha256": "0" * 64}
    with pytest.raises(StepError):
        await call("scat_trial_run", {"reference": bad, "trial_id": sample["plan"]["trial_ids"][0]})
    with pytest.raises(StepError):
        await call("scat_evaluation_summarize", {"references": [sample["reference"]]})
    assert app.skills.public(row)["backend_executions"] == []


@pytest.mark.asyncio
async def test_receipt_payload_tamper_cannot_finish(app):
    _, original = await approve(app)
    row = app.skills.store.claim("worker-a")
    await execute_flow(app, row, "worker-a", row["job"]["generation"])
    with app.store.connect() as db:
        saved = db.execute(
            "SELECT id,body FROM model_step_artifacts WHERE scope=? AND run=? AND kind='prediction' LIMIT 1",
            (row["scope"], row["run_id"]),
        ).fetchone()
        body = json.loads(saved["body"])
        body["payload"]["samples"] = []
        db.execute(
            "UPDATE model_step_artifacts SET body=?,sha=? WHERE id=?",
            (json.dumps(body), digest(body), saved["id"]),
        )
    with pytest.raises(StepError):
        completion_reference(app, original)


@pytest.mark.asyncio
async def test_busy_unit_has_no_effect_and_retry_preserves_identity(app, monkeypatch):
    _, original = await approve(app)
    row = app.skills.store.claim("worker-a")
    capability, session, _ = bound(app, row)

    async def call(name, args):
        return await app.skills.invoke(
            capability, name, args, scope=session["scope"], native_run=session["native_run"]
        )

    sample = await call("scat_sample_prepare", {})
    monkeypatch.setenv("SCAT_S5_CPU_DELAY_SECONDS", "1")
    one = {"reference": sample["reference"], "trial_id": sample["plan"]["trial_ids"][0]}
    two = {"reference": sample["reference"], "trial_id": sample["plan"]["trial_ids"][1]}
    running = asyncio.create_task(call("scat_trial_run", one))
    for _ in range(100):
        if app.skills.public(row)["backend_executions"]:
            break
        await asyncio.sleep(0.02)
    with pytest.raises(StepError, match="workflow_resource_busy"):
        await call("scat_trial_run", two)
    await running
    result = await call("scat_trial_run", two)
    assert result["unit_outcome"] == "succeeded"
    assert all(b["calls"] == 1 for b in app.skills.public(original)["backend_executions"])
    assert ensure_account(app.skills.store, row).snapshot()["finished_at"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["keep_partial", "stop"])
async def test_partial_materialization_and_stop_policy(app, policy):
    from workflow_runtime.data import SyntheticSource

    class MissingPhase(SyntheticSource):
        async def get(self, batch, external_id):
            data = await super().get(batch, external_id)
            if external_id.endswith("002"):
                data["phase_points"] = []
            return data

    app.workflow_source = MissingPhase()
    context, row = await approve(
        app,
        proposal(
            kind="materialization",
            models=[],
            materializations=["phase_segments", "prediction_metrics"],
            failure_policy=policy,
        ),
    )
    await SkillWorker(app, execute_flow).once()
    view = await app.get(context, row["task_id"])
    expected = "partial" if policy == "keep_partial" else "failed"
    assert view["skill_job"]["state"] == view["run"]["status"] == expected
    assert len(view["skill_job"]["backend_executions"]) == (2 if policy == "keep_partial" else 1)
    assert view["skill_job"]["budget"]["finished_at"] is not None


@pytest.mark.asyncio
async def test_real_worker_sigkill_preserves_started_unit_and_resumes_once(app, monkeypatch):
    if sys.platform == "win32":
        pytest.skip("Linux server process-fault acceptance")
    context, _original = await approve(app)
    row = app.skills.store.claim("worker-a", lease_seconds=20)
    capability, session, _ = bound(app, row)
    sample = await app.skills.invoke(
        capability,
        "scat_sample_prepare",
        {},
        scope=session["scope"],
        native_run=session["native_run"],
    )
    args = {"reference": sample["reference"], "trial_id": sample["plan"]["trial_ids"][0]}
    code = """import asyncio,json,sys;sys.path.insert(0,'src')
from application.skill_job_factory import create_skill_tasks
app=create_skill_tasks('.',sys.argv[1])
payload=json.loads(sys.stdin.read())
asyncio.run(app.skills.invoke(payload['capability'],'scat_trial_run',payload['arguments'],scope=payload['scope'],native_run=payload['run']))
"""
    child = await asyncio.to_thread(
        subprocess.Popen,
        [sys.executable, "-B", "-c", code, str(app.store.path)],
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=os.environ | {"SCAT_S5_CPU_DELAY_SECONDS": "2", "PYTHONDONTWRITEBYTECODE": "1"},
        start_new_session=True,
        text=True,
    )
    child.stdin.write(
        json.dumps(
            {
                "capability": capability,
                "arguments": args,
                "scope": session["scope"],
                "run": session["native_run"],
            }
        )
    )
    child.stdin.close()
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            executions = app.skills.public(row)["backend_executions"]
            if executions and executions[0]["state"] == "running":
                break
            await asyncio.sleep(0.03)
        else:
            pytest.fail("Independent backend did not start")
        os.killpg(child.pid, signal.SIGKILL)
        child.wait(timeout=5)
        for _ in range(150):
            execution = app.skills.public(row)["backend_executions"][0]
            if execution["state"] == "succeeded":
                break
            await asyncio.sleep(0.03)
        assert execution["state"] == "succeeded" and execution["calls"] == 1
        await asyncio.sleep(max(0, row["job"]["lease"] - time.time() + 0.05))
        app.skills.reconcile(context, row["task_id"])
        assert await SkillWorker(app, execute_flow).once()
        final = await app.get(context, row["task_id"])
        assert final["skill_job"]["state"] == "succeeded"
        assert len(final["skill_job"]["backend_executions"]) == 2
        assert all(e["calls"] == 1 for e in final["skill_job"]["backend_executions"])
        assert final["skill_job"]["generation"] > row["job"]["generation"]
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
