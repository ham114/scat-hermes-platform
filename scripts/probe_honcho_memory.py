"""Real Honcho/SDK/plugin synthetic memory checks in the isolated Conda image."""

import importlib.util
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "var/deploy/honcho"
OUT = ROOT / "var/validation/20261027-honcho/memory"
WORKSPACE = "scat-honcho-test-20261027"
OTHER = "scat-honcho-other-20261027"


def as_text(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return value
    return json.dumps(value, ensure_ascii=False, default=str)


def main():
    if not (Path(sys.prefix) / "conda-meta").is_dir() or sys.version_info[:2] != (3, 14):
        raise ValueError("candidate_native_Conda_required")
    import httpx
    from honcho import Honcho

    os.umask(0o077)
    OUT.mkdir(exist_ok=True)
    auth_only = "--auth-only" in sys.argv
    with (OUT / ("auth-started.json" if auth_only else "started.json")).open("x") as stream:
        json.dump(
            {
                "synthetic_only": True,
                "historical_import": False,
                "independent_proxy_max_requests": 60,
                "independent_proxy_cap": 500000,
                "SDK_retries": 0,
            },
            stream,
        )
    report = {"status": "running", "stage": "auth", "checks": {}, "responses": {}}
    checks = report["checks"]

    def save():
        (OUT / ("auth-recheck.json" if auth_only else "result.json")).write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        )

    def client(workspace, filename):
        return Honcho(
            base_url="http://127.0.0.1:18140",
            api_key=(DEPLOY / "secrets" / filename).read_text().strip(),
            workspace_id=workspace,
            timeout=75,
            max_retries=0,
        )

    providers = []
    try:
        admin = client(WORKSPACE, "admin.key")
        admin.get_configuration()
        client(OTHER, "admin.key").get_configuration()
        sdk = client(WORKSPACE, "workspace.key")
        sdk.get_configuration()
        denied = Honcho(
            base_url="http://127.0.0.1:18140",
            api_key="invalid-synthetic-token",
            workspace_id=WORKSPACE,
            timeout=10,
            max_retries=0,
        )
        try:
            denied.get_configuration()
        except Exception as exc:  # noqa: BLE001 -- classify SDK auth failures by status only
            checks["invalid_auth_rejected"] = getattr(exc, "status", None) == 401
        else:
            checks["invalid_auth_rejected"] = False
        try:
            client(OTHER, "workspace.key").get_configuration()
        except Exception as exc:  # noqa: BLE001 -- classify SDK scope failures by status only
            checks["workspace_token_cannot_read_other_workspace"] = getattr(
                exc, "status", None
            ) in {401, 403, 404}
        else:
            checks["workspace_token_cannot_read_other_workspace"] = False
        if auth_only:
            report.update(
                status="passed" if all(checks.values()) else "failed",
                stage="auth-verified",
                new_model_calls=0,
            )
            if not all(checks.values()):
                raise RuntimeError("authentication_recheck_failed")
            return
        token = (DEPLOY / "secrets/embedding.key").read_text().strip()
        with httpx.Client(trust_env=False, timeout=60) as http:
            checks["embedding_requires_auth"] = (
                http.post(
                    "http://127.0.0.1:18141/v1/embeddings",
                    json={"model": "bge-m3", "input": ["synthetic"]},
                ).status_code
                == 401
            )
            response = http.post(
                "http://127.0.0.1:18141/v1/embeddings",
                headers={"Authorization": "Bearer " + token},
                json={
                    "model": "bge-m3",
                    "input": ["合成用户偏好中文", "Synthetic user prefers Chinese"],
                },
            )
            response.raise_for_status()
            rows = response.json()["data"]
            checks["local_cpu_embeddings_1024"] = len(rows) == 2 and all(
                len(row["embedding"]) == 1024
                and abs(sum(x * x for x in row["embedding"]) - 1) < 0.001
                for row in rows
            )
        sys.path.insert(0, str(ROOT / "vendor/hermes-agent"))
        folder = DEPLOY / "plugin"
        spec = importlib.util.spec_from_file_location(
            "honcho_probe_plugin", folder / "__init__.py", submodule_search_locations=[str(folder)]
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        home = DEPLOY / "testing/plugin-home"
        home.mkdir(exist_ok=True)
        config = json.loads((ROOT / "config/honcho/honcho.json.example").read_text())
        config["apiKey"] = (DEPLOY / "secrets/workspace.key").read_text().strip()
        config["hosts"]["hermes_scat-main"]["workspace"] = WORKSPACE
        config["timeout"] = 75
        (home / "honcho.json").write_text(json.dumps(config))
        (home / "honcho.json").chmod(0o600)
        os.environ.update(
            HERMES_HOME=str(home),
            HERMES_HONCHO_HOST="hermes_scat-main",
            HOME=str(home),
            HERMES_DISABLE_LAZY_INSTALLS="1",
        )

        def provider(identity):
            item = module.HonchoMemoryProvider()
            providers.append(item)
            item.initialize(identity, platform="api_server", cwd=str(ROOT))
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and not item._session_ready():
                time.sleep(0.2)
            if not item._session_ready():
                raise RuntimeError("plugin_session_not_ready")
            return item

        report["stage"] = "write-and-derive"
        save()
        first = provider("honcho-synthetic-thread-a-20261027")
        checks["plugin_resolves_correct_host_and_workspace"] = (
            first._config.host == "hermes_scat-main" and first._config.workspace_id == WORKSPACE
        )
        first.sync_turn(
            "这是独立合成验收。我的偏好是简体中文简短回答，距离单位使用海里。我的测试项目代号是 HONCHO-TEST-741。",
            "已记录本次合成测试偏好。",
        )
        if first._sync_thread:
            first._sync_thread.join(timeout=60)
        peer = sdk.peer("scat-researcher")
        deadline = time.monotonic() + 180
        representation = ""
        while time.monotonic() < deadline:
            representation = peer.representation()
            if "海里" in representation or "nautical" in representation.lower():
                break
            time.sleep(3)
        report["responses"]["derived_representation"] = representation
        checks["actual_model_derived_memory"] = (
            "海里" in representation or "nautical" in representation.lower()
        ) and ("中文" in representation or "chinese" in representation.lower())
        save()
        if not checks["actual_model_derived_memory"]:
            raise RuntimeError("deriver_did_not_extract_synthetic_preferences")
        report["stage"] = "cross-session-recall"
        second = provider("honcho-synthetic-thread-b-20261027")
        checks["distinct_thread_session_keys"] = first._session_key != second._session_key
        answer = second.handle_tool_call(
            "honcho_reasoning",
            {
                "query": "请仅根据已有记忆说明这个合成用户的回复语言、回复长度和距离单位，不要猜测。",
                "peer": "user",
            },
        )
        report["responses"]["cross_session_recall"] = as_text(answer)
        checks["cross_session_recall"] = "海里" in as_text(answer) and "中文" in as_text(answer)
        save()
        if not checks["cross_session_recall"]:
            raise RuntimeError("cross_session_recall_not_verified")
        report["stage"] = "correction"
        second.sync_turn(
            "请更正本次合成测试的偏好：从现在开始距离单位改为公里，不再使用海里。简体中文简短回复和测试项目代号保持不变。",
            "已将合成测试的当前距离单位更正为公里。",
        )
        if second._sync_thread:
            second._sync_thread.join(timeout=60)
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            representation = peer.representation()
            if "公里" in representation or "kilomet" in representation.lower():
                break
            time.sleep(3)
        report["responses"]["corrected_representation"] = representation
        answer = second.handle_tool_call(
            "honcho_reasoning",
            {
                "query": "请根据最新偏好更正，只回答这个合成用户现在应使用的距离单位。",
                "peer": "user",
            },
        )
        report["responses"]["corrected_recall"] = as_text(answer)
        checks["preference_correction_recalled"] = (
            "公里" in as_text(answer) or "kilomet" in as_text(answer).lower()
        )
        save()
        if not checks["preference_correction_recalled"]:
            raise RuntimeError("corrected_preference_not_verified")
        report["stage"] = "delete-canary"
        canary = "这是可删除的独立合成记忆标记 HONCHO-DELETE-741。"
        second.handle_tool_call("honcho_conclude", {"conclusion": canary, "peer": "user"})
        listed = json.loads(
            second.handle_tool_call(
                "honcho_conclude", {"list": True, "query": "HONCHO-DELETE-741", "peer": "user"}
            )
        )
        identities = []

        def find(value):
            if isinstance(value, dict):
                if value.get("id") and "HONCHO-DELETE-741" in as_text(value):
                    identities.append(value["id"])
                for child in value.values():
                    find(child)
            elif isinstance(value, list):
                for child in value:
                    find(child)

        find(listed)
        if len(set(identities)) != 1:
            raise RuntimeError("canary_conclusion_id_not_unique")
        deleted = second.handle_tool_call(
            "honcho_conclude", {"delete_id": identities[0], "peer": "user"}
        )
        remaining = json.loads(
            second.handle_tool_call(
                "honcho_conclude", {"list": True, "query": "HONCHO-DELETE-741", "peer": "user"}
            )
        )
        checks["synthetic_conclusion_deleted"] = "deleted" in as_text(
            deleted
        ).lower() and "HONCHO-DELETE-741" not in as_text(remaining)
        report["responses"]["canary_delete"] = as_text(deleted)
        for role in ["hermes_scat-worker", "hermes_scat-author"]:
            os.environ["HERMES_HONCHO_HOST"] = role
            inactive = module.HonchoMemoryProvider()
            inactive.initialize("disabled-role-synthetic", platform="api_server")
            checks[role + "_inactive"] = inactive._config is None and inactive._manager is None
            inactive.shutdown()
        os.environ["HERMES_HONCHO_HOST"] = "hermes_scat-main"
        report["session_keys"] = [first._session_key, second._session_key]
        report["status"] = "passed" if all(checks.values()) else "failed"
        report["stage"] = "verified"
    except Exception as exc:
        report.update(status="failed", error_type=type(exc).__name__, error=str(exc)[:300])
        raise
    finally:
        for item in providers:
            item.shutdown()
        save()
        print(
            json.dumps(
                {key: value for key, value in report.items() if key != "responses"},
                ensure_ascii=False,
            )
        )
    if report["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
