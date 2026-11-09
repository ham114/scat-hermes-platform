"""M6-00/M6-08 实验子代理：内部可达性、请求校验与对外投影。"""

from __future__ import annotations

import pytest

from agents.agents import agents, get_all_agent_info
from agents.experiment_agent import (
    experiment_agent,
    public_experiment_agent_result,
)


def test_experiment_agent_is_internal_and_not_registered():
    assert "experiment-agent" not in agents

    exposed_keys = {info.key for info in get_all_agent_info()}
    assert "scat-flight-agent" in exposed_keys
    assert "experiment-agent" not in exposed_keys


@pytest.mark.asyncio
async def test_experiment_agent_marks_unsupported_kind():
    result = await experiment_agent.ainvoke(
        {"request": {"experiment_kind": "speed_heading_baseline"}}
    )

    assert result["status"] == "failed"
    assert result["error_code"] == "unsupported_experiment_kind"
    assert result["run_id"]


@pytest.mark.asyncio
async def test_experiment_agent_rejects_malformed_request():
    result = await experiment_agent.ainvoke({"request": {}})

    assert result["status"] == "failed"
    assert result["error_code"] == "invalid_request"


def test_public_projection_excludes_internal_payload():
    state = {
        "run_id": "run-1",
        "status": "failed",
        "progress": 1.0,
        "message": "m",
        "error_code": "x",
        "result_summary": {"a": 1},
        "request": {"experiment_kind": "k"},
        "internal_tensor": "secret",
    }

    projected = public_experiment_agent_result(state)  # type: ignore[arg-type]

    assert projected == {
        "run_id": "run-1",
        "status": "failed",
        "progress": 1.0,
        "message": "m",
        "error_code": "x",
        "result_summary": {"a": 1},
    }
    assert "request" not in projected
    assert "internal_tensor" not in projected
