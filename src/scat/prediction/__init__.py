"""SCAT 预测模块：M5 评估口径 + M6 统一预测器框架。

- `alignment`：实飞/预测轨迹对齐与误差（口径见 docs/PREDICTION_EVALUATION.md）；
- `interfaces`：M6-01/M6-02 统一预测器协议与输入输出 schema；
- `baselines`：M6-04/M6-05 基础线（匀速/恒航向、Kalman 常速度）；
- `registry`：预测器注册表；
- `datasets`：M6-03 航班级数据集划分；
- `geo`：局部 ENU 坐标工具。
"""

from __future__ import annotations

from scat.prediction.alignment import (
    HORIZONS_S,
    PREDICTION_METRIC_VERSION,
    ExclusionReason,
    FlightEvaluation,
    SnapshotComparison,
    TrackPoint,
    evaluate_flight,
    evaluate_snapshot,
)
from scat.prediction.baselines import (
    ConstantVelocityHeadingPredictor,
    KalmanConstantVelocityPredictor,
)
from scat.prediction.datasets import (
    FlightSplit,
    split_flights_by_flight,
    split_flights_by_time,
)
from scat.prediction.geo import geodetic_to_local_m, local_to_geodetic_m
from scat.prediction.interfaces import (
    PREDICTOR_SCHEMA_VERSION,
    PredictionRequest,
    PredictionResult,
    TrajectoryPredictor,
    validate_request,
)
from scat.prediction.registry import (
    PredictorRegistry,
    build_default_registry,
    default_registry,
)

__all__ = [
    "HORIZONS_S",
    "PREDICTION_METRIC_VERSION",
    "PREDICTOR_SCHEMA_VERSION",
    "ConstantVelocityHeadingPredictor",
    "ExclusionReason",
    "FlightEvaluation",
    "FlightSplit",
    "KalmanConstantVelocityPredictor",
    "PredictionRequest",
    "PredictionResult",
    "PredictorRegistry",
    "SnapshotComparison",
    "TrackPoint",
    "TrajectoryPredictor",
    "build_default_registry",
    "default_registry",
    "evaluate_flight",
    "evaluate_snapshot",
    "geodetic_to_local_m",
    "local_to_geodetic_m",
    "split_flights_by_flight",
    "split_flights_by_time",
    "validate_request",
]
