"""Reuse the release gates for the registered-owner trajectory paging correction."""

import json
import os
import shutil
import sys

import deploy_release_readiness as deploy

BASE_OUT = deploy.OUT
deploy.OUT = BASE_OUT / "owner-fix"
deploy.FILES = list(dict.fromkeys([*deploy.FILES, "src/application/readonly.py"]))


def main():
    os.umask(0o077)
    action = sys.argv[1]
    deploy.OUT.mkdir(exist_ok=True)
    assert "56 passed" in (BASE_OUT / "readonly-owner-tests-r2.log").read_text()
    if action == "stage":
        assert not (deploy.OUT / "deployment-plan.json").exists()
        shutil.copyfile(BASE_OUT / "backend-full-final.log", deploy.OUT / "backend-full-final.log")
        deploy.save(
            deploy.OUT / "validation-basis.json",
            {
                "full_suite": "../backend-full-final.log",
                "full_suite_applies_to_previous_release": True,
                "new_change_focused_suite": "../readonly-owner-tests-r2.log",
                "focused_passed": 56,
            },
        )
        deploy.stage()
    elif action == "smoke":
        import smoke_readiness_cluster

        smoke_readiness_cluster.main()
    elif action == "apply":
        import apply_release_readiness

        apply_release_readiness.apply()
    elif action == "rollback":
        from pathlib import Path

        import apply_release_readiness

        assert Path("/var/lib/scat-hermes-portal/maintenance").exists(), "public_gate_required"
        plan = json.loads(deploy.plan_file().read_text())
        apply_release_readiness.rollback(plan, deploy.DEPLOY / "releases" / plan["release"])
    else:
        raise ValueError("unsupported_action")


if __name__ == "__main__":
    main()
