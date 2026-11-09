"""Real synthetic main/worker/author profile acceptance; export only a small metadata report."""

import asyncio
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path
from uuid import uuid4

from pydantic import SecretStr

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]


async def run():
    from evaluate_skills_s6 import APP, HERMES, Evaluation, stop

    from application.context import ExecutionContext
    from hermes_runtime.client import HermesHTTPRuntime
    from hermes_runtime.registry import RunRegistry
    from hermes_runtime.skill_authoring import SECTIONS

    report_path = ROOT / "docs/validation/20261024-profiles-runtime.json"
    report = {
        "status": "running",
        "synthetic_only": True,
        "production_gpu": False,
        "profile_cases": {},
    }
    report_path.write_text(json.dumps(report, indent=2) + "\n")
    evaluation = Evaluation(acceptance_profiles=True)
    try:
        await evaluation.run()
        assert evaluation.report["status"] == "passed"
        report["flow_report"] = evaluation.report_path.relative_to(ROOT).as_posix()
        report["profile_cases"]["main"] = {
            "passed": evaluation.attempts[0]["passed"],
            "operations": len(evaluation.attempts[0]["operations"]),
        }
        report["profile_cases"]["worker"] = {
            "passed": evaluation.attempts[1]["passed"],
            "actual_backend_calls": sum(
                b["calls"] for b in evaluation.attempts[1]["backend_executions"]
            ),
        }
        report["flow_usage"] = {
            key: evaluation.report["summary"][key]
            for key in (
                "llm_calls",
                "tool_calls",
                "conservative_tokens",
                "provider_input_tokens",
                "provider_output_tokens",
                "provider_cached_tokens",
            )
        }
    except BaseException as exc:
        report.update(status="failed", stage="main_worker", error_type=type(exc).__name__)
        report_path.write_text(json.dumps(report, indent=2) + "\n")
        raise
    home = ROOT / ".tmp/profiles" / ("author-" + uuid4().hex[:8])
    home.mkdir(parents=True)
    state = (home / "catalog.sqlite").relative_to(ROOT).as_posix()
    evaluation.env = os.environ | {
        "PYTHONDONTWRITEBYTECODE": "1",
        "SCAT_SKILL_RELEASE_STATE": state,
        "TMPDIR": str(home),
        "TMP": str(home),
        "TEMP": str(home),
    }
    children = []
    try:
        children.append(
            await evaluation.launch(
                [APP, "-B", "scripts/run_skill_author_api.py", "--port", "18112"],
                "profile-author-backend",
                "http://127.0.0.1:18112/health",
            )
        )
        children.append(
            await evaluation.launch(
                [
                    HERMES,
                    "-B",
                    "scripts/run_hermes_api.py",
                    "--profile",
                    "scat-author",
                    "--home",
                    str((home / "native").relative_to(ROOT)),
                    "--port",
                    "18109",
                ],
                "profile-author-native",
                "http://127.0.0.1:18109/health",
            )
        )
        name = "scat-profile-author-check"
        body = (
            "---\nname: "
            + name
            + "\ndescription: 合成profile作者验收候选\n---\n"
            + "\n".join(
                "## " + s + "\n使用已发布查询技能；没有数据停止，所有结果注明合成范围。\n"
                for s in SECTIONS
            )
        )
        content = {
            "changes": [
                {
                    "name": name,
                    "version": "0.1.0",
                    "allowed_operations": [],
                    "requires_skills": {"scat-flight-query": "0.2.0"},
                    "contract": {
                        "version": 1,
                        "inputs": ["scat.request.v1"],
                        "outputs": ["scat.query-result.v1"],
                    },
                    "files": {"SKILL.md": body},
                }
            ]
        }
        prompt = (
            "合成作者验收：先用profile_manage查看scat-author，再查询已发布技能目录，然后用skill_draft创建一次下列精确草稿并调用validate校验。不要试跑、发布或修改profile。最后报告草稿id和实际校验状态。content="
            + json.dumps(content, ensure_ascii=False)
        )
        context = ExecutionContext("scat-researcher", "author-" + uuid4().hex)
        registry = RunRegistry(home / "bindings.sqlite", workspace_root=ROOT)
        key = SecretStr((ROOT / "var/secrets/hermes-api.key").read_text().strip())
        tools = set()
        async with HermesHTTPRuntime("http://127.0.0.1:18109", key, registry) as runtime:
            run = await runtime.create_run(context, prompt, request_id=uuid4())
            async for event in runtime.events(context, run["run_id"]):
                if event.name == "tool.started":
                    value = (
                        event.payload.get("name")
                        or event.payload.get("tool_name")
                        or event.payload.get("tool")
                    )
                    if isinstance(value, str):
                        tools.add(value)
                if event.name in {
                    "run.completed",
                    "run.failed",
                    "run.interrupted",
                    "run.cancelled",
                }:
                    assert event.name == "run.completed"
            final = await runtime.get_run(context, run["run_id"])
            assert final["status"] == "completed"
        with sqlite3.connect(ROOT / state) as db:
            assert db.execute("SELECT count(*) FROM drafts").fetchone()[0] == 1
            assert db.execute("SELECT count(*) FROM trials").fetchone()[0] == 0
            assert db.execute("SELECT count(*) FROM release_events").fetchone()[0] == 0
            row = db.execute("SELECT content FROM revisions").fetchone()
            assert json.loads(row[0]) == content
        records = [
            json.loads(p.read_text()) for p in (home / "native/sessions").glob("profile-*.json")
        ]
        assert (
            records
            and records[-1]["profile"] == "scat-author"
            and records[-1]["temperature"] == 0.4
        )
        assert set(records[-1]["tools"]) == {"profile_manage", "skill_draft"}
        with sqlite3.connect(home / "native/author-budget.sqlite") as db:
            db.row_factory = sqlite3.Row
            usage = dict(db.execute("SELECT * FROM skill_run_budgets").fetchone())
            assert usage["tool_calls"] >= 4 and usage["llm_calls"] > 0
        report["profile_cases"]["author"] = {
            "passed": True,
            "draft_created": True,
            "profile_manage_called": "profile_manage" in tools,
            "tool_policy": records[-1]["tools"],
            "temperature": records[-1]["temperature"],
            "provider_key_exposed": False,
            "publication_events": 0,
            "llm_calls": usage["llm_calls"],
            "tool_calls": usage["tool_calls"],
            "conservative_tokens": usage["charged_tokens"],
        }
        report["status"] = "passed"
    except BaseException as exc:
        report.update(status="failed", stage="author", error_type=type(exc).__name__)
        raise
    finally:
        for child in reversed(children):
            await stop(child)
        assert home.resolve().parent == (ROOT / ".tmp/profiles").resolve() and not home.is_symlink()
        for p in home.rglob("*"):
            if p.is_file() and not p.is_symlink():
                p.chmod(0o600)
        shutil.rmtree(home)
        report["temporary_removed"] = True
        report_path.write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps({"status": report["status"], "profiles_verified": len(report["profile_cases"])}),
        flush=True,
    )


if __name__ == "__main__":
    if not (Path(sys.prefix) / "conda-meta").is_dir():
        raise RuntimeError("Use existing app Anaconda")
    asyncio.run(run())
