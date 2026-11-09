import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from hermes_runtime.native_budget import attach_budget
from hermes_runtime.skill_budget import BudgetExhausted, RunBudget
from hermes_runtime.skill_policy import Budget, SkillPolicy, load_policy


@pytest.mark.parametrize(
    "patch",
    [
        {"unknown": 1},
        {"trace": {"max_hot_records": 0}},
        {"sessions": {"max_active": True}},
        {"composition": {"max_depth": 17}},
        {"trace": {"archive_high_watermark": float("nan")}},
        {"trace": {"hot_retention_days": 20, "archive_retention_days": 10}},
        {"budgets": {"readonly": {"max_cost": 1.0}}},
        {"budgets": {"readonly": {"currency": "INVALID"}}},
        {"budgets": {"readonly": {"max_turns": 17}}},
        {"user_preferences": {"one": {"shell": 1}}},
    ],
)
def test_bad_deployment_policy_is_rejected(patch):
    with pytest.raises((ValidationError, ValueError)):
        SkillPolicy.model_validate(patch)


def test_policy_override_order_and_snapshot(tmp_path):
    policy = SkillPolicy(
        category_overrides={"query": {"max_tool_calls": 20}},
        user_preferences={"one": {"max_tool_calls": 10}},
    )
    assert (
        policy.effective(category="query", user="one", request={"max_tool_calls": 3}).max_tool_calls
        == 3
    )
    with pytest.raises(ValueError, match="ceiling"):
        policy.effective(category="query", user="one", request={"max_tool_calls": 11})
    assert policy.sha256 != SkillPolicy().sha256
    path = tmp_path / "policy.yaml"
    path.write_text("schema_version: 1\nsessions:\n  max_active: 2\n")
    assert load_policy(tmp_path, path).sessions.max_active == 2
    with pytest.raises(ValueError, match="outside_workspace"):
        load_policy(tmp_path, Path("../outside.yaml"))


def ledger(tmp_path, **limits):
    return RunBudget(tmp_path / "budget.sqlite", "owner", "run", Budget(**limits), "policy")


def test_concurrent_tool_attempts_and_reopen_cannot_reset_budget(tmp_path):
    budgets = [ledger(tmp_path, max_tool_calls=3) for _ in range(8)]

    def attempt(budget):
        try:
            budget.admit_tool()
            return True
        except BudgetExhausted:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(attempt, budgets)) == 3
    assert ledger(tmp_path, max_tool_calls=3).snapshot()["tool_calls"] == 3
    with pytest.raises(ValueError, match="snapshot_mismatch"):
        ledger(tmp_path, max_tool_calls=4)


def test_llm_input_output_reservation_and_missing_usage(tmp_path):
    budget = ledger(tmp_path, max_llm_tokens=1000, max_turns=2)
    requests = []

    def create(**kwargs):
        requests.append(kwargs)
        return SimpleNamespace(
            usage=SimpleNamespace(
                model_dump=lambda: {
                    "prompt_tokens": 8,
                    "completion_tokens": 5,
                    "prompt_cache_hit_tokens": 3,
                }
            )
        )

    invoke = budget.wrap_create(create)
    invoke(messages=[{"role": "user", "content": "合成"}], max_tokens=900)
    row = budget.snapshot()
    assert row["provider_input_tokens"] == 8 and row["provider_cached_tokens"] == 3
    assert 5 < row["charged_tokens"] < 1000
    assert requests[0]["max_tokens"] < 1000

    def fail(**kwargs):
        raise OSError("provider failure")

    with pytest.raises(OSError):
        budget.wrap_create(fail)(messages=[], max_tokens=5)
    before = budget.snapshot()["charged_tokens"]
    with pytest.raises(BudgetExhausted):
        invoke(messages=[], max_tokens=5)
    assert len(requests) == 1 and budget.snapshot()["charged_tokens"] == before


def test_no_llm_request_if_input_reservation_exceeds_remaining_tokens(tmp_path):
    budget = ledger(tmp_path, max_llm_tokens=5)
    with pytest.raises(BudgetExhausted, match="max_llm_tokens"):
        budget.wrap_create(lambda **kwargs: pytest.fail("provider must not be called"))(
            messages=["large"]
        )
    assert budget.snapshot()["llm_calls"] == 0


def test_streaming_usage_and_close_are_accounted(tmp_path):
    budget = ledger(tmp_path)
    chunks = [
        SimpleNamespace(usage=None),
        SimpleNamespace(usage={"prompt_tokens": 10, "completion_tokens": 7}),
    ]
    stream = budget.wrap_create(lambda **kw: iter(chunks))(messages=[], stream=True)
    assert list(stream) == chunks
    stream.close()
    assert budget.snapshot()["provider_output_tokens"] == 7


def test_wall_and_finish_block_new_calls(tmp_path):
    now = [100.0]
    budget = RunBudget(
        tmp_path / "b.sqlite", "s", "r", Budget(wall_seconds=1), "p", clock=lambda: now[0]
    )
    now[0] = 102
    with pytest.raises(BudgetExhausted, match="wall_seconds"):
        budget.admit_tool()
    budget.finish()
    assert budget.snapshot()["finished_at"] == 102


def test_native_request_factory_reuse_cannot_bypass_limits(tmp_path):
    budget = ledger(tmp_path, max_turns=1)
    calls = []
    client = SimpleNamespace(
        max_retries=3,
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=lambda **kw: calls.append(kw) or SimpleNamespace(usage=None)
            )
        ),
    )
    bridge = SimpleNamespace(_budgets={}, finish_session=lambda sid: calls.append(sid))

    def conversation(**kwargs):
        agent._create_request_openai_client(reason="one").chat.completions.create(messages=[])
        agent._create_request_openai_client(reason="two").chat.completions.create(messages=[])
        pytest.fail("second request must be refused")

    agent = SimpleNamespace(
        api_mode="chat_completions",
        client=client,
        session_id="n",
        _create_request_openai_client=lambda **kw: client,
        run_conversation=conversation,
        interrupt=lambda reason: None,
    )
    attach_budget(agent, budget, bridge)
    result = agent.run_conversation()
    assert result["failed"] and result["turn_exit_reason"] == "budget_exhausted"
    assert len(calls) == 2 and calls[-1] == "n" and client.max_retries == 0
    assert budget.snapshot()["llm_calls"] == 1


def test_checked_in_policy_and_schema_are_serializable():
    root = Path(__file__).resolve().parents[2]
    policy = load_policy(root)
    assert policy.effective().max_turns == 16
    assert json.loads(json.dumps(SkillPolicy.model_json_schema()))["additionalProperties"] is False
