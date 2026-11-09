from datetime import UTC
from types import SimpleNamespace

import pytest

from application.context import ExecutionContext
from application.readonly_display import receipt_display
from application.trajectory_view import trajectory_view
from hermes_runtime.results import ResultNotFoundError, ToolResultStore
from hermes_runtime.trajectory_reply import attach_reply_guard, coverage, normalize_reply


def page(total, start=0, step=1, limit=100):
    indices = list(range(start, min(total, start + step * limit), step))
    consumed = min(step * limit, total - start)
    return {
        "status": "ok",
        "query": {
            "batch": "SYNTH",
            "external_id": "100002",
            "start": start,
            "every_nth": step,
            "limit": limit,
            "fields": ["position"],
        },
        "data": {
            "pagination": {
                "total_points": total,
                "start": start,
                "source_points_consumed": consumed,
                "returned_points": len(indices),
                "has_more": start + consumed < total,
                "next_start": start + consumed if start + consumed < total else None,
            },
            "points": [
                {
                    "sequence": i,
                    "position": {
                        "lat_deg": 50 + i / 100000,
                        "lon_deg": 4 + i / 100000,
                        "altitude_m": 1000,
                    },
                }
                for i in indices
            ],
        },
    }


@pytest.fixture
def setup(tmp_path):
    return ToolResultStore(tmp_path / "results.sqlite", workspace_root=tmp_path), ExecutionContext(
        "owner", "thread"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("total", [1, 393, 524, 2000, 2001, 10001, 50001])
async def test_full_or_bounded_sample_and_cached_refresh(setup, total):
    store, context = setup
    seed = store.put(context.scope_key, "run_one", "seed", "get_scat_track_points", page(total))
    calls = []

    async def invoke(name, args, *, context):
        assert args["every_nth"] <= 20
        calls.append(args)
        return page(total, args["start"], args["every_nth"], args["limit"])

    result = await trajectory_view(
        store, context, "run_one", seed["result_id"], SimpleNamespace(invoke=invoke)
    )
    fact = result["coverage"]
    assert fact["full_extent"] and not fact["partial"] and fact["loaded"] <= 2000
    assert fact["complete"] == (total <= 2000)
    actual = [
        p["sequence"]
        for ref in result["items"]
        for p in store.get(context.scope_key, "run_one", ref["result_id"])["result"]["data"][
            "points"
        ]
    ]
    assert actual[0] == 0 and actual[-1] == total - 1
    before = len(calls)
    assert (
        await trajectory_view(
            store, context, "run_one", seed["result_id"], SimpleNamespace(invoke=invoke)
        )
        == result
    )
    assert len(calls) == before <= 22
    assert coverage(store, context.scope_key, "run_one")[0]["loaded"] == min(total, 100)
    assert (
        "display_only" not in store.get(context.scope_key, "run_one", seed["result_id"])["result"]
    )


@pytest.mark.asyncio
async def test_scope_validation_and_partial_failure_never_claims_full(setup):
    store, context = setup
    seed = store.put(context.scope_key, "run_one", "seed", "get_scat_track_points", page(524))
    calls = []

    async def invoke(name, args, *, context):
        calls.append(args)
        return page(524) if len(calls) == 1 else {"status": "timeout", "data": None}

    app = SimpleNamespace(invoke=invoke)
    with pytest.raises(ResultNotFoundError):
        await trajectory_view(
            store, ExecutionContext("other", "thread"), "run_one", seed["result_id"], app
        )
    assert not calls
    result = await trajectory_view(store, context, "run_one", seed["result_id"], app)
    assert result["coverage"]["partial"] and not result["coverage"]["complete"]
    assert result["coverage"]["loaded"] == 100
    assert await trajectory_view(store, context, "run_one", seed["result_id"], app) == result
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_display_pages_cannot_be_used_as_recursive_expansion_sources(setup):
    store, context = setup
    raw = page(524)
    raw["display_only"] = True
    ref = store.put(context.scope_key, "run_one", "display", "get_scat_track_points", raw)

    async def forbidden(*args, **kwargs):
        raise AssertionError("must_not_query")

    with pytest.raises(ValueError, match="original_analysis_source_required"):
        await trajectory_view(
            store, context, "run_one", ref["result_id"], SimpleNamespace(invoke=forbidden)
        )


def test_false_full_coverage_claim_is_replaced_and_guard_is_idempotent(setup):
    store, context = setup
    for start, count in [(0, 100), (400, 100), (500, 24)]:
        store.put(
            context.scope_key,
            "run_one",
            str(start),
            "get_scat_track_points",
            page(524, start, 1, count),
        )
    facts = coverage(store, context.scope_key, "run_one")
    assert facts[0]["loaded"] == 224 and not facts[0]["complete"]
    text = "我读取了全部524个点，时间覆盖全程。\n\n趋势：由平飞转为下降。"
    result = normalize_reply(text, facts)
    assert "224/524" in result and "读取了全部" not in result and "趋势：" in result
    assert normalize_reply(result, facts) == result
    agent = SimpleNamespace(
        run_conversation=lambda **_: {
            "final_response": text,
            "messages": [{"role": "assistant", "content": text}],
        }
    )
    attach_reply_guard(agent, store, context.scope_key, "run_one")
    checked = agent.run_conversation()
    assert checked["messages"][0]["content"] == checked["final_response"] == result


@pytest.mark.asyncio
async def test_timed_pages_share_actual_origin_without_resetting_each_page(setup):
    from datetime import datetime, timedelta

    store, context = setup
    seed = store.put(context.scope_key, "run_timed", "seed", "get_scat_track_points", page(120))
    origin = datetime(2020, 1, 1, tzinfo=UTC)

    async def invoke(name, args, *, context):
        result = page(120, args["start"], args["every_nth"], args["limit"])
        for p in result["data"]["points"]:
            p["timestamp"] = (origin + timedelta(seconds=p["sequence"])).isoformat()
        return result

    result = await trajectory_view(
        store, context, "run_timed", seed["result_id"], SimpleNamespace(invoke=invoke)
    )
    outputs = [
        receipt_display(store.get(context.scope_key, "run_timed", ref["result_id"]))[
            "analysis_result"
        ]
        for ref in result["items"]
    ]
    assert len({a["start_time"] for a in outputs}) == 1
    second = next(b for b in outputs[1]["blocks"] if b["type"] == "trajectory_2d")
    assert second["points"][0][second["time_key"]] == 100
