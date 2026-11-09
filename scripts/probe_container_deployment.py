"""Finite checks against the owned deployment; paid workflow requires --workflow."""

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261027-h7b"
DEPLOY = ROOT / "var/deploy/h7b"


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def key(name):
    return (DEPLOY / "secrets" / (name + ".key")).read_text().strip()


def main():
    global OUT
    if sys.executable != "/home/h3c/anaconda3/envs/scat-agent/bin/python":
        raise ValueError("existing_APP_Conda_required")
    import httpx

    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workflow", action="store_true")
    parser.add_argument("--network-recovery", action="store_true")
    args = parser.parse_args()
    prior_calls = prior_tokens = 0
    if args.network_recovery:
        if not args.workflow:
            raise ValueError("network_recovery_requires_workflow")
        previous = json.loads((OUT / "workflow-result.json").read_text())
        if previous["status"] != "failed" or previous.get("task_id"):
            raise ValueError("only_failed_planner_without_effects_can_be_replaced")
        prior_calls = sum(row["llm_calls"] for row in previous["main_budgets"])
        prior_tokens = sum(row["charged_tokens"] for row in previous["main_budgets"])
        if prior_calls + 48 > 72 or prior_tokens + 1600000 > 3000000:
            raise ValueError("campaign_budget_insufficient")
        OUT = OUT / "workflow-network-v2"
        OUT.mkdir(exist_ok=True)
    report = {"checks": {}, "model_requests": 0}
    checks = report["checks"]
    with httpx.Client(trust_env=False, timeout=90) as http:
        for service, port, route in [
            ("platform", 18120, "/health/ready"),
            ("hermes-main", 18121, "/health"),
            ("hermes-worker", 18122, "/health"),
            ("actions", 18123, "/health"),
            ("worker", 18126, "/health"),
            ("author-api", 18124, "/health"),
            ("hermes-author", 18125, "/health"),
        ]:
            response = http.get(f"http://127.0.0.1:{port}" + route)
            response.raise_for_status()
            checks[service + "_health"] = True
        prefix = "http://127.0.0.1:18120/api/v1/hermes"
        auth = {"Authorization": "Bearer " + key("platform-auth")}
        checks["platform_requires_auth"] = http.get(prefix + "/threads").status_code == 401
        catalog = http.get(prefix + "/workflow-catalog", headers=auth)
        catalog.raise_for_status()
        report["workflow_catalog"] = catalog.json()
        checks["workflow_enabled"] = catalog.json()["enabled"]
        action = "http://127.0.0.1:18123"
        headers = {
            "Authorization": "Bearer " + key("rpc-main"),
            "Accept": "application/json, text/event-stream",
        }
        checks["mcp_requires_auth"] = http.post(action + "/mcp/", json={}).status_code == 401

        def mcp(method, params):
            result = http.post(
                action + "/mcp/",
                headers=headers,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            )
            result.raise_for_status()
            assert "result" in result.json(), "mcp_protocol_error"
            return result.json()["result"]

        mcp(
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "h7b", "version": "1"},
            },
        )
        tools = mcp("tools/list", {})["tools"]
        report["mcp_tools"] = [row["name"] for row in tools]
        result = mcp("tools/call", {"name": "search_scat_flights", "arguments": {"limit": 1}})
        checks["mcp_real_business_query"] = not result.get("isError", False)
        save(OUT / "mcp-business-result.json", result)
        result = mcp(
            "tools/call",
            {
                "name": "search_scat_knowledge",
                "arguments": {"query": "SCAT 数据集事实 SCAT_DATASET.md", "limit": 3},
            },
        )
        checks["mcp_knowledge_query"] = not result.get("isError", False)
        save(OUT / "mcp-knowledge-result.json", result)
        forbidden = http.post(
            action + "/v1/actions/steps",
            headers=headers,
            json={
                "operation": "scat_model_run",
                "arguments": {},
                "scope": "probe",
                "source_run_id": "probe",
                "job_session": "forged",
            },
        )
        checks["main_cannot_run_worker_steps"] = forbidden.status_code == 403
        checks["job_controller_requires_auth"] = (
            http.post("http://127.0.0.1:18122/v1/jobs/start", json={}).status_code == 401
        )
        author = "http://127.0.0.1:18124/v1/skills"
        checks["author_requires_independent_key"] = http.get(author + "/drafts").status_code == 401
        author_headers = {"X-Scat-Author-Key": key("author")}
        release = http.get(author + "/releases", headers=author_headers)
        release.raise_for_status()
        report["active_release"] = release.json()["active"]
        checks["author_cannot_confirm_release"] = (
            http.post(
                author + "/releases/confirm",
                headers=author_headers,
                json={"preview": "untrusted", "sha256": "a" * 64, "confirmed": True},
            ).status_code
            == 403
        )
        report["external_services"] = {}
        for name, origin, route in [
            ("rag", "http://127.0.0.1:18133", "/health"),
            ("gbar", "http://172.30.67.2:3000", "/ready"),
        ]:
            result = http.get(origin + route)
            checks[name + "_external_ready"] = result.status_code == 200
            report["external_services"][name] = {"origin": origin, "status": result.status_code}
        assert all(checks.values()), "nonpaid_deployment_check_failed"
        report["status"] = "passed"
        save(OUT / "integration-readonly.json", report)
        if not args.workflow:
            print(json.dumps(report, ensure_ascii=False))
            return
        assert catalog.json()["synthetic"], "requires_isolated_smoke_overlay"
        marker = OUT / "workflow-started.json"
        with marker.open("x") as stream:
            json.dump(
                {
                    "case": "one synthetic comparison, one main planner plus one approved child",
                    "max_model_requests": 72,
                    "max_conservative_tokens": 3000000,
                    "automatic_paid_retry": False,
                    "prior_requests": prior_calls,
                    "prior_conservative": prior_tokens,
                    "this_main_ceiling": {"max_turns": 24, "max_llm_tokens": 600000},
                    "inputs": ["SYNTH-S5-001", "SYNTH-S5-002"],
                },
                stream,
                indent=2,
            )
        evidence = {"status": "running", "stage": "planner"}
        path = OUT / "workflow-result.json"
        save(path, evidence)
        try:
            response = http.post(prefix + "/threads", headers=auth, json={})
            response.raise_for_status()
            tid = response.json()["thread_id"]
            base = prefix + "/threads/" + tid
            response = http.post(
                base + "/runs",
                headers=auth,
                json={
                    "request_id": str(uuid4()),
                    "message": '这是合成数据验收。先 skills_list，加载 scat-model-comparison 正文并查询 scat_workflow_catalog，然后仅准备一次以下比较方案：kind=comparison，batch=SYNTH-S5，external_ids=[SYNTH-S5-001,SYNTH-S5-002]，models=[{"model":"constant-velocity-heading"},{"model":"kalman-cv"}]，materializations=["phase_segments"]，missing_phase=build，title=合成双模型对比。其他参数用默认值。只准备并给出页面确认提示，不执行计算，不查询真实业务数据，不使用旧 scat_task_prepare。',
                },
            )
            response.raise_for_status()
            rid = response.json()["run_id"]
            evidence.update(thread_id=tid, parent_run_id=rid)
            save(path, evidence)
            with http.stream(
                "GET", base + "/runs/" + rid + "/events", headers=auth, timeout=650
            ) as events:
                events.raise_for_status()
                lines = []
                for line in events.iter_lines():
                    if line.startswith("data: "):
                        value = json.loads(line[6:])
                        lines.append(value)
                        if value.get("event") in {
                            "run.completed",
                            "run.failed",
                            "run.interrupted",
                            "run.cancelled",
                            "stream.error",
                        }:
                            break
                save(OUT / "workflow-planner-events.json", lines)
                assert lines and lines[-1].get("event") == "run.completed", "planner_not_complete"
            result = http.get(base + "/model-tasks", headers=auth)
            result.raise_for_status()
            items = result.json()["items"]
            assert len(items) == 1 and "workflow" in items[0], "planner_workflow_missing"
            view = items[0]
            endpoint = base + "/model-tasks/" + view["task_id"]
            evidence.update(task_id=view["task_id"], stage="approved-child")
            save(path, evidence)
            for _ in range(2):
                result = http.post(
                    endpoint + "/confirm",
                    headers=auth,
                    json={"snapshot_sha256": view["snapshot_sha256"], "confirmed": True},
                )
                result.raise_for_status()
            deadline = time.monotonic() + 1050
            while time.monotonic() < deadline:
                response = http.get(endpoint, headers=auth)
                response.raise_for_status()
                final = response.json()
                job = final.get("skill_job") or {}
                save(OUT / "workflow-task.json", final)
                if job.get("state") in {"succeeded", "failed", "cancelled", "unknown"}:
                    break
                time.sleep(2)
            assert final["skill_job"]["state"] == final["run"]["status"] == "succeeded", (
                "approved_workflow_not_verified"
            )
            backends = final["skill_job"]["backend_executions"]
            assert len(backends) == 3 and all(
                row["calls"] == 1 and row["state"] == "succeeded" for row in backends
            ), "backend_execution_not_exactly_once"
            artifact_id = final["run"]["result_artifact_ids"][0]
            response = http.get(endpoint + "/artifacts/" + artifact_id, headers=auth)
            response.raise_for_status()
            artifact = response.json()
            save(OUT / "workflow-artifact.json", artifact)
            assert (
                artifact["analysis_result"]["schema_version"] == "1.4"
                and artifact["analysis_result"]["context"]["synthetic"]
            )
            evidence.update(
                status="passed",
                stage="verified",
                duplicate_confirmation_single_job=True,
                result_artifact_id=artifact_id,
                backends=backends,
            )
        except Exception as exc:
            evidence.update(status="failed", error_type=type(exc).__name__, error=str(exc)[:300])
            raise
        finally:
            budgets = []
            file = DEPLOY / "shared/homes/main/skill-loads.sqlite"
            with sqlite3.connect(file) as db:
                db.row_factory = sqlite3.Row
                budgets = [
                    dict(row)
                    for row in db.execute(
                        "SELECT run_id,llm_calls,charged_tokens,exhausted FROM skill_run_budgets"
                    )
                ]
            evidence["main_budgets"] = budgets
            save(path, evidence)
            print(json.dumps(evidence, ensure_ascii=False))


if __name__ == "__main__":
    main()
