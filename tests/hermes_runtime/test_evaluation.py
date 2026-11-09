import copy
import json
from pathlib import Path

import pytest

from hermes_runtime.evaluation import (
    business_payload,
    provider_payload,
    score_boundaries,
    score_case,
)

ROOT = Path(__file__).resolve().parents[2]
CORPUS = json.loads((ROOT / "evaluations/hermes_h2_v1.json").read_text(encoding="utf-8"))


def test_corpus_has_twenty_unique_questions_and_five_readonly_tools():
    cases = CORPUS["cases"]
    assert len(cases) >= 20 and len({c["id"] for c in cases}) == len(cases)
    assert {c["expected_tool"] for c in cases if c["expected_tool"]} == {
        "search_scat_flights",
        "get_scat_batch_overview",
        "get_scat_track_points",
        "inspect_scat_collection_data_quality",
        "search_scat_knowledge",
    }
    assert sum(c["refusal"] for c in cases) == 4


def test_scorer_requires_actual_call_parameters_result_and_terminal():
    case = CORPUS["cases"][0]
    call = {
        "tool": "mcp__scat__search_scat_flights",
        "arguments": case["expected_arguments"],
        "result": {"status": "ok", "data": {"total": 10637}},
    }
    assert score_case(case, [call], "10637", "run.completed")["passed"]
    assert not score_case(case, [], "10637", "run.completed")["passed"]
    assert not score_case(case, [call], "10637", "run.failed")["passed"]
    assert not score_case(case, [call], "999", "run.completed")["passed"]
    changed = call | {"arguments": case["expected_arguments"] | {"adep": "ESSA"}}
    assert not score_case(case, [changed], "10637", "run.completed")["parameters"]
    assert not score_case(case, [call, call], "10637", "run.completed")["tool_selection"]


@pytest.mark.parametrize(
    "value",
    [
        {"status": "ok", "data": {}},
        '{"status":"ok","data":{}}',
        {"content": [{"type": "text", "text": '{"status":"ok","data":{}}'}]},
        {"structuredContent": {"status": "ok", "data": {}}},
    ],
)
def test_mcp_result_shapes(value):
    assert business_payload(value) == {"status": "ok", "data": {}}


def test_failed_tool_is_not_success_and_citation_must_be_from_result():
    case = CORPUS["cases"][11]
    call = {
        "tool": "mcp__scat__search_scat_knowledge",
        "arguments": {"query": "ADE FDE", "limit": 2},
        "result": {"status": "ok", "data": {"hits": [{"source_id": "DOC-REAL"}]}},
    }
    assert score_case(case, [call], "参考 DOC-REAL", "run.completed")["passed"]
    assert not score_case(case, [call], "参考 DOC-FAKE", "run.completed")["evidence"]
    call["result"] = {"status": "timeout", "data": None}
    assert not score_case(case, [call], "调用成功 DOC-REAL", "run.completed")["passed"]


def test_refusal_requires_no_tool_and_a_refusal_response():
    case = CORPUS["cases"][16]
    assert score_case(case, [], "当前只读，无法删除。", "run.completed")["passed"]
    assert not score_case(case, [], "已删除。", "run.completed")["passed"]


def passing_boundaries():
    return {
        "memory": {"same_scope_recalls": True, "other_scope_does_not_recall": True},
        "transient_timeout": {
            "injected_timeout": True,
            "wire": [{"sdk_max_retries": 0}, {"sdk_max_retries": 0}],
            "retry_limits": {
                "api_attempts": 3,
                "automatic_recovery_cycles": 0,
                "primary_transport_recovery_cycles": 1,
            },
            "terminal": "run.completed",
            "output": "H12-RETRY-OK",
        },
        "model_unavailable": {"injected_auth_failures": 1, "terminal": "run.failed", "output": ""},
        "compression": {
            "compression_count": 1,
            "before_chars": 100,
            "after_chars": 40,
            "summary_carrier": True,
            "head_preserved": True,
            "tail_marker_preserved": True,
            "post_compression_recall": True,
            "threshold_before": {"at_threshold": True, "below_threshold": False},
        },
        "approval": {
            "wrong_request_status": 409,
            "deny_status": 200,
            "replay_status": 409,
            "restricted_route_status": 404,
            "result": "deny",
            "waiter_woken": True,
            "once_status": 200,
            "once_result": "once",
            "once_waiter_woken": True,
            "event": "approval.request",
            "choices": ["once", "deny"],
        },
    }


