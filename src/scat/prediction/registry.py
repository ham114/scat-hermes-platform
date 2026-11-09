"""M6-01 预测器注册表：统一发现与获取（供数据集实验与评估对比使用）。

`predictor_id` 全局唯一；默认注册两条基线（M6-04 匀速/恒航向、M6-05 Kalman）。
后续自研模型接入时按同一接口注册；版本由各预测器的 `predictor_version` 记录。
"""

from __future__ import annotations

from scat.prediction.baselines import (
    ConstantVelocityHeadingPredictor,
    KalmanConstantVelocityPredictor,
)
from scat.prediction.interfaces import TrajectoryPredictor

__all__ = [
    "PredictorRegistry",
    "build_default_registry",
    "default_registry",
]


class PredictorRegistry:
    """内存注册表：登记、获取与列表（只读使用，不持有运行状态）。"""

    def __init__(self) -> None:
        self._predictors: dict[str, TrajectoryPredictor] = {}

    def register(self, predictor: TrajectoryPredictor) -> None:
        predictor_id = predictor.predictor_id
        if not predictor_id:
            raise ValueError("predictor_id 不能为空")
        if predictor_id in self._predictors:
            raise ValueError(f"预测器已注册: {predictor_id}")
        self._predictors[predictor_id] = predictor

    def get(self, predictor_id: str) -> TrajectoryPredictor:
        try:
            return self._predictors[predictor_id]
        except KeyError as exc:
            raise KeyError(f"未注册的预测器: {predictor_id}") from exc

    def list_predictors(self) -> list[tuple[str, str]]:
        """返回 (predictor_id, predictor_version) 列表，按 id 排序。"""
        return sorted(
            (item.predictor_id, item.predictor_version) for item in self._predictors.values()
        )


def build_default_registry() -> PredictorRegistry:
    registry = PredictorRegistry()
    registry.register(ConstantVelocityHeadingPredictor())
    registry.register(KalmanConstantVelocityPredictor())
    return registry


default_registry = build_default_registry()
