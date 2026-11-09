"""M6-10 候选→默认模型的准入门槛（promotion gate，只读计算）。

策略（`PromotionPolicy`，阈值可覆盖；口径见 `docs/MODEL_PROMOTION_POLICY.md`）：

- **证据条件**：候选与现任必须是同一批次、同一协议（`m6-eval-v1`）、同一数据版本，
  且状态为 `succeeded`（失败的实验不能作为准入证据）；
- **证据规模**：候选覆盖航班 ≥ `min_flight_count`（默认 200），且不低于现任覆盖航班；
- **无回归**：两方样本量均 ≥ `min_samples_per_horizon`（默认 100）的每个时域，
  候选 MAE ≤ 现任 MAE × (1 + `mae_regression_tolerance`)（默认容忍 +5%）；
- **有增益**：上述可比较时域的**平均 MAE 改善** ≥ `min_overall_improvement`（默认 5%）；
- **报告**：逐项 check（含每时域数值与阈值）+ 双方摘要；不写任何数据。

使用（只读）：

    python -m scat.experiments.cli promote-check \\
        --candidate-predictor kalman-cv --incumbent-predictor constant-velocity-heading
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

__all__ = [
    "DEFAULT_PROMOTION_POLICY",
    "PromotionPolicy",
    "evaluate_promotion",
    "select_latest_succeeded",
]


@dataclass(frozen=True, slots=True)
class PromotionPolicy:
    """准入门槛阈值（修改阈值属口径变更，需更新策略文档与版本记录）。"""

    min_flight_count: int = 200
    min_samples_per_horizon: int = 100
    mae_regression_tolerance: float = 0.05
    min_overall_improvement: float = 0.05
    required_protocol: str = "m6-eval-v1"


DEFAULT_PROMOTION_POLICY = PromotionPolicy()


def select_latest_succeeded(experiments: list[dict[str, Any]]) -> dict[str, Any] | None:
    """从（按创建时间倒序的）实验列表中选最新一条 succeeded 且有指标的记录。"""
    for item in experiments:
        if item.get("status") == "succeeded" and item.get("metrics"):
            return item
    return None


def evaluate_promotion(
    *,
    candidate: dict[str, Any],
    incumbent: dict[str, Any],
    policy: PromotionPolicy = DEFAULT_PROMOTION_POLICY,
) -> dict[str, Any]:
    """按门槛逐项检查候选 vs 现任；返回通过情况、逐项明细与双方摘要。"""
    checks: list[dict[str, Any]] = []

    def add(name: str, passed: bool, detail: Any) -> None:
        checks.append({"name": name, "passed": bool(passed), "detail": detail})

    add(
        "same_batch",
        candidate.get("batch") == incumbent.get("batch"),
        {"candidate": candidate.get("batch"), "incumbent": incumbent.get("batch")},
    )
    add(
        "same_data_version",
        candidate.get("data_version") == incumbent.get("data_version"),
        {
            "candidate": candidate.get("data_version"),
            "incumbent": incumbent.get("data_version"),
        },
    )
    add(
        "protocol",
        candidate.get("protocol_version") == policy.required_protocol
        and incumbent.get("protocol_version") == policy.required_protocol,
        {
            "required": policy.required_protocol,
            "candidate": candidate.get("protocol_version"),
            "incumbent": incumbent.get("protocol_version"),
        },
    )
    add(
        "candidate_status",
        candidate.get("status") == "succeeded",
        candidate.get("status"),
    )
    add(
        "incumbent_status",
        incumbent.get("status") == "succeeded",
        incumbent.get("status"),
    )

    candidate_flights = _flight_count(candidate)
    incumbent_flights = _flight_count(incumbent)
    add(
        "candidate_evidence",
        candidate_flights >= policy.min_flight_count,
        {"flight_count": candidate_flights, "min_flight_count": policy.min_flight_count},
    )
    add(
        "coverage_not_reduced",
        candidate_flights >= incumbent_flights,
        {"candidate": candidate_flights, "incumbent": incumbent_flights},
    )

    candidate_horizons = _horizon_map(candidate)
    incumbent_horizons = _horizon_map(incumbent)
    shared = sorted(set(candidate_horizons) & set(incumbent_horizons))
    comparable: list[dict[str, Any]] = []
    for horizon in shared:
        candidate_row = candidate_horizons[horizon]
        incumbent_row = incumbent_horizons[horizon]
        if (
            min(float(candidate_row["sample_count"]), float(incumbent_row["sample_count"]))
            < policy.min_samples_per_horizon
        ):
            continue
        limit_m = float(incumbent_row["mae_m"]) * (1 + policy.mae_regression_tolerance)
        comparable.append(
            {
                "horizon_s": horizon,
                "candidate_mae_m": float(candidate_row["mae_m"]),
                "incumbent_mae_m": float(incumbent_row["mae_m"]),
                "limit_m": limit_m,
                "passed": float(candidate_row["mae_m"]) <= limit_m,
            }
        )

    add(
        "horizon_coverage",
        bool(comparable),
        {
            "comparable_horizons": len(comparable),
            "min_samples_per_horizon": policy.min_samples_per_horizon,
        },
    )
    add(
        "no_horizon_regression",
        bool(comparable) and all(row["passed"] for row in comparable),
        comparable,
    )

    if comparable:
        candidate_overall = sum(row["candidate_mae_m"] for row in comparable) / len(comparable)
        incumbent_overall = sum(row["incumbent_mae_m"] for row in comparable) / len(comparable)
        improvement = (
            (incumbent_overall - candidate_overall) / incumbent_overall
            if incumbent_overall > 0
            else 0.0
        )
    else:
        candidate_overall = None
        incumbent_overall = None
        improvement = None
    add(
        "overall_improvement",
        improvement is not None and improvement >= policy.min_overall_improvement,
        {
            "improvement": improvement,
            "min_overall_improvement": policy.min_overall_improvement,
        },
    )

    passed = all(check["passed"] for check in checks)
    return {
        "passed": passed,
        "protocol_version": policy.required_protocol,
        "policy": asdict(policy),
        "checks": checks,
        "candidate": _summary(candidate, candidate_flights),
        "incumbent": _summary(incumbent, incumbent_flights),
        "overall": {
            "candidate_mae_m": candidate_overall,
            "incumbent_mae_m": incumbent_overall,
            "improvement": improvement,
            "horizons_compared": [row["horizon_s"] for row in comparable],
        },
    }


def _flight_count(record: dict[str, Any]) -> int:
    dataset = record.get("dataset") or {}
    coverage = record.get("coverage") or {}
    value = dataset.get("flight_count") or coverage.get("flights_processed") or 0
    return int(value)


def _horizon_map(record: dict[str, Any]) -> dict[float, dict[str, Any]]:
    rows: dict[float, dict[str, Any]] = {}
    for metric in record.get("metrics") or []:
        if metric.get("mae_m") is None:
            continue
        rows[float(metric["horizon_s"])] = metric
    return rows


def _summary(record: dict[str, Any], flight_count: int) -> dict[str, Any]:
    return {
        "experiment_id": record.get("experiment_id"),
        "predictor_id": record.get("predictor_id"),
        "predictor_version": record.get("predictor_version"),
        "status": record.get("status"),
        "batch": record.get("batch"),
        "data_version": record.get("data_version"),
        "flight_count": flight_count,
        "finished_at": record.get("finished_at"),
    }
