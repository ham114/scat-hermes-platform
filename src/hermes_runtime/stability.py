"""Evidence aggregation without hiding failures or conflating shared domain runs."""

import math
from collections import defaultdict


def summarize(attempts, *, target=0.9, repetitions=3, expected_domains=None):
    domains = defaultdict(list)
    for row in attempts:
        for domain in row["domains"]:
            domains[domain].append(row)
    result = {}
    for domain, rows in sorted(domains.items()):
        times = sorted(r["elapsed_seconds"] for r in rows)
        passed = sum(r["passed"] for r in rows)
        result[domain] = {
            "passed": passed,
            "total": len(rows),
            "completion_rate": passed / len(rows),
            "selection_parameter_passed": sum(
                r.get("selection_parameters_passed", False) for r in rows
            ),
            "p50_seconds": times[math.ceil(0.5 * len(times)) - 1],
            "p95_seconds": times[math.ceil(0.95 * len(times)) - 1],
            "elapsed_distribution_seconds": times,
            "small_sample_no_sla": True,
            "gate_passed": len(rows) >= repetitions
            and passed / len(rows) >= target
            and sum(r.get("selection_parameters_passed", False) for r in rows) / len(rows)
            >= target,
        }
    return {
        "domains": result,
        "independent_attempts": len(attempts),
        "passed_attempts": sum(r["passed"] for r in attempts),
        "all_failures_retained": True,
        "status": "passed"
        if (
            set(result) == set(expected_domains)
            if expected_domains is not None
            else len(result) == 11
        )
        and bool(result)
        and all(d["gate_passed"] for d in result.values())
        else "failed",
        "provider_input_tokens": sum(
            r.get("usage", {}).get("provider_input_tokens", 0) for r in attempts
        ),
        "provider_output_tokens": sum(
            r.get("usage", {}).get("provider_output_tokens", 0) for r in attempts
        ),
        "provider_cached_tokens": sum(
            r.get("usage", {}).get("provider_cached_tokens", 0) for r in attempts
        ),
        "conservative_tokens": sum(r.get("usage", {}).get("charged_tokens", 0) for r in attempts),
        "llm_calls": sum(r.get("usage", {}).get("llm_calls", 0) for r in attempts),
        "tool_calls": sum(r.get("usage", {}).get("tool_calls", 0) for r in attempts),
        "cost_estimate": None,
        "cost_reason": "No verified price table; provider usage and conservative admission units are reported separately.",
    }
