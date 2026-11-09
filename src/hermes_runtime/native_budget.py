"""Budget adapter for the pinned DeepSeek Chat Completions Hermes profile."""

from __future__ import annotations

import hashlib
import json
from threading import Timer

from hermes_runtime.skill_budget import BudgetExhausted


def verify_budget_source(root, source):
    pins = json.loads((root / "services/hermes/budget.lock.json").read_text())
    for name, expected in pins["native_sources"].items():
        if hashlib.sha256((source / name).read_bytes()).hexdigest() != expected:
            raise RuntimeError("native_budget_source_mismatch")


def attach_budget(agent, budget, bridge):
    if getattr(agent, "api_mode", None) != "chat_completions":
        raise RuntimeError("unsupported_budget_transport")
    # The separate auxiliary compression client is outside this bounded transport.
    # Fail on an oversized context rather than dispatch an unmetered model request.
    agent.compression_enabled = False
    original_factory = agent._create_request_openai_client
    wrapped_clients = []

    def instrument(client):
        if any(client is old for old in wrapped_clients):
            return client
        client.max_retries = 0
        completions = client.chat.completions
        completions.create = budget.wrap_create(completions.create)
        wrapped_clients.append(client)
        return client

    def factory(**kwargs):
        return instrument(original_factory(**kwargs))

    agent._create_request_openai_client = factory
    instrument(agent.client)
    original_run = agent.run_conversation
    bridge._budgets[agent.session_id] = budget

    def run(**kwargs):
        def expired():
            budget.exhaust("wall_seconds")
            agent.interrupt("budget_exhausted:wall_seconds")

        timer = Timer(budget.limits.wall_seconds, expired)
        timer.daemon = True
        timer.start()
        result = None
        try:
            try:
                result = original_run(**kwargs)
            except BudgetExhausted:
                result = {"messages": [], "final_response": "只读运行额度已用尽。"}
            state = budget.snapshot()
            reason = state["exhausted"]
            # A provider request or tool may have returned after its deadline.
            with budget.lock:
                if budget.clock() - state["started_at"] >= budget.limits.wall_seconds:
                    reason = "wall_seconds"
                    budget.exhaust(reason)
            if reason:
                result = {
                    **result,
                    "failed": True,
                    "completed": False,
                    "turn_exit_reason": "budget_exhausted",
                    "error": "budget_exhausted:" + reason,
                }
            return result
        finally:
            timer.cancel()
            budget.finish()
            bridge.finish_session(agent.session_id)

    agent.run_conversation = run
    agent._scat_run_budget = budget
