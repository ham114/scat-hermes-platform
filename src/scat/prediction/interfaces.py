"""M6-01/M6-02 统一预测器协议（TrajectoryPredictor）与输入输出 schema。

自研模型的统一约定：

- 时间一律 UTC；坐标 WGS84 度；高度米；速度米/秒；
- 输入 `PredictionRequest`：观测轨迹（按时间严格升序的 `TrackPoint`）+ 目标时刻列表
  （必须晚于观测末点、严格升序）；
- 输出 `PredictionResult`：`fixes` 与 `target_times` 一一对齐（复用 M5 口径的数据类
  `PredictedPoint`，便于后续在评估页与 CFPL 基准同表比较）；
- `PREDICTOR_SCHEMA_VERSION` 为输入/输出 schema 版本：字段、单位或顺序发生破坏性
  变化时必须递增，并随每次预测结果一同记录。

基线与后续模型（Kalman/机器学习/深度模型）都必须实现 `TrajectoryPredictor`。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from scat.prediction.alignment import PredictedPoint, TrackPoint

PREDICTOR_SCHEMA_VERSION = 1

__all__ = [
    "PREDICTOR_SCHEMA_VERSION",
    "PredictionRequest",
    "PredictionResult",
    "TrajectoryPredictor",
    "validate_request",
]


@dataclass(frozen=True, slots=True)
class PredictionRequest:
    """一次预测请求：观测轨迹 + 目标时刻。"""

    observed: tuple[TrackPoint, ...]
    target_times: tuple[datetime, ...]


@dataclass(frozen=True, slots=True)
class PredictionResult:
    """一次预测结果：`fixes` 与请求的 `target_times` 顺序一一对齐。"""

    predictor_id: str
    predictor_version: str
    fixes: tuple[PredictedPoint, ...]


@runtime_checkable
class TrajectoryPredictor(Protocol):
    """统一的轨迹预测器协议（M6-01）。"""

    predictor_id: str
    predictor_version: str

    def predict(self, request: PredictionRequest) -> PredictionResult:
        """按请求返回预测点；输入不满足约束时应抛出 ValueError。"""
        ...


def validate_request(request: PredictionRequest, *, min_points: int = 2) -> None:
    """公共输入校验：观测点数、时间严格升序、目标时刻晚于观测末点。"""
    if len(request.observed) < min_points:
        raise ValueError(f"观测点不足（至少 {min_points} 个）")
    for before, after in zip(request.observed, request.observed[1:]):
        if after.time <= before.time:
            raise ValueError("观测轨迹必须按时间严格升序")
    if not request.target_times:
        raise ValueError("至少需要一个目标时刻")
    last_time = request.observed[-1].time
    if request.target_times[0] <= last_time:
        raise ValueError("目标时刻必须晚于观测末点")
    for before, after in zip(request.target_times, request.target_times[1:]):
        if after <= before:
            raise ValueError("目标时刻必须严格升序")
