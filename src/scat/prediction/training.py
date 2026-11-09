"""M6-02 训练侧输入输出 schema：训练数据划分、产物清单与训练日志引用。

训练口径：

- **输入**：航班级划分（`scat.prediction.datasets`）产生的 train/validation/test 集合，
  绑定 `batch` + `source_version` + `feature_version`（输入特征 schema 版本）与划分参数
  （方法 / 种子 / 比例）；登记数量与集合哈希，不复制原始数据；
- **输出**：`TrainingManifest`——模型识别（predictor_id / predictor_version）、模型产物
  文件（经 `scat.experiments.artifacts` 登记，含 sha256/size）、训练日志产物引用、
  超参数、代码版本与起止时间，以及产出的评估实验 id（按 `m6-eval-v1` 报告性能）；
- 训练清单必须先于“注册为可用预测器”落盘；产出模型进入**默认**模型前，按
  `docs/MODEL_PROMOTION_POLICY.md` 的准入门槛验收（`scat.experiments.promotion`）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

TRAINING_SCHEMA_VERSION = 1

__all__ = [
    "TRAINING_SCHEMA_VERSION",
    "TrainingArtifact",
    "TrainingDatasetSpec",
    "TrainingManifest",
]

_SHA256_LENGTH = 64
_SPLIT_NAMES = {"train", "validation", "test"}


class TrainingArtifact(BaseModel):
    """训练产物文件引用（与 `ExperimentArtifactStore.store` 的元数据同构）。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=140)
    path: str = Field(min_length=1, max_length=300)
    sha256: str = Field(min_length=_SHA256_LENGTH, max_length=_SHA256_LENGTH)
    size: int = Field(ge=0)

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        if any(character not in "0123456789abcdef" for character in value):
            raise ValueError("sha256 必须是 64 位小写十六进制")
        return value


class TrainingDatasetSpec(BaseModel):
    """训练/验证/测试划分的登记信息（数量 + 集合哈希，不复制数据）。"""

    model_config = ConfigDict(extra="forbid")

    batch: str = Field(min_length=1, max_length=100)
    source_version: str = Field(min_length=1, max_length=100)
    feature_version: str = Field(min_length=1, max_length=100)
    split_method: Literal["by_flight", "by_time"]
    seed: int | None = Field(default=None, ge=0)
    ratios: dict[str, float] | None = None
    train_flight_count: int = Field(ge=0)
    validation_flight_count: int = Field(default=0, ge=0)
    test_flight_count: int = Field(ge=0)
    flight_ids_sha256: dict[str, str] = Field(default_factory=dict)

    @field_validator("ratios")
    @classmethod
    def validate_ratios(
        cls,
        value: dict[str, float] | None,
    ) -> dict[str, float] | None:
        if value is None:
            return None
        unknown = set(value) - _SPLIT_NAMES
        if unknown:
            raise ValueError(f"ratios 含未知划分名：{sorted(unknown)}")
        if any(ratio <= 0 for ratio in value.values()):
            raise ValueError("ratios 必须为正数")
        return value

    @field_validator("flight_ids_sha256")
    @classmethod
    def validate_hashes(
        cls,
        value: dict[str, str],
    ) -> dict[str, str]:
        unknown = set(value) - _SPLIT_NAMES
        if unknown:
            raise ValueError(f"flight_ids_sha256 含未知划分名：{sorted(unknown)}")
        for digest in value.values():
            if len(digest) != _SHA256_LENGTH or any(
                character not in "0123456789abcdef" for character in digest
            ):
                raise ValueError("flight_ids_sha256 的取值必须是 64 位小写十六进制")
        return value

    @model_validator(mode="after")
    def validate_counts(self) -> TrainingDatasetSpec:
        if self.train_flight_count + self.validation_flight_count + self.test_flight_count <= 0:
            raise ValueError("划分后的航班总数必须大于 0")
        return self


class TrainingManifest(BaseModel):
    """训练任务产出清单（训练侧主契约；随实验产物落盘留痕）。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=TRAINING_SCHEMA_VERSION, ge=1)
    predictor_id: str = Field(min_length=1, max_length=100)
    predictor_version: str = Field(min_length=1, max_length=50)
    dataset: TrainingDatasetSpec
    hyperparameters: dict[str, Any] = Field(default_factory=dict)
    model_artifacts: list[TrainingArtifact] = Field(default_factory=list)
    train_log_artifact: TrainingArtifact | None = None
    evaluation_experiment_id: str | None = Field(default=None, max_length=140)
    code_version: str = Field(min_length=1, max_length=120)
    started_at: datetime
    finished_at: datetime | None = None
    notes: str | None = Field(default=None, max_length=500)
