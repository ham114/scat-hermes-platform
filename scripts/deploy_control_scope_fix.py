"""Deploy the native control bridge owner fix with the existing candidate/rollback gates."""

import deploy_readonly_owner_fix as followup

followup.deploy.OUT = followup.BASE_OUT / "scope-fix"
followup.deploy.FILES = list(
    dict.fromkeys(
        [*followup.deploy.FILES, "scripts/run_workflow_tool.py", "scripts/run_model_task_tool.py"]
    )
)

if __name__ == "__main__":
    text = (followup.BASE_OUT / "control-scope-tests.log").read_text()
    assert "passed" in text and "failed" not in text and "ERROR" not in text
    followup.main()
