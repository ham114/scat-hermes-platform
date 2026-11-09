"""M6-10 准入门槛测试：逐项 check 的通过与失败路径。"""

from __future__ import annotations

from typing import Any

from scat.experiments.promotion import (
    PromotionPolicy,
    evaluate_promotion,
    select_latest_succeeded,
)

BATCH = "scat20161015_20161021"


def _record(
    *,
    experiment_id: str,
    predictor_id: str,
    mae_by_horizon: dict[float, float],
    status: str = "succeeded",
    protocol: str = "m6-eval-v1",
    data_version: str = "scat-import-2",
    flights: int = 300,
    samples: int = 500,
) -> dict[str, Any]:
    return {
        "experiment_id": experiment_id,
        "predictor_id": predictor_id,
        "predictor_version": "1",
        "status": status,
        "batch": BATCH,
        "protocol_version": protocol,
        "data_version": data_version,
        "dataset": {"flight_count": flights},
        "coverage": {"flights_processed": flights},
        "finished_at": "2026-09-20T09:00:00Z",
        "metrics": [
            {
                "horizon_s": horizon,
                "sample_count": samples,
                "flight_count": flights,
                "mae_m": mae,
            }
            for horizon, mae in mae_by_horizon.items()
        ],
    }


INCUMBENT = _record(
    experiment_id="incumbent",
    predictor_id="constant-velocity-heading",
    mae_by_horizon={60.0: 1000.0, 300.0: 5000.0, 1200.0: 40000.0},
)

IMPROVED = {60.0: 500.0, 300.0: 2000.0, 1200.0: 20000.0}


def test_promotion_passes_with_clear_improvement() -> None:
    candidate = _record(
        experiment_id="candidate",
        predictor_id="kalman-cv",
        mae_by_horizon=IMPROVED,
    )

    report = evaluate_promotion(candidate=candidate, incumbent=INCUMBENT)

    assert report["passed"] is True
    assert all(check["passed"] for check in report["checks"])
    improvement = report["overall"]["improvement"]
    assert improvement is not None and improvement > 0.5
    assert report["candidate"]["experiment_id"] == "candidate"


def test_promotion_fails_on_horizon_regression() -> None:
    candidate = _record(
        experiment_id="candidate",
        predictor_id="kalman-cv",
        mae_by_horizon={**IMPROVED, 300.0: 5400.0},  # 相对现任 +8%，超出 +5% 容忍
    )

    report = evaluate_promotion(candidate=candidate, incumbent=INCUMBENT)

    assert report["passed"] is False
    regression = next(
        check for check in report["checks"] if check["name"] == "no_horizon_regression"
    )
    assert regression["passed"] is False
    assert any(row["horizon_s"] == 300.0 and row["passed"] is False for row in regression["detail"])


def test_promotion_fails_without_evidence_flights() -> None:
    candidate = _record(
        experiment_id="candidate",
        predictor_id="kalman-cv",
        mae_by_horizon=IMPROVED,
        flights=50,
    )

    report = evaluate_promotion(candidate=candidate, incumbent=INCUMBENT)

    assert report["passed"] is False
    evidence = next(check for check in report["checks"] if check["name"] == "candidate_evidence")
    assert evidence["passed"] is False


def test_promotion_requires_same_protocol_and_data_version() -> None:
    wrong_protocol = _record(
        experiment_id="candidate",
        predictor_id="kalman-cv",
        mae_by_horizon=IMPROVED,
        protocol="m6-eval-v2",
    )
    protocol_check = next(
        check
        for check in evaluate_promotion(candidate=wrong_protocol, incumbent=INCUMBENT)["checks"]
        if check["name"] == "protocol"
    )
    assert protocol_check["passed"] is False

    wrong_version = _record(
        experiment_id="candidate",
        predictor_id="kalman-cv",
        mae_by_horizon=IMPROVED,
        data_version="scat-import-3",
    )
    version_check = next(
        check
        for check in evaluate_promotion(candidate=wrong_version, incumbent=INCUMBENT)["checks"]
        if check["name"] == "same_data_version"
    )
    assert version_check["passed"] is False


def test_promotion_policy_thresholds_can_be_overridden() -> None:
    candidate = _record(
        experiment_id="candidate",
        predictor_id="kalman-cv",
        mae_by_horizon=IMPROVED,
        flights=50,
    )
    small_incumbent = _record(
        experiment_id="small-incumbent",
        predictor_id="constant-velocity-heading",
        mae_by_horizon={60.0: 1000.0, 300.0: 5000.0, 1200.0: 40000.0},
        flights=20,
    )

    default_report = evaluate_promotion(candidate=candidate, incumbent=small_incumbent)
    assert default_report["passed"] is False  # 默认要求候选 ≥200 架证据

    lenient_report = evaluate_promotion(
        candidate=candidate,
        incumbent=small_incumbent,
        policy=PromotionPolicy(min_flight_count=10),
    )

    assert lenient_report["passed"] is True


def test_select_latest_succeeded_skips_unusable_rows() -> None:
    rows: list[dict[str, Any]] = [
        {"status": "pending", "metrics": None},
        {"status": "failed", "metrics": [{"horizon_s": 60.0, "mae_m": 1.0}]},
        {"status": "succeeded", "metrics": []},
        {
            "experiment_id": "ok",
            "status": "succeeded",
            "metrics": [{"horizon_s": 60.0, "mae_m": 1.0}],
        },
    ]

    selected = select_latest_succeeded(rows)

    assert selected is not None and selected["experiment_id"] == "ok"
    assert select_latest_succeeded([]) is None
