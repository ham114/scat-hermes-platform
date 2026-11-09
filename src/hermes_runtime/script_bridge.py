"""Run-bound script tool; nested operations retain their original platform guards."""

import json
import sqlite3
from contextlib import closing

from hermes_runtime.results import publish_tool_result, tool_payload
from hermes_runtime.script_language import ScriptError, encode
from hermes_runtime.script_policy import load_script_policy
from hermes_runtime.script_resources import descriptors
from hermes_runtime.script_runtime import ScriptRunner, ScriptStore
from hermes_runtime.skills import digest

SCRIPT_TOOL = "skill_script_run"
SCHEMA = {
    "name": SCRIPT_TOOL,
    "description": "执行当前固定技能包中的受限Python薄脚本。先实际加载技能正文；依赖工具仍须加载对应子技能。输入来自实际用户参数或工具结果。不能传代码/解释器/环境/身份，不扩权，不发布；同一调用重试须保留request_id UUID。返回成功仅表示脚本返回，业务成功须核对实际工具回执。",
    "parameters": {
        "type": "object",
        "properties": {
            "skill": {"type": "string"},
            "script": {"type": "string", "description": "已发布scripts/<name>.py路径"},
            "inputs": {"type": "object"},
            "request_id": {"type": "string", "description": "稳定UUID"},
        },
        "required": ["skill", "script", "inputs", "request_id"],
        "additionalProperties": False,
    },
}


class ScriptBridge:
    def __init__(
        self,
        root,
        bridge,
        registry,
        results,
        state,
        *,
        job_check=None,
        job_namespace=None,
        approved_skills=None,
        approved_operations=None,
    ):
        self.root, self.bridge, self.registry, self.results = root, bridge, registry, results
        self.store = ScriptStore(state, root, recover_namespace=job_namespace)
        self.job_check, self.job_namespace = job_check, job_namespace
        self.approved_skills, self.approved_operations = approved_skills, approved_operations

    def handler(self, args, *, session_id="", **kwargs):
        try:
            if not isinstance(args, dict) or set(args) != {
                "skill",
                "script",
                "inputs",
                "request_id",
            }:
                raise ScriptError("script_arguments_invalid")
            selected = self.bridge.session_bridge(session_id)
            context = selected._sessions.get(session_id)
            if not context:
                raise ScriptError("trusted_script_context_required")
            selected._verify()
            name, path = args["skill"], args["script"]
            if not isinstance(name, str) or not isinstance(path, str):
                raise ScriptError("script_identity_invalid")
            resources = descriptors(selected.release.entries, selected.release.files)
            if (name, path) not in resources:
                raise ScriptError("script_not_published")
            descriptor = resources[name, path]
            if self.approved_skills is not None and (
                name not in self.approved_skills
                or set(descriptor["operations"]) - set(self.approved_operations)
            ):
                raise ScriptError("script_outside_approved_job")
            with closing(sqlite3.connect(selected.trace_path)) as db:
                loaded = db.execute(
                    "SELECT 1 FROM skill_loads WHERE scope=? AND run_id=? AND session_id=? AND skill_id=? AND file_path='SKILL.md' AND bundle_sha256=?",
                    (*context, session_id, name, selected.release.bundle_sha256),
                ).fetchone()
            if not loaded:
                raise ScriptError("script_skill_load_required")
            budget = self.bridge._budgets.get(session_id)
            if budget is None:
                raise ScriptError("script_parent_budget_required")

            def check():
                selected._verify()
                if budget._check(budget.snapshot()):
                    raise ScriptError("script_parent_budget_exhausted")
                if self.job_check:
                    self.job_check()

            def dispatch(operation, arguments, identity, seq):
                if operation not in descriptor["operations"]:
                    raise ScriptError("script_operation_rejected")
                # Re-enter original registry: skill load, allowlist, budget,
                # proposal/job capability and backend idempotency all still run.
                raw = self.registry.dispatch(operation, arguments, session_id=session_id)
                reference = publish_tool_result(
                    self.results,
                    scope_key=context[0],
                    run_id=context[1],
                    call_id="script:" + identity + ":" + str(seq),
                    tool=operation,
                    result=raw,
                )
                value = tool_payload(raw)
                if value is None:
                    value = json.loads(raw) if isinstance(raw, str) else raw
                encode(value)
                return value, reference

            metadata = {
                "skill": name,
                "script": path,
                "bundle_sha256": selected.release.bundle_sha256,
                "script_sha256": digest(selected.release.files[name + "/" + path]),
                "descriptor_sha256": digest(
                    selected.release.files[name + "/" + path[:-3] + ".json"]
                ),
            }
            namespace = self.job_namespace or context
            policy = load_script_policy(self.root)
            return json.dumps(
                ScriptRunner(self.root, self.store, policy).run(
                    scope=namespace[0],
                    run=namespace[1],
                    request_id=args["request_id"],
                    source=selected.release.files[name + "/" + path].decode(),
                    descriptor=descriptor,
                    inputs=args["inputs"],
                    metadata=metadata,
                    dispatch=dispatch,
                    check=check,
                    deadline=budget.snapshot()["started_at"] + budget.limits.wall_seconds,
                ),
                ensure_ascii=False,
            )
        except (ValueError, TypeError, KeyError, OSError, sqlite3.Error) as exc:
            return json.dumps(
                {
                    "success": False,
                    "error": str(exc)
                    if isinstance(exc, ScriptError)
                    else "skill_script_unavailable",
                }
            )
