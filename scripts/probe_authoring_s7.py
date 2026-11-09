"""Finite real author HTTP lifecycle on synthetic data and a separate release store."""

import asyncio
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
APP = "/home/h3c/anaconda3/envs/scat-agent/bin/python"


async def run():
    from hermes_runtime.release_store import ReleaseStore
    from hermes_runtime.skill_authoring import SECTIONS

    identity = uuid4().hex[:10]
    home = ROOT / ".tmp/s7a" / ("accept-" + identity)
    home.mkdir(parents=True)
    for filename in ("author.key", "release.key"):
        path = home / filename
        path.write_text(secrets.token_urlsafe(40))
        path.chmod(0o600)
    author, release = [(home / f).read_text() for f in ("author.key", "release.key")]
    state = (home / "catalog.sqlite").relative_to(ROOT).as_posix()
    env = os.environ | {
        "SCAT_SKILL_RELEASE_STATE": state,
        "SCAT_AUTHOR_KEY_FILE": (home / "author.key").relative_to(ROOT).as_posix(),
        "SCAT_AUTHOR_RELEASE_KEY_FILE": (home / "release.key").relative_to(ROOT).as_posix(),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    env.update({k: str(home) for k in ("TMP", "TEMP", "TMPDIR")})
    report = {
        "identity": identity,
        "status": "running",
        "synthetic_only": True,
        "production_publication": False,
        "frontend_changed": False,
    }
    report_path = ROOT / "docs/validation" / ("20261024-S7A-lifecycle-" + identity + ".json")

    def save():
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    save()
    with (ROOT / "var/validation" / ("s7a-author-api-" + identity + ".log")).open("x") as log:
        Path(log.name).chmod(0o600)
        child = await asyncio.to_thread(
            subprocess.Popen,
            [APP, "-B", "scripts/run_skill_author_api.py", "--port", "18104"],
            cwd=ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    try:
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:18104", trust_env=False, timeout=30
        ) as client:
            for _ in range(100):
                if child.poll() is not None:
                    raise RuntimeError("author_api_exited")
                try:
                    if (await client.get("/health")).status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.2)
            assert (await client.get("/v1/skills/drafts")).status_code == 401
            headers = {"X-SCAT-Author-Key": author}
            name = "scat-author-investigation"
            instructions = {
                "适用条件": "合成 SCAT 查询、质量和轨迹组合调查。",
                "必要参数": "批次、呼号、查询上限和轨迹点上限，缺参补问。",
                "输入输出": "scat.request.v1 → scat.evidence-report.v1。",
                "前置条件": "仅使用已发布且精确版本匹配的三个子技能。",
                "允许操作": "本技能直接权限为空。显式加载子技能正文后才调用其允许操作。",
                "执行流程": "加载 scat-flight-query 后查询指定批次/呼号；加载 scat-data-quality 检查同批次质量；加载 scat-trajectory-analysis，用查询实际返回标识读取轨迹。仅执行用户请求分支，无数据停止。",
                "完成条件": "工具实际成功；汇总返回点数、米制高度、质量缺失点数，注明合成和范围。",
                "失败处理": "缺参补问；空结果停止航班分支；失败保留证据、不伪造数值和完成状态。",
                "示例": "SYNTH-S6-HOLDOUT/QA601：查询 limit=1，轨迹 limit=2。",
                "评测": "固定合成留出题三次；真实原生工具回执、参数范围、正文加载和数值均须通过。",
            }
            body = (
                "---\nname: "
                + name
                + "\ndescription: 受控创作的复杂组合调查候选\n---\n\n"
                + "\n".join("## " + s + "\n\n" + instructions[s] + "\n" for s in SECTIONS)
            )
            content = {
                "changes": [
                    {
                        "name": name,
                        "version": "0.1.0",
                        "allowed_operations": [],
                        "requires_skills": {
                            s: "0.2.0"
                            for s in (
                                "scat-flight-query",
                                "scat-data-quality",
                                "scat-trajectory-analysis",
                            )
                        },
                        "contract": {
                            "version": 1,
                            "inputs": ["scat.request.v1"],
                            "outputs": ["scat.evidence-report.v1"],
                        },
                        "files": {"SKILL.md": body},
                    }
                ]
            }
            response = await client.post(
                "/v1/skills/drafts", headers=headers, json={"content": content}
            )
            response.raise_for_status()
            draft = response.json()
            endpoint = "/v1/skills/drafts/" + draft["draft"]
            content["changes"][0]["files"]["SKILL.md"] += (
                "\n只报告实际工具证据，不输出发布成功声明。\n"
            )
            response = await client.put(
                endpoint,
                headers=headers,
                json={"content": content, "expected_sha256": draft["sha"]},
            )
            response.raise_for_status()
            latest = response.json()
            assert latest["revision"] == 2
            assert (
                len((await client.get(endpoint + "/history", headers=headers)).json()["items"]) == 2
            )
            assert (await client.post(endpoint + "/validate", headers=headers)).json()["valid"]
            diff = (await client.get(endpoint + "/diff", headers=headers)).json()
            assert len(diff["files"]) == 1
            initial = ReleaseStore(ROOT, state).pin(
                "acceptance-scope", "in-flight-before-publication"
            )
            response = await client.post(endpoint + "/trials", headers=headers)
            response.raise_for_status()
            trial = response.json()
            report.update(
                draft=draft["draft"],
                revision=2,
                trial=trial["id"],
                base=initial.bundle_sha256,
                stage="real_native_trial",
            )
            save()
            deadline = time.monotonic() + 2400
            while time.monotonic() < deadline:
                await asyncio.sleep(3)
                response = await client.get("/v1/skills/trials/" + trial["id"], headers=headers)
                response.raise_for_status()
                trial = response.json()
                if trial["state"] != "running":
                    break
            report["trial_state"] = trial["state"]
            report["trial_report"] = trial.get("report")
            save()
            assert trial["state"] == "passed", "candidate_trial_not_passed"
            response = await client.post(
                "/v1/skills/releases/preview",
                headers=headers,
                json={"draft": draft["draft"], "trial": trial["id"]},
            )
            response.raise_for_status()
            preview = response.json()
            confirmation = {
                "preview": preview["id"],
                "sha256": preview["sha256"],
                "confirmed": True,
            }
            assert (
                await client.post("/v1/skills/releases/confirm", headers=headers, json=confirmation)
            ).status_code == 403
            trusted = headers | {"X-SCAT-Release-Key": release}
            response = await client.post(
                "/v1/skills/releases/confirm", headers=trusted, json=confirmation
            )
            response.raise_for_status()
            assert response.json()["target"] == trial["candidate"]
            assert (
                await client.post("/v1/skills/releases/confirm", headers=trusted, json=confirmation)
            ).json()["replayed"]
            releases = ReleaseStore(ROOT, state)
            assert (
                releases.pin("acceptance-scope", "in-flight-before-publication").bundle_sha256
                == initial.bundle_sha256
            )
            assert (
                releases.pin("acceptance-scope", "after-publication").bundle_sha256
                == trial["candidate"]
            )
            response = await client.post(
                "/v1/skills/releases/preview",
                headers=headers,
                json={"rollback": initial.bundle_sha256},
            )
            response.raise_for_status()
            rollback = response.json()
            response = await client.post(
                "/v1/skills/releases/confirm",
                headers=trusted,
                json={"preview": rollback["id"], "sha256": rollback["sha256"], "confirmed": True},
            )
            response.raise_for_status()
            assert releases.current().bundle_sha256 == initial.bundle_sha256
            assert (
                releases.pin("acceptance-scope", "after-publication").bundle_sha256
                == trial["candidate"]
            )
            report.update(
                status="passed",
                stage="verified",
                revision_history=True,
                independent_release_authority=True,
                confirmation_replay_idempotent=True,
                run_pins_preserved=True,
                rollback_passed=True,
            )
            save()
    except BaseException as exc:
        report.update(
            status="failed",
            error_type=type(exc).__name__,
            error_code=str(exc) if isinstance(exc, AssertionError) else "acceptance_failed",
        )
        save()
        raise
    finally:
        if child.poll() is None:
            child.terminate()
            await asyncio.to_thread(child.wait, timeout=45)
        if report["status"] == "passed":
            assert home.resolve().parent == (ROOT / ".tmp/s7a").resolve() and not home.is_symlink()
            for p in home.rglob("*"):
                if p.is_file():
                    p.chmod(0o600)
            shutil.rmtree(home)
            report["temporary_home_removed"] = True
        else:
            # Private evidence stays on the server until explicit final cleanup.
            for name in ("author.key", "release.key"):
                (home / name).unlink(missing_ok=True)
        save()
    print(
        json.dumps(
            {"status": report["status"], "report": report_path.relative_to(ROOT).as_posix()}
        ),
        flush=True,
    )


if __name__ == "__main__":
    if sys.executable != APP or not (Path(sys.prefix) / "conda-meta").is_dir():
        raise RuntimeError("Use existing scat-agent Anaconda")
    asyncio.run(run())