def test_boundary_evidence_requires_all_checks():
    assert all(score_boundaries(passing_boundaries()).values())
    assert not any(score_boundaries({}).values())


@pytest.mark.parametrize(
    "section,key,value,check",
    [
        ("memory", "other_scope_does_not_recall", False, "scoped_history"),
        ("transient_timeout", "wire", [{"sdk_max_retries": 0}] * 7, "bounded_retry"),
        ("model_unavailable", "terminal", "run.completed", "model_unavailable"),
        ("compression", "post_compression_recall", False, "compression"),
        ("approval", "replay_status", 200, "native_approval_fixture"),
    ],
)
def test_failed_or_missing_boundary_cannot_pass(section, key, value, check):
    evidence = copy.deepcopy(passing_boundaries())
    evidence[section][key] = value
    assert score_boundaries(evidence)[check] is False


def test_numeric_and_missing_results_require_matching_answer_evidence():
    case = CORPUS["cases"][8]
    call = {
        "tool": "mcp__scat__get_scat_track_points",
        "arguments": case["expected_arguments"],
        "result": {"status": "ok", "data": {"pagination": {"returned_points": 3}}},
    }
    assert score_case(case, [call], "实际返回3点", "run.completed")["passed"]
    assert not score_case(case, [call], "实际返回30点", "run.completed")["evidence"]
    call["result"] = {"status": "not_found", "data": None}
    assert score_case(case, [call], "没有找到该航班", "run.completed")["passed"]
    assert not score_case(case, [call], "这里有3点", "run.completed")["evidence"]


@pytest.mark.parametrize(
    "value",
    [
        [{"type": "text", "text": '{"status":"ok","data":{}}'}],
        {"content": '{"status":"ok","data":{}}'},
        {"type": "text", "text": '{"status":"ok","data":{}}'},
    ],
)
def test_result_collection_accepts_text_blocks_and_lists(value):
    assert business_payload(value) == {"status": "ok", "data": {}}


def test_overview_uses_total_statistics_not_a_distribution_bucket():
    case = CORPUS["cases"][6]
    call = {
        "tool": "mcp__scat__get_scat_batch_overview",
        "arguments": case["expected_arguments"],
        "result": {
            "status": "ok",
            "data": {
                "statistics": {"flight_count": 10637},
                "top_aircraft_types": [{"flight_count": 3}],
            },
        },
    }
    assert score_case(case, [call], "总航班数10637", "run.completed")["passed"]
    assert not score_case(case, [call], "总航班数3", "run.completed")["evidence"]


def test_provider_observation_includes_sdk_extra_body_messages_and_tools():
    effective = provider_payload(
        {
            "model": "deepseek-flash",
            "extra_body": {
                "messages": [{"role": "tool", "tool_call_id": "call-real", "content": "ok"}],
                "tools": [{"function": {"name": "mcp__scat__search_scat_flights"}}],
                "thinking": {"type": "disabled"},
            },
        }
    )
    assert effective["messages"][0]["tool_call_id"] == "call-real"
    assert effective["tools"][0]["function"]["name"] == "mcp__scat__search_scat_flights"
    assert provider_payload({"messages": [], "extra_body": {"messages": [1]}})["messages"] == [1]


def test_pinned_hermes_result_envelope_unwraps_nested_json_text():
    wire_result = json.dumps({"result": json.dumps({"status": "ok", "data": {"total": 10637}})})
    assert business_payload(wire_result) == {"status": "ok", "data": {"total": 10637}}


def test_retry_bound_allows_one_primary_repair_but_no_unbounded_or_sdk_retry():
    evidence = passing_boundaries()
    evidence["transient_timeout"]["wire"] = [{"sdk_max_retries": 0}] * 6
    assert score_boundaries(evidence)["bounded_retry"]
    evidence["transient_timeout"]["wire"] = [{"sdk_max_retries": 0}] * 7
    assert not score_boundaries(evidence)["bounded_retry"]
    evidence["transient_timeout"]["wire"] = [{"sdk_max_retries": 2}] * 2
    assert not score_boundaries(evidence)["bounded_retry"]
    evidence["transient_timeout"]["wire"] = [{"sdk_max_retries": 0}] * 2
    evidence["transient_timeout"]["retry_limits"]["automatic_recovery_cycles"] = 1
    assert not score_boundaries(evidence)["bounded_retry"]
