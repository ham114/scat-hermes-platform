"""Exercise orphan-profile cleanup after a synthetic trial worker interruption, no LLM."""

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]


async def run():
    from evaluate_skills_s6 import Evaluation

    from hermes_runtime.author_trials import recover_interrupted
    from hermes_runtime.authoring_store import AuthoringStore
    from hermes_runtime.skill_authoring import SECTIONS

    home = ROOT / ".tmp/s7a" / ("recovery-" + uuid4().hex[:8])
    home.mkdir(parents=True)
    state = (home / "catalog.sqlite").relative_to(ROOT).as_posix()
    os.environ["SCAT_SKILL_RELEASE_STATE"] = state
    store = AuthoringStore(ROOT)
    name = "scat-recovery-query"
    body = (
        "---\nname: "
        + name
        + "\ndescription: 合成恢复候选\n---\n"
        + "\n".join("## " + s + "\n只查询已注册合成数据。\n" for s in SECTIONS)
    )
    content = {
        "changes": [
            {
                "name": name,
                "version": "1.0.0",
                "allowed_operations": ["mcp__scat__search_scat_flights"],
                "requires_skills": {},
                "contract": {
                    "version": 1,
                    "inputs": ["scat.request.v1"],
                    "outputs": ["scat.query-result.v1"],
                },
                "files": {"SKILL.md": body},
            }
        ]
    }
    row = store.write("probe", content)
    trial = store.begin_trial("probe", row["draft"])
    evaluation = Evaluation(trial | {"changes": content["changes"]})
    child = await asyncio.to_thread(
        subprocess.Popen,
        [sys.executable, "-B", "-c", "import time;time.sleep(90)", str(evaluation.home)],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        assert await recover_interrupted(store) == 1
        await asyncio.to_thread(child.wait, timeout=10)
        assert child.returncode != 0
        assert not evaluation.home.exists()
        assert store.trial("probe", trial["id"])["state"] == "interrupted"
        assert store.releases.current().bundle_sha256 == row["base"]
        report = {
            "status": "passed",
            "orphan_process_stopped": True,
            "profile_removed": True,
            "interrupted_trial_requires_retry": True,
            "active_release_unchanged": True,
            "model_calls": 0,
            "effects": 0,
        }
        (ROOT / "docs/validation/20261024-S7A-interrupted-trial.json").write_text(
            json.dumps(report, indent=2) + "\n"
        )
        print(json.dumps(report))
    finally:
        if child.poll() is None:
            child.kill()
            await asyncio.to_thread(child.wait, timeout=10)
        assert home.resolve().parent == (ROOT / ".tmp/s7a").resolve() and not home.is_symlink()
        for path in home.rglob("*"):
            if path.is_file():
                path.chmod(0o600)
        shutil.rmtree(home)


if __name__ == "__main__":
    if not (Path(sys.prefix) / "conda-meta").is_dir():
        raise RuntimeError("Use existing app Anaconda")
    asyncio.run(run())
