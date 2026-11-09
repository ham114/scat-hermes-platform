"""M6-06/07 自研模型实验记录契约（实验台账 + 评估结果留存）。

口径：

- 一次实验 = 一个（预测器, 数据, 协议）三元组在指定航班集合上的一次运行；
- 版本四要素显式记录：`data_version`（源数据版本）、`feature_version`（预测器输入
  schema 版本）、`model_version`（模型/预测器版本）、`hyperparameters`（可覆盖参数）；
- 结果记录含六档时域聚合指标、覆盖与排除统计、最差样本 Top-K（有界），以及
  阶段分组指标（`phase_metrics`）与运行耗时（`timing`）；大产物（模型文件、训练日志、
  评估报告）由 `scat.experiments.artifacts` 写入文件并在 `artifacts` 中留痕（sha256+size）；
- 报告完整性（M6 验收）：效果（metrics/phase_metrics）、推理耗时（timing）、
  适用范围（applicability）三者必须同时存在；
- 状态机：CLI 直接运行 = running→终态；对话提交（M6-08）= pending→（运维 CLI 领取）
  running→终态，`source`/`requested_by` 记录来源与发起用户。

集合：`scat_model_experiments`（唯一 `experiment_id`；查询索引 batch + predictor）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

EXPERIMENT_PROTOCOL_VERSION = "m6-eval-v1"

ExperimentStatus = Literal["pending", "running", "succeeded", "partial", "failed"]


class HorizonMetrics(BaseModel):
    """单档时域的聚合指标（米制）。"""

    model_config = ConfigDict(extra="forbid")

    horizon_s: float = Field(ge=0)
    sample_count: int = Field(ge=0)
    flight_count: int = Field(ge=0)
    mae_m: float | None = Field(default=None, ge=0)
    rmse_m: float | None = Field(default=None, ge=0)
    median_m: float | None = Field(default=None, ge=0)
    p90_m: float | None = Field(default=None, ge=0)
    p95_m: float | None = Field(default=None, ge=0)


class PhaseHorizonMetrics(BaseModel):
    """按飞行阶段分组的单档时域指标（分组表现；阶段取观测截止点所在段）。"""

    model_config = ConfigDict(extra="forbid")

    phase: str = Field(min_length=1, max_length=50)
    horizon_s: float = Field(ge=0)
    sample_count: int = Field(ge=0)
    flight_count: int = Field(ge=0)
    mae_m: float | None = Field(default=None, ge=0)
    median_m: float | None = Field(default=None, ge=0)
    p90_m: float | None = Field(default=None, ge=0)


class WorstSample(BaseModel):
    """最差样本（按水平误差倒序，有界 Top-K）。"""

    model_config = ConfigDict(extra="forbid")

    external_id: str = Field(min_length=1, max_length=100)
    cutoff_time: datetime
    horizon_s: float = Field(ge=0)
    error_m: float = Field(ge=0)


class ExperimentRecord(BaseModel):
    """`scat_model_experiments` 文档：实验台账（M6-06）与评估结果（M6-07 起）。"""

    model_config = ConfigDict(extra="forbid")

    experiment_id: str = Field(min_length=1, max_length=140)
    owner_user_id: str | None = Field(default=None, min_length=1, max_length=200)
    kind: str = Field(default="predictor_evaluation", min_length=1, max_length=50)
    status: ExperimentStatus
    batch: str = Field(min_length=1, max_length=100)
    predictor_id: str = Field(min_length=1, max_length=100)
    predictor_version: str = Field(min_length=1, max_length=50)
    model_version: str = Field(min_length=1, max_length=50)
    data_version: str = Field(min_length=1, max_length=100)
    feature_version: str = Field(min_length=1, max_length=100)
    protocol_version: str = Field(default=EXPERIMENT_PROTOCOL_VERSION, min_length=1, max_length=50)
    hyperparameters: dict[str, Any] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)
    dataset: dict[str, Any] = Field(default_factory=dict)
    coverage: dict[str, Any] = Field(default_factory=dict)
    metrics: list[HorizonMetrics] = Field(default_factory=list)
    phase_metrics: list[PhaseHorizonMetrics] = Field(default_factory=list)
    worst_samples: list[WorstSample] = Field(default_factory=list)
    timing: dict[str, Any] = Field(default_factory=dict)
    applicability: str | None = Field(default=None, max_length=500)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    message: str | None = Field(default=None, max_length=500)
    source: str = Field(default="cli", min_length=1, max_length=50, description="cli|chat")
    requested_by: str | None = Field(
        default=None, max_length=200, description="发起用户 ID（对话来源）"
    )
    lease_owner: str | None = Field(
        default=None, min_length=1, max_length=200, description="当前/最近一次领取者"
    )
    lease_token: str | None = Field(
        default=None, min_length=16, max_length=100, description="当前领取租约的不透明令牌"
    )
    lease_acquired_at: datetime | None = Field(default=None, description="租约首次领取时间（UTC）")
    lease_heartbeat_at: datetime | None = Field(default=None, description="最近一次续租时间（UTC）")
    lease_expires_at: datetime | None = Field(default=None, description="当前租约过期时间（UTC）")
    attempt_count: int = Field(default=0, ge=0, description="已开始的执行尝试次数")
    max_attempts: int = Field(default=3, ge=1, le=20, description="允许的最大执行尝试次数")
    next_attempt_at: datetime | None = Field(default=None, description="退避后最早可再次领取时间")
    requeue_count: int = Field(default=0, ge=0, description="人工重新入队次数")
    requeue_history: list[dict[str, Any]] = Field(
        default_factory=list,
        max_length=20,
        description="有界人工重入审计记录（不保存 lease token）",
    )
    code_version: str = Field(min_length=1, max_length=120)
    created_at: datetime = Field(description="UTC 起始时间")
    finished_at: datetime | None = Field(
        default=None, description="UTC 完成时间；running 时为 None"
    )
