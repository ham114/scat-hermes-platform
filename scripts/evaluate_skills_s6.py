"""Finite real-model synthetic holdout evaluation; every attempt is retained."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
APP = os.environ.get("SCAT_AGENT_PYTHON", "/home/h3c/anaconda3/envs/scat-agent/bin/python")
HERMES = os.environ.get("SCAT_HERMES_PYTHON", "/home/h3c/anaconda3/envs/scat-hermes/bin/python")


async def stop(child):
    if child and child.poll() is None:
        child.terminate()
        try:
            await asyncio.to_thread(child.wait, timeout=12)
        except subprocess.TimeoutExpired:
            child.kill()
            await asyncio.to_thread(child.wait, timeout=5)


class Evaluation:
    def __init__(self, trial=None, *, acceptance_profiles=False):
        from dotenv import dotenv_values

        from hermes_runtime.skills import SkillRelease

        self.trial = trial
        self.acceptance_profiles = acceptance_profiles
        self.identity = uuid4().hex[:10]
        self.temp_root = ROOT / (
            ".tmp/profiles" if acceptance_profiles else ".tmp/s7a" if trial else ".tmp/s6"
        )
        self.ports = (
            (18110, 18108, 18111)
            if acceptance_profiles
            else (18106, 18105, 18107)
            if trial
            else (18100, 18101, 18102)
        )
        self.home = self.temp_root / ("native-" + self.identity)
        self.home.mkdir(parents=True)
        self.values = dotenv_values(ROOT / ".env")
        self.suite_path = ROOT / "evaluations/hermes_s6_holdout_v1.json"
        self.suite = json.loads(self.suite_path.read_text())
        self.expected_domains = None
        self.required_repetitions = 3
        self.script_resources = {}
        if acceptance_profiles:
            self.suite["cases"] = [
                case
                for case in self.suite["cases"]
                if case["id"] in {"investigation-holdout", "prediction-holdout"}
            ]
            self.suite.update(min_repetitions=1, max_repetitions=1)
            self.required_repetitions = 1
            self.expected_domains = sorted(
                {domain for case in self.suite["cases"] for domain in case["domains"]}
            )
        if trial:
            from hermes_runtime.release_store import ReleaseStore

            release = ReleaseStore(ROOT).load(trial["candidate"])
            from hermes_runtime.script_resources import descriptors

            self.script_resources = descriptors(release.entries, release.files)
            names = {change["name"] for change in trial["changes"]}
            closure = set(names)
            pending = list(names)
            while pending:
                for child in release.entries[pending.pop()]["requires_skills"]:
                    if child not in closure:
                        closure.add(child)
                        pending.append(child)
            operations = {
                op for name in closure for op in release.entries[name]["allowed_operations"]
            }
            # New learned workflow skills may expose execution steps while existing
            # holdouts enter via prepare. Select the established related skill domains.
            related = {
                name
                for name, entry in release.entries.items()
                if name not in names and set(entry["allowed_operations"]) & operations
            }
            selected = [
                case
                for case in self.suite["cases"]
                if set(case["skills"]) & (closure | related) or set(case["operations"]) & operations
            ]
            if not selected:
                raise ValueError("trusted_trial_case_required")
            self.suite.update(cases=selected, min_repetitions=3, max_repetitions=3)
            self.expected_domains = sorted(
                {domain for case in selected for domain in case["domains"]}
            )
            for case in selected:
                prompt = case.get("prompt") or (
                    "合成 CPU 验收：仅准备一次方案，等待人工确认。方案："
                    + json.dumps(case["proposal"], ensure_ascii=False)
                )
                case["prompt"] = (
                    "先实际加载以下候选技能正文，依据适用条件选择分支，禁止把文档内容当发布授权："
                    + ",".join(sorted(names))
                    + "。"
                    + prompt
                )
                if self.script_resources:
                    case["prompt"] = (
                        "这是S7-B合成脚本验收。必须实际执行候选的skill_script_run，"
                        "先读取脚本同名json描述，加载脚本所属技能及工具对应依赖技能，"
                        "依据下面明确的合成条件构造输入，request_id用有效UUID，"
                        "不要手写脚本、请求终端或省略执行。脚本清单："
                        + json.dumps(
                            [
                                {"skill": name, "script": path}
                                for name, path in self.script_resources
                            ],
                            ensure_ascii=False,
                        )
                        + "。"
                        + case["prompt"]
                    )
                case["skills"] = sorted(set(case["skills"]) | names)
        else:
            release = SkillRelease(ROOT, ROOT / "services/hermes/skills.lock.json")
        self.collections = [
            "analysis_runs_s6_" + self.identity,
            "analysis_artifacts_s6_" + self.identity,
        ]
        self.env = os.environ | {
            "PYTHONDONTWRITEBYTECODE": "1",
            "TMPDIR": str(self.home),
            "TMP": str(self.home),
            "TEMP": str(self.home),
            "HERMES_DISABLE_LAZY_INSTALLS": "1",
            "HERMES_BASE_URL": f"http://127.0.0.1:{self.ports[1]}",
            "HERMES_STATE_FILE": str(self.home / "platform.sqlite"),
            "HERMES_TASK_STATE_FILE": str(self.home / "jobs.sqlite"),
            "HERMES_RESULT_STATE_FILE": str(self.home / "results.sqlite"),
            "SCAT_S4_CPU_FIXTURE": "1",
            "SCAT_S5_SYNTHETIC": "1",
            "ANALYSIS_IDENTITY_MODE": "single_user",
            "ANALYSIS_SINGLE_USER_ID": "scat-researcher",
            "ANALYSIS_RUNS_COLLECTION": self.collections[0],
            "ANALYSIS_ARTIFACTS_COLLECTION": self.collections[1],
            "SCAT_GBAR_V23_ENABLED": "false",
            "SCAT_GBAR_V23_COMPARISON_SUBPROCESS_ENABLED": "false",
            "SCAT_JOB_TEMP_DIR": str(self.home / "worker-tmp"),
        }
        if self.script_resources:
            self.env["SCAT_SKILL_SCRIPTS_ENABLED"] = "1"
        # No HERMES_*_ENABLED or SCAT_S5_WORKFLOWS override: exercise S6 defaults.
        for key in ("HERMES_SKILL_JOBS_ENABLED", "HERMES_WORKFLOWS_ENABLED", "SCAT_S5_WORKFLOWS"):
            self.env.pop(key, None)
        self.children = []
        self.attempts = []
        self.started = time.monotonic()
        self.report_path = (
            ROOT
            / "docs/validation"
            / (
                (
                    "20261024-profiles-flow-"
                    if acceptance_profiles
                    else "20261024-S7A-trial-"
                    if trial
                    else "20261023-S6-native-"
                )
                + self.identity
                + ".json"
            )
        )
        self.report = {
            "status": "running",
            "identity": self.identity,
            "suite": self.suite["identity"],
            "suite_sha256": hashlib.sha256(self.suite_path.read_bytes()).hexdigest(),
            "bundle_sha256": release.bundle_sha256,
            "bundle_version": release.manifest["bundle_version"],
            "model": self.values.get("DEFAULT_MODEL") or "deepseek-flash",
            "hermes_commit": release.manifest["upstream_commit"],
            "synthetic_only": True,
            "production_gpu": False,
            "default_profile_exercised": True,
            "limits": self.suite["suite_limits"],
            "required_domains": self.expected_domains,
            "trial_id": trial["id"] if trial else None,
            "profile_acceptance": acceptance_profiles,
            "required_repetitions": self.required_repetitions,
            "accounting": self.suite["shared_case_accounting"],
            "attempts": self.attempts,
        }
        self.save()

    def save(self):
        from hermes_runtime.stability import summarize

        self.report["summary"] = summarize(
            self.attempts,
            expected_domains=self.expected_domains,
            repetitions=self.required_repetitions,
        )
        self.report["elapsed_seconds"] = round(time.monotonic() - self.started, 2)
        self.report["script_native_runs"] = {
            name + "/" + path: sum(
                name + "/" + path in row.get("scripts_executed", [])
                for row in self.attempts
                if row["passed"]
            )
            for name, path in self.script_resources
        }
        temp = self.home / "report.json"
        temp.write_text(json.dumps(self.report, ensure_ascii=False, indent=2) + "\n")
        temp.replace(self.report_path)

    async def launch(self, command, name, health=None):
        path = ROOT / "var/validation" / ("s6-" + self.identity + "-" + name + ".log")
        with path.open("x") as log:
            path.chmod(0o600)
            child = await asyncio.to_thread(
                subprocess.Popen,
                command,
                cwd=ROOT,
                env=self.env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
        self.children.append(child)
        if health:
            async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
                for _ in range(140):
                    if child.poll() is not None:
                        raise RuntimeError(name + "_exited")
                    try:
                        if (await client.get(health)).status_code == 200:
                            return child
                    except httpx.TransportError:
                        pass
                    await asyncio.sleep(0.5)
                raise RuntimeError(name + "_health_timeout")
        return child

    def usage(self, context, run, task_id=None):
        path = self.home / "main/skill-loads.sqlite"
        budget_run = run
        if task_id and (self.home / "jobs.sqlite").exists():
            with sqlite3.connect(self.home / "jobs.sqlite") as db:
                job = db.execute(
                    "SELECT run_id FROM model_tasks WHERE task_id=?", (task_id,)
                ).fetchone()
            if job:
                path, budget_run = self.home / "jobs.sqlite", "job:" + task_id
        with sqlite3.connect(path) as db:
            db.row_factory = sqlite3.Row
            if not db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='skill_run_budgets'"
            ).fetchone():
                return {}
            row = db.execute(
                "SELECT * FROM skill_run_budgets WHERE scope=? AND run_id=?",
                (context.scope_key, budget_run),
            ).fetchone()
            # Job budgets use their persistent task run id; do not double count planners.
            if row is None and task_id:
                row = db.execute(
                    "SELECT * FROM skill_run_budgets WHERE scope=?", (context.scope_key,)
                ).fetchone()
            return dict(row) if row else {}

    def traces(self, context, parent, task_id):
        files = [self.home / "main/skill-loads.sqlite"]
        if task_id:
            files += list((self.home / "background" / task_id).glob("*/skill-loads.sqlite"))
        loads, operations = [], []
        for path in files:
            with sqlite3.connect(path) as db:
                db.row_factory = sqlite3.Row
                clause, values = (
                    (" WHERE scope=? AND run_id=?", (context.scope_key, parent))
                    if path == files[0]
                    else ("", ())
                )
                loads.extend(
                    dict(r)
                    for r in db.execute(
                        "SELECT skill_id,file_path FROM skill_loads" + clause, values
                    )
                )
                operations.extend(
                    dict(r)
                    for r in db.execute(
                        "SELECT operation,skill_id,outcome,arguments_sha256,result_sha256,load_id FROM skill_operations"
                        + clause,
                        values,
                    )
                )
        return loads, operations

    async def attempt(self, client, case, number):
        from hermes_runtime.registry import RunRegistry
        from hermes_runtime.results import ToolResultStore
        from workflow_runtime.contracts import WorkflowProposal

        row = {
            "case": case["id"],
            "repetition": number,
            "domains": case["domains"],
            "passed": False,
            "selection_parameters_passed": False,
            "stage": "start",
        }
        started = time.monotonic()
        parent = task_id = context = worker = None
        try:
            response = await client.post("/api/v1/hermes/threads", json={})
            response.raise_for_status()
            tid = response.json()["thread_id"]
            base = f"/api/v1/hermes/threads/{tid}"
            context = RunRegistry(
                self.home / "platform.sqlite", workspace_root=ROOT
            ).require_thread("scat-researcher", tid)
            prompt = case.get("prompt") or (
                "合成留出验收：请选择适用技能和目录，仅准备一次以下方案，不执行效果，不调用旧 scat_task_prepare，准备后等待人工确认。方案："
                + json.dumps(case["proposal"], ensure_ascii=False)
            )
            response = await client.post(
                base + "/runs", json={"request_id": str(uuid4()), "message": prompt}
            )
            response.raise_for_status()
            parent = response.json()["run_id"]
            row.update(stage="planner", parent_run_id=parent)
            terminal = None
            async with client.stream(
                "GET", base + "/runs/" + parent + "/events", timeout=270
            ) as events:
                events.raise_for_status()
                async for line in events.aiter_lines():
                    if line.startswith("data: "):
                        data = json.loads(line[6:])
                        if data.get("event") in {
                            "run.completed",
                            "run.failed",
                            "run.cancelled",
                            "run.interrupted",
                            "stream.error",
                        }:
                            terminal = data["event"]
                            break
            assert terminal == "run.completed", "planner_not_completed"
            response = await client.get(base + "/runs/" + parent)
            response.raise_for_status()
            output = response.json().get("output") or ""
            assert all(text in output for text in case.get("output_contains", [])), (
                "expected_synthetic_values_missing"
            )
            row["output"] = output
            if case["kind"] == "readonly":
                results = ToolResultStore(self.home / "results.sqlite", workspace_root=ROOT)
                refs = results.list(context.scope_key, parent)
                assert len(refs) >= len(case["operations"]), "actual_read_receipts_missing"
                with sqlite3.connect(results.path) as db:
                    payloads = [
                        json.loads(r[0])
                        for r in db.execute(
                            "SELECT payload FROM tool_results WHERE scope_key=? AND run_id=?",
                            (context.scope_key, parent),
                        )
                    ]
                assert all(
                    p.get("synthetic") is True and p.get("status") == "ok" for p in payloads
                ), "readonly_payload_invalid"
                by_operation = {p["operation"]: p for p in payloads}
                if "query" in case["domains"]:
                    q = by_operation["search_scat_flights"]["query"]
                    t = by_operation["get_scat_track_points"]["query"]
                    assert (
                        q["batch"] == t["batch"] == "SYNTH-S6-HOLDOUT"
                        and q["callsign"] == "QA601"
                        and q["limit"] == 1
                        and t["limit"] == 2
                        and t["external_id"] == q["batch"] + "-001"
                    ), "query_parameters_wrong"
                if "uav" in case["domains"]:
                    assert by_operation["get_uav_samples"]["query"]["limit"] == 2, (
                        "uav_parameters_wrong"
                    )
                row["actual_result_references"] = refs
            else:
                response = await client.get(base + "/model-tasks")
                response.raise_for_status()
                items = response.json()["items"]
                assert len(items) == 1 and items[0]["confirmation_required"], (
                    "single_prepared_task_required"
                )
                view = items[0]
                task_id = view["task_id"]
                row.update(task_id=task_id, stage="approval")
                if case.get("proposal"):
                    expected = WorkflowProposal.model_validate(case["proposal"]).model_dump(
                        mode="json"
                    )
                    assert view["workflow"]["proposal"] == expected, "approved_parameters_differ"
                with sqlite3.connect(self.home / "jobs.sqlite") as db:
                    assert (
                        db.execute(
                            "SELECT count(*) FROM skill_backend_executions WHERE task_id=?",
                            (task_id,),
                        ).fetchone()[0]
                        == 0
                    ), "effect_before_confirmation"
                endpoint = base + "/model-tasks/" + task_id
                for _ in range(2):
                    response = await client.post(
                        endpoint + "/confirm",
                        json={"snapshot_sha256": view["snapshot_sha256"], "confirmed": True},
                    )
                    response.raise_for_status()
                row["stage"] = "background"
                worker = await self.launch(
                    [
                        APP,
                        "-B",
                        str(ROOT / "scripts/run_model_worker.py"),
                        "--once",
                        "--job-home",
                        str((self.home / "background").relative_to(ROOT)),
                        "--background-port",
                        str(self.ports[2]),
                    ],
                    case["id"] + "-" + str(number),
                )
                deadline = time.monotonic() + 540
                while worker.poll() is None and time.monotonic() < deadline:
                    await asyncio.sleep(0.5)
                assert worker.poll() == 0, "worker_failed_or_timed_out"
                response = await client.get(endpoint)
                response.raise_for_status()
                final = response.json()
                assert final["skill_job"]["state"] == final["run"]["status"] == "succeeded", (
                    "actual_job_not_succeeded"
                )
                backends = final["skill_job"]["backend_executions"]
                expected_count = (
                    1
                    if case["kind"] == "prediction"
                    else len(final["workflow"]["trials"])
                    + len(case["proposal"].get("materializations", []))
                )
                assert len(backends) == expected_count and all(
                    b["calls"] == 1 and b["state"] == "succeeded" for b in backends
                ), "backend_count_or_state_wrong"
                artifact_id = final["run"]["result_artifact_ids"][0]
                response = await client.get(endpoint + "/artifacts/" + artifact_id)
                response.raise_for_status()
                artifact = response.json()
                analysis = artifact["analysis_result"]
                synthetic = (
                    analysis["context"].get("synthetic") is True
                    if case["kind"] != "prediction"
                    else analysis["context"].get("model_key") == "synthetic-cpu-cv"
                    and analysis["context"].get("batch") == "SYNTH-CPU"
                )
                assert analysis["schema_version"] == "1.4" and synthetic, "actual_result_invalid"
                other = await client.post("/api/v1/hermes/threads", json={})
                other.raise_for_status()
                assert (
                    await client.get(
                        endpoint.replace(tid, other.json()["thread_id"])
                        + "/artifacts/"
                        + artifact_id
                    )
                ).status_code == 404, "cross_thread_read_allowed"
                row.update(
                    backend_executions=backends,
                    result_artifact_id=artifact_id,
                    analysis_result=analysis,
                    confirmation_replay_single_job=True,
                    scope_isolation=True,
                )
            loads, operations = self.traces(context, parent, task_id)
            assert set(case["skills"]) <= {
                r["skill_id"] for r in loads if r["file_path"] == "SKILL.md"
            }, "applicable_skills_not_loaded"
            assert set(case["operations"]) <= {r["operation"] for r in operations}, (
                "required_operations_missing"
            )
            assert all(r["outcome"] == "returned" and r["result_sha256"] for r in operations), (
                "unfinished_dispatch"
            )
            row.update(
                passed=True,
                selection_parameters_passed=True,
                stage="verified",
                loads=loads,
                operations=operations,
            )
            if self.script_resources:
                with sqlite3.connect(self.home / "main/skill-scripts.sqlite") as db:
                    records = [
                        json.loads(r[0])
                        for r in db.execute(
                            "SELECT metadata FROM skill_script_runs WHERE scope=? AND run=? AND state='succeeded'",
                            (context.scope_key, parent),
                        )
                    ]
                matched = {(item["skill"], item["script"]) for item in records}
                assert set(self.script_resources) <= matched, (
                    "actual_candidate_scripts_not_executed"
                )
                row["scripts_executed"] = [name + "/" + path for name, path in sorted(matched)]
        except Exception as exc:  # noqa: BLE001 -- retain failed cases and continue the finite suite
            row.update(
                passed=False,
                error_type=type(exc).__name__,
                error_code=str(exc)
                if isinstance(exc, AssertionError)
                else "evaluation_step_failed",
            )
        finally:
            await stop(worker)
            if context and parent:
                row["usage"] = self.usage(context, parent, task_id)
            row["elapsed_seconds"] = round(time.monotonic() - started, 2)
            self.attempts.append(row)
            self.save()
            print(
                json.dumps(
                    {
                        k: row.get(k)
                        for k in (
                            "case",
                            "repetition",
                            "passed",
                            "stage",
                            "error_type",
                            "error_code",
                            "elapsed_seconds",
                        )
                    }
                ),
                flush=True,
            )

    async def cleanup(self):
        from pymongo import AsyncMongoClient

        from application.skill_cpu_backend import process_start, request_stop
        from application.skill_job_factory import create_skill_tasks

        for child in reversed(self.children):
            await stop(child)
        inputs = []
        if (self.home / "jobs.sqlite").exists():
            app = create_skill_tasks(
                ROOT,
                self.home / "jobs.sqlite",
                cpu_fixture=True,
                workflows_enabled=True,
                workflow_synthetic=True,
            )
            with app.store.connect() as db:
                rows = db.execute("SELECT scope,task_id,snapshot FROM model_tasks").fetchall()
            for row in rows:
                plan = json.loads(row["snapshot"]).get("workflow")
                if plan:
                    inputs.append(plan["input_sha256"])
                if app.skills.store.get(row["scope"], row["task_id"])["job"]:
                    app.skills.store.cancel(row["scope"], row["task_id"])
                    request_stop(app.skills.store, row["task_id"])

        def owned():
            found = []
            for p in Path("/proc").iterdir():
                if not p.name.isdecimal() or int(p.name) == os.getpid():
                    continue
                try:
                    if p.stat().st_uid != os.getuid():
                        continue
                    args = [
                        a.decode(errors="replace")
                        for a in (p / "cmdline").read_bytes().split(b"\0")
                    ]
                except (OSError, ProcessLookupError):
                    continue
                if any(
                    a in {str(self.home), str(self.home.relative_to(ROOT))}
                    or a.startswith((str(self.home) + "/", str(self.home.relative_to(ROOT)) + "/"))
                    for a in args
                ):
                    found.append((int(p.name), process_start(int(p.name))))
            return found

        for sig in (signal.SIGTERM, signal.SIGKILL):
            for pid, birth in owned():
                if birth and process_start(pid) == birth:
                    try:
                        os.kill(pid, sig)
                    except ProcessLookupError:
                        pass
            for _ in range(30):
                if not owned():
                    break
                await asyncio.sleep(0.1)
        assert not owned(), "owned_processes_remaining"
        mongo = AsyncMongoClient(self.values.get("FLIGHT_MONGO_URI") or "mongodb://127.0.0.1:27017")
        try:
            for name in self.collections:
                assert name in {
                    "analysis_runs_s6_" + self.identity,
                    "analysis_artifacts_s6_" + self.identity,
                }
                await mongo[self.values.get("FLIGHT_MONGO_DB") or "flight_data"].drop_collection(
                    name
                )
        finally:
            await mongo.close()
        for identity in inputs:
            path = ROOT / "var/workflow-inputs" / (identity + ".json")
            assert (
                path.parent.resolve() == (ROOT / "var/workflow-inputs").resolve()
                and not path.is_symlink()
            )
            path.unlink(missing_ok=True)
        self.report.update(owned_processes_remaining=0, isolated_collections_removed=True)
        self.save()
        assert self.home.resolve().parent == self.temp_root.resolve() and not self.home.is_symlink()
        shutil.rmtree(self.home)
        self.report["temporary_home_removed"] = True
        self.report_path.write_text(json.dumps(self.report, ensure_ascii=False, indent=2) + "\n")

    async def run(self):
        from hermes_runtime.stability import summarize

        try:
            for port in self.ports:
                with socket.socket() as sock:
                    sock.bind(("127.0.0.1", port))
            task = asyncio.current_task()
            asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)
            await self.launch(
                [
                    HERMES,
                    "-B",
                    str(ROOT / "scripts/run_hermes_api.py"),
                    "--home",
                    str((self.home / "main").relative_to(ROOT)),
                    "--port",
                    str(self.ports[1]),
                    "--synthetic-mcp",
                    "--s4-cpu-fixture",
                ]
                + (
                    [
                        "--with-skills",
                        "--with-mcp",
                        "--with-model-tasks",
                        "--with-workflows",
                        "--skill-bundle",
                        self.trial["candidate"],
                    ]
                    if self.trial
                    else ["--profile", "scat-main"]
                    if self.acceptance_profiles
                    else []
                ),
                "main",
                f"http://127.0.0.1:{self.ports[1]}/health",
            )
            await self.launch(
                [
                    APP,
                    "-B",
                    str(ROOT / "scripts/run_platform_api.py"),
                    "--port",
                    str(self.ports[0]),
                ],
                "api",
                f"http://127.0.0.1:{self.ports[0]}/health/live",
            )
            headers = (
                {"Authorization": "Bearer " + self.values["AUTH_SECRET"]}
                if self.values.get("AUTH_SECRET")
                else {}
            )
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{self.ports[0]}",
                headers=headers,
                trust_env=False,
                timeout=280,
            ) as client:
                catalog = await client.get("/api/v1/hermes/workflow-catalog")
                catalog.raise_for_status()
                assert catalog.json()["enabled"] and catalog.json()["synthetic"], (
                    "default_skills_profile_not_active"
                )
                for case in self.suite["cases"]:
                    for number in range(1, self.suite["max_repetitions"] + 1):
                        summary = summarize(
                            self.attempts,
                            expected_domains=self.expected_domains,
                            repetitions=self.required_repetitions,
                        )
                        cap = self.suite["suite_limits"]
                        if (
                            summary["llm_calls"] + 24 > cap["llm_calls"]
                            or summary["conservative_tokens"] + 1000000 > cap["conservative_tokens"]
                            or time.monotonic() - self.started > cap["wall_seconds"]
                        ):
                            raise RuntimeError("suite_budget_exhausted")
                        await self.attempt(client, case, number)
                        rows = [r for r in self.attempts if r["case"] == case["id"]]
                        if (
                            number >= self.suite["min_repetitions"]
                            and sum(r["passed"] for r in rows) / len(rows)
                            >= self.suite["domain_target"]
                        ):
                            break
            self.report["status"] = summarize(
                self.attempts,
                expected_domains=self.expected_domains,
                repetitions=self.required_repetitions,
            )["status"]
        except BaseException as exc:
            self.report.update(status="failed", error_type=type(exc).__name__)
            raise
        finally:
            await self.cleanup()
            print(
                json.dumps({"report": str(self.report_path), "status": self.report["status"]}),
                flush=True,
            )


if __name__ == "__main__":
    if not (Path(sys.prefix) / "conda-meta").is_dir():
        raise RuntimeError("Existing Anaconda Python required")
    evaluation = Evaluation()
    asyncio.run(evaluation.run())
    raise SystemExit(0 if evaluation.report["status"] == "passed" else 1)
