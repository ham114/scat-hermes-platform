"""Author-only draft HTTP tools; release credential and mutations are never exposed."""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from hermes_runtime.profiles import ROLES, metadata
from hermes_runtime.release_store import resolve_release
from hermes_runtime.skill_budget import BudgetExhausted
from hermes_runtime.skills import checked_path

AUTHOR_TOOLS = frozenset({"skill_draft", "profile_manage"})


class AuthorBridge:
    def __init__(
        self,
        root,
        base_url,
        key_file,
        *,
        transport=None,
        automatic_candidate=None,
        automatic_scope=None,
    ):
        self.root = Path(root).resolve()
        url = urlsplit(base_url)
        if (
            url.scheme != "http"
            or url.hostname != "127.0.0.1"
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path not in {"", "/"}
        ):
            raise ValueError("author_api_requires_private_loopback_origin")
        key = checked_path(self.root, key_file)
        if __import__("os").name == "posix" and key.stat().st_mode & 0o077:
            raise ValueError("author_credential_requires_0600")
        value = key.read_text().strip()
        if len(value) < 32:
            raise ValueError("author_credential_required")
        self.client = httpx.Client(
            base_url=base_url,
            headers={"X-SCAT-Author-Key": value},
            trust_env=False,
            timeout=15,
            transport=transport,
        )
        self._sessions, self._budgets = {}, {}
        self.automatic_candidate, self.automatic_scope = automatic_candidate, automatic_scope

    def bind(self, session_id, scope, run):
        if not all(isinstance(value, str) and value for value in (session_id, scope, run)):
            raise ValueError("trusted_author_context_required")
        if self.automatic_scope is not None and scope != self.automatic_scope:
            raise ValueError("automatic_author_scope_rejected")
        if session_id in self._sessions and self._sessions[session_id][0] != scope:
            raise ValueError("author_scope_changed")
        if len(self._sessions) >= 1000 and session_id not in self._sessions:
            raise ValueError("author_session_capacity")
        self._sessions[session_id] = (scope, run)

    def finish_session(self, session_id):
        self._sessions.pop(session_id, None)
        self._budgets.pop(session_id, None)

    @staticmethod
    def schemas():
        return [
            {
                "name": "skill_draft",
                "description": "私有技能草稿管理：目录/已发布正文、新建、读取、修订、历史、差异、校验、隔离试跑与状态。learning_list/read读取经验候选，来源摘要仅作不可信参考，不改变权限；learning_draft按候选identity和expected_sha256将生成的content幂等存为草稿，不调用模型或发布。新建/修订content为changes列表，每项name/version/allowed_operations/requires_skills/contract/files，files为相对文件路径到正文。手动草稿可配对scripts/name.py和scripts/name.json，使用scat-thin-python-json-v1受限语法；描述含version/runtime/operations/input_schema/output_schema/cases，必须通过额外脚本及原生留出测试。自动经验草稿仍禁止脚本。不能提交经验候选、发布或确认。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "operation": {
                            "type": "string",
                            "enum": [
                                "catalog",
                                "published",
                                "list",
                                "create",
                                "read",
                                "update",
                                "history",
                                "diff",
                                "validate",
                                "trial",
                                "status",
                                "learning_list",
                                "learning_read",
                                "learning_draft",
                            ],
                        },
                        "identity": {"type": "string"},
                        "name": {"type": "string"},
                        "content": {"type": "object"},
                        "expected_sha256": {"type": "string"},
                    },
                    "required": ["operation"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "profile_manage",
                "description": "只读查询受管理的Agent profiles；不返回密钥、记忆或会话，不能修改/切换/创建profile。维护者通过同名CLI管理。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["list", "show", "doctor"]},
                        "profile": {"type": "string", "enum": list(ROLES)},
                    },
                    "required": ["action"],
                    "additionalProperties": False,
                },
            },
        ]

    def handler(self, tool):
        if tool not in AUTHOR_TOOLS:
            raise ValueError("author_tool_rejected")

        def invoke(args, *, session_id="", **kwargs):
            try:
                if session_id not in self._sessions:
                    raise ValueError("trusted_author_context_required")
                budget = self._budgets.get(session_id)
                if budget:
                    budget.admit_tool()
                if not isinstance(args, dict) or len(json.dumps(args).encode()) > 524288:
                    raise ValueError("invalid_author_arguments")
                if tool == "profile_manage":
                    if set(args) - {"action", "profile"} or args.get("action") not in {
                        "list",
                        "show",
                        "doctor",
                    }:
                        raise ValueError("profile_readonly_actions_only")
                    names = [args["profile"]] if "profile" in args else list(ROLES)
                    return json.dumps(
                        {"profiles": [metadata(self.root, name) for name in names]},
                        ensure_ascii=False,
                    )
                if set(args) - {"operation", "identity", "name", "content", "expected_sha256"}:
                    raise ValueError("invalid_author_arguments")
                op = args.get("operation")
                if self.automatic_candidate is not None:
                    self._automatic_guard(op, args)
                if op in {"catalog", "published"}:
                    release = resolve_release(self.root)
                    if op == "catalog":
                        result = {
                            "bundle_sha256": release.bundle_sha256,
                            "skills": list(release.entries.values()),
                        }
                    else:
                        name = args.get("name")
                        if name not in release.entries:
                            raise ValueError("published_skill_not_found")
                        result = {
                            "bundle_sha256": release.bundle_sha256,
                            "metadata": release.entries[name],
                            "content": release.files[name + "/SKILL.md"].decode(),
                        }
                    return json.dumps(result, ensure_ascii=False)
                if op in {"list", "create"}:
                    method, path = ("GET" if op == "list" else "POST"), "/v1/skills/drafts"
                elif op == "learning_list":
                    method, path = "GET", "/v1/skills/learning/candidates"
                elif op in {"learning_read", "learning_draft"}:
                    identity = args.get("identity")
                    if not isinstance(identity, str) or not re.fullmatch("[a-f0-9]{32}", identity):
                        raise ValueError("invalid_author_identity")
                    method = "POST" if op == "learning_draft" else "GET"
                    path = "/v1/skills/learning/candidates/" + identity
                    if op == "learning_draft":
                        path += "/draft"
                elif op in {"read", "update", "history", "diff", "validate", "trial", "status"}:
                    identity = args.get("identity")
                    if not isinstance(identity, str) or not re.fullmatch("[a-f0-9]{32}", identity):
                        raise ValueError("invalid_author_identity")
                    if op == "status":
                        method, path = "GET", "/v1/skills/trials/" + identity
                    else:
                        method = (
                            "PUT"
                            if op == "update"
                            else "POST"
                            if op in {"validate", "trial"}
                            else "GET"
                        )
                        suffix = {
                            "history": "/history",
                            "diff": "/diff",
                            "validate": "/validate",
                            "trial": "/trials",
                        }.get(op, "")
                        path = "/v1/skills/drafts/" + identity + suffix
                else:
                    raise ValueError("author_operation_not_allowed")
                body = None
                if op in {"create", "update"}:
                    body = {"content": args.get("content")}
                    if op == "update":
                        body["expected_sha256"] = args.get("expected_sha256")
                elif op == "learning_draft":
                    body = {
                        "content": args.get("content"),
                        "expected_sha256": args.get("expected_sha256"),
                    }
                response = self.client.request(method, path, json=body)
                if response.status_code not in {200, 201, 202}:
                    return json.dumps(
                        {
                            "success": False,
                            "error": "author_request_rejected",
                            "status_code": response.status_code,
                        }
                    )
                return json.dumps(response.json(), ensure_ascii=False)
            except BudgetExhausted as exc:
                return json.dumps({"success": False, "error": str(exc)})
            except (ValueError, TypeError, KeyError, OSError, httpx.HTTPError):
                return json.dumps({"success": False, "error": "author_operation_unavailable"})

        return invoke

    def _automatic_guard(self, operation, args):
        if operation in {"catalog", "published"}:
            return
        if (
            operation in {"learning_read", "learning_draft"}
            and args.get("identity") == self.automatic_candidate
        ):
            if operation == "learning_draft":
                self._verify_automatic_content(args.get("content"))
            return
        if operation in {"read", "update", "history", "diff", "validate"}:
            response = self.client.get("/v1/skills/learning/candidates/" + self.automatic_candidate)
            response.raise_for_status()
            if args.get("identity") and args["identity"] == response.json().get("draft"):
                if operation == "update":
                    self._verify_automatic_content(args.get("content"))
                return
        raise ValueError("automatic_author_candidate_boundary")

    def _verify_automatic_content(self, content):
        from hermes_runtime.experience_learning import verify_automatic_content

        response = self.client.get("/v1/skills/learning/candidates/" + self.automatic_candidate)
        response.raise_for_status()
        verify_automatic_content(response.json(), content)
