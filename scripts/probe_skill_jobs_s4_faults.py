"""Deterministic process faults against the real durable CPU backend; no external LLM/DB."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def app_for(path):
    from application.skill_job_factory import create_skill_tasks

    # This probe exercises compute/receipt persistence; the real API probe covers Mongo publishing.
    class Runs:
        async def get_for_owner(self, *args, **kwargs):
            return None

    return create_skill_tasks(ROOT, path, cpu_fixture=True, runs=Runs(), artifacts=object())


async def invoke(app, cap, name, args):
    from hermes_runtime.job_budget import JobBudget

    session = app.skills.store.session(cap, operation=name)
    with app.store.connect() as db:
        raw = db.execute(
            "SELECT scope FROM model_tasks WHERE task_id=?", (session["task_id"],)
        ).fetchone()
    row = app.skills.store.get(raw[0], session["task_id"])
    JobBudget(app.skills.store, row, session).admit_tool()
    return await app.skills.invoke(
        cap, name, args, scope=session["scope"], native_run=session["native_run"]
    )


async def actor(path, cap, mode):
    from application.model_step_store import ModelStepStore

    app = app_for(path)
    prepared = await invoke(app, cap, "scat_model_prepare_input", {})
    args = {"reference": prepared["reference"]}
    if mode == "before_effect":
        session = app.skills.store.session(cap)
        with app.store.connect() as db:
            task = db.execute(
                "SELECT * FROM model_tasks WHERE task_id=?", (session["task_id"],)
            ).fetchone()
        ModelStepStore(path, workspace_root=ROOT).reserve(
            task["scope"], task["run_id"], "scat_model_run", args
        )
        os._exit(87)
    if mode == "commit_lost":
        original = ModelStepStore.save

        def lost(self, *args, **kwargs):
            if args[3] == "prediction":
                raise OSError("synthetic lost step commit")
            return original(self, *args, **kwargs)

        ModelStepStore.save = lost
    await invoke(app, cap, "scat_model_run", args)
    if mode == "reply_lost":
        os._exit(87)


def approve(app, *, budget=None):
    from application.context import ExecutionContext
    from application.model_tasks import TaskProposal
    from scat_model_runtime import ModelTaskRequest

    context = ExecutionContext("scat-researcher", uuid4().hex)
    request = ModelTaskRequest.model_validate_json(app.skills.kernel()[1].request_json)
    proposal = TaskProposal(**{n: getattr(request, n) for n in TaskProposal.model_fields})
    view = app.prepare(context, proposal, proposal_key=uuid4().hex, budget=budget)
    app.confirm(context, view["task_id"], view["snapshot_sha256"])
    row = app.skills.store.claim("fault-worker-1")
    return context, row


def bind(app, row, worker):
    from application.context import ExecutionContext

    child = ExecutionContext("scat-researcher", "child-" + uuid4().hex)
    grant = app.skills.store.delegate(
        row["scope"], row["task_id"], child.scope_key, row["snapshot"]["skills"]["operations"]
    )
    return app.skills.store.bind(
        grant, child.scope_key, "run_" + uuid4().hex, uuid4().hex, worker, row["job"]["generation"]
    )


def executions(app):
    with app.store.connect() as db:
        return [dict(r) for r in db.execute("SELECT * FROM skill_backend_executions")]


def expire(app, row):
    with app.store.connect() as db:
        db.execute("UPDATE skill_jobs SET lease=0 WHERE task_id=?", (row["task_id"],))
        db.execute("UPDATE model_tasks SET lease=0 WHERE task_id=?", (row["task_id"],))


async def complete(app, cap):
    args = {}
    for operation in (
        "scat_model_prepare_input",
        "scat_model_run",
        "scat_model_evaluate",
        "scat_model_save_result",
        "scat_model_read_result",
    ):
        result = await invoke(app, cap, operation, args)
        args = {"reference": result["reference"]}
    return result


async def wait_backend(app, states, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = executions(app)
        if rows and rows[0]["state"] in states:
            return rows[0]
        await asyncio.sleep(0.05)
    raise RuntimeError("backend_probe_timeout")


async def probe(report):
    from application.task_store import TaskConflict
    from hermes_runtime.skill_budget import BudgetExhausted

    identity = uuid4().hex[:10]
    home = ROOT / ".tmp/s4" / ("faults-" + identity)
    home.mkdir(parents=True)
    children = []
    apps = []
    cases = []
    report["cases"] = cases
    try:
        for mode in ["before_effect", "during_compute", "commit_lost", "reply_lost", "cancel"]:
            folder = home / mode
            folder.mkdir()
            app = app_for(folder / "jobs.sqlite")
            apps.append(app)
            context, row = approve(app)
            cap = bind(app, row, "fault-worker-1")
            log = ROOT / "var/validation" / ("s4-fault-" + identity + "-" + mode + ".log")
            with log.open("x") as stream:
                log.chmod(0o600)
                env = os.environ | {
                    "SCAT_S4_CPU_DELAY_SECONDS": "3"
                    if mode in {"during_compute", "cancel"}
                    else "0",
                    "TMPDIR": str(folder),
                    "TMP": str(folder),
                    "TEMP": str(folder),
                    "PYTHONDONTWRITEBYTECODE": "1",
                }
                child = await asyncio.to_thread(
                    subprocess.Popen,
                    [
                        sys.executable,
                        "-B",
                        str(Path(__file__).resolve()),
                        "--actor",
                        mode,
                        "--state",
                        str(app.store.path),
                        "--capability",
                        cap,
                    ],
                    cwd=ROOT,
                    env=env,
                    stdout=stream,
                    stderr=stream,
                )
            children.append(child)
            if mode in {"during_compute", "cancel"}:
                await wait_backend(app, {"running"})
                if mode == "during_compute":
                    child.kill()
                    await asyncio.to_thread(child.wait, timeout=10)
                    expire(app, row)
                    assert (
                        app.skills.store.get(context.scope_key, row["task_id"])["job"]["state"]
                        == "unknown"
                    )
                    assert app.skills.store.claim("premature-worker") is None
                    await wait_backend(app, {"succeeded"})
                else:
                    app.skills.store.cancel(context.scope_key, row["task_id"])
                    for _ in range(100):
                        app.skills.reconcile(context, row["task_id"])
                        if executions(app)[0]["state"] == "cancelled":
                            break
                        await asyncio.sleep(0.05)
                    final = app.skills.reconcile(context, row["task_id"])
                    assert final["job"]["state"] == "cancelled" and executions(app)[0]["calls"] == 1
                    if child.poll() is None:
                        await asyncio.to_thread(child.wait, timeout=10)
                    cases.append(
                        {
                            "case": mode,
                            "status": "passed",
                            "backend_calls": 1,
                            "actual_stop_acknowledged": True,
                        }
                    )
                    continue
            else:
                await asyncio.to_thread(child.wait, timeout=20)
                assert child.returncode == 87
                expire(app, row)
            with contextlib_suppress(TaskConflict):
                # A stale caller is rejected before starting an additional effect.
                try:
                    app.skills.store.session(cap)
                except TaskConflict:
                    pass
                else:
                    raise AssertionError("old capability survived lease loss")
            app.skills.reconcile(context, row["task_id"])
            restored = app_for(app.store.path)  # independent controller instance (worker restart)
            resumed = restored.skills.store.claim("fault-worker-2")
            assert resumed and resumed["job"]["generation"] > row["job"]["generation"]
            result = await complete(restored, bind(restored, resumed, "fault-worker-2"))
            assert executions(restored)[0]["calls"] == 1
            assert result["analysis_result"]["schema_version"] == "1.4"
            cases.append(
                {
                    "case": mode,
                    "status": "passed",
                    "backend_calls": 1,
                    "generation_advanced": True,
                    "result_reference": result["reference"],
                }
            )
        folder = home / "budget"
        folder.mkdir()
        app = app_for(folder / "jobs.sqlite")
        apps.append(app)
        context, row = approve(app, budget={"max_tool_calls": 2})
        cap = bind(app, row, "fault-worker-1")
        first = await invoke(app, cap, "scat_model_prepare_input", {})
        prediction = await invoke(app, cap, "scat_model_run", {"reference": first["reference"]})
        try:
            await invoke(app, cap, "scat_model_evaluate", {"reference": prediction["reference"]})
        except BudgetExhausted:
            pass
        else:
            raise AssertionError("budget did not stop child")
        app.skills.store.state(row["scope"], row["task_id"], "unknown")
        assert app.skills.reconcile(context, row["task_id"])["job"]["state"] == "paused"
        app.skills.revise_budget(context, row["task_id"], {"max_tool_calls": 12})
        resumed = app.skills.store.claim("fault-worker-2")
        await complete(app, bind(app, resumed, "fault-worker-2"))
        assert executions(app)[0]["calls"] == 1
        cases.append(
            {
                "case": "budget_revision_resume",
                "status": "passed",
                "backend_calls": 1,
                "budget_revision": 1,
                "usage_was_not_reset": True,
            }
        )
        with app.store.connect() as db:
            db.execute("UPDATE skill_backend_executions SET output_sha=?", ("0" * 64,))
        app.skills.store.state(row["scope"], row["task_id"], "unknown")
        try:
            app.skills.reconcile(context, row["task_id"])
        except TaskConflict:
            pass
        else:
            raise AssertionError("tampered backend receipt trusted")
        assert app.skills.store.get(context.scope_key, row["task_id"])["job"]["state"] == "unknown"
        cases.append(
            {
                "case": "backend_receipt_tamper",
                "status": "passed",
                "backend_calls": 1,
                "unknown_held": True,
            }
        )
        report.update(
            status="passed",
            cases=cases,
            synthetic=True,
            real_cpu_backend=True,
            real_llm=False,
            real_database_queries=0,
            production_gpu=False,
        )
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                await asyncio.to_thread(child.wait, timeout=10)
        for app in apps:
            for record in executions(app):
                if record["state"] == "running":
                    with app.store.connect() as db:
                        raw = db.execute(
                            "SELECT scope FROM model_tasks WHERE task_id=?", (record["task_id"],)
                        ).fetchone()
                    app.skills.store.cancel(raw[0], record["task_id"])
                    from application.context import ExecutionContext

                    row = app.skills.store.get(raw[0], record["task_id"])
                    context = ExecutionContext(
                        row["snapshot"]["run"]["owner_user_id"], row["snapshot"]["run"]["thread_id"]
                    )
                    for _ in range(100):
                        app.skills.reconcile(context, record["task_id"])
                        if not any(e["state"] == "running" for e in executions(app)):
                            break
                        await asyncio.sleep(0.05)
                assert not any(e["state"] == "running" for e in executions(app)), (
                    "backend still active; keep temp evidence"
                )
        assert home.resolve().is_relative_to((ROOT / ".tmp/s4").resolve()) and not home.is_symlink()
        shutil.rmtree(home)
        report["temporary_home_removed"] = not home.exists()


def contextlib_suppress(*types):
    from contextlib import suppress

    return suppress(*types)


if __name__ == "__main__":
    if not (Path(sys.prefix) / "conda-meta").is_dir():
        raise RuntimeError("Use existing Anaconda")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--actor")
    parser.add_argument("--state")
    parser.add_argument("--capability")
    args = parser.parse_args()
    if args.actor:
        try:
            asyncio.run(actor(Path(args.state), args.capability, args.actor))
        except BaseException:  # noqa: BLE001 -- deliberate process-fault sentinel
            os._exit(87)
    else:
        report = {"status": "running"}
        try:
            asyncio.run(probe(report))
        except BaseException as exc:
            report.update(status="failed", error_type=type(exc).__name__)
            raise
        finally:
            path = ROOT / "docs/validation" / ("20261023-S4-faults-" + uuid4().hex[:10] + ".json")
            with path.open("x", encoding="utf-8") as stream:
                json.dump(report, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            print(json.dumps({"report": str(path), "status": report["status"]}))
