"""M6 自研模型实验模块：实验记录、评估协议与运行器（M6-06/07/08/10）。

- `contracts`：实验台账与指标契约（数据/特征/模型版本、超参数、结果、产物引用）；
- `repository`：`scat_model_experiments` 集合读写、租约领取/心跳、人工重入、fencing 与索引；
- `evaluation`：M6 评估协议 v1（`m6-eval-v1`：采样、误差、聚合，纯计算）；
- `artifacts`：实验产物目录存储（报告/训练日志/模型文件，sha256 留痕）；
- `grouping`：按飞行阶段的分组表现（物化段级标注，缺失降级）；
- `promotion`：候选→默认模型的准入门槛（M6-10，只读）；
- `runner`：实验创建（pending）/ 领取执行 / 直接运行 / 报告产物导出；
- `cli`：`python -m scat.experiments.cli`（run / submit / claim / requeue / list / show /
  export-report / promote-check / ensure-indexes）。
"""

from __future__ import annotations

from scat.experiments.artifacts import ExperimentArtifactStore
from scat.experiments.contracts import (
    EXPERIMENT_PROTOCOL_VERSION,
    ExperimentRecord,
    ExperimentStatus,
    HorizonMetrics,
    PhaseHorizonMetrics,
    WorstSample,
)
from scat.experiments.evaluation import (
    EvaluationConfig,
    SampleResult,
    aggregate_phase_metrics,
    aggregate_samples,
    cutoff_times,
    evaluate_track,
)
from scat.experiments.promotion import (
    DEFAULT_PROMOTION_POLICY,
    PromotionPolicy,
    evaluate_promotion,
    select_latest_succeeded,
)
from scat.experiments.repository import EXPERIMENT_COLLECTION, ExperimentRepository
from scat.experiments.runner import (
    create_pending_experiment,
    export_experiment_report,
    run_pending_experiment,
    run_predictor_evaluation,
)

__all__ = [
    "DEFAULT_PROMOTION_POLICY",
    "EXPERIMENT_COLLECTION",
    "EXPERIMENT_PROTOCOL_VERSION",
    "EvaluationConfig",
    "ExperimentArtifactStore",
    "ExperimentRecord",
    "ExperimentRepository",
    "ExperimentStatus",
    "HorizonMetrics",
    "PhaseHorizonMetrics",
    "PromotionPolicy",
    "SampleResult",
    "WorstSample",
    "aggregate_phase_metrics",
    "aggregate_samples",
    "create_pending_experiment",
    "cutoff_times",
    "evaluate_promotion",
    "evaluate_track",
    "export_experiment_report",
    "run_pending_experiment",
    "run_predictor_evaluation",
    "select_latest_succeeded",
]
