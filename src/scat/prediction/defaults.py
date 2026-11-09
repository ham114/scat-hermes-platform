"""自研预测器的平台默认（M6-10）。

`DEFAULT_PREDICTOR_ID` 是对话工具（`predict_scat_flight_trajectory`）、单航班预测服务
与实验入队的默认预测器。**切换默认必须有依据**：按 `docs/MODEL_PROMOTION_POLICY.md`
的准入门槛，用 `scat.experiments.promotion` 对候选与现任做同批次同协议对比并全部
通过后，才允许修改本常量；切换属于代码变更，随版本记录（`code_version`）。
"""

from __future__ import annotations

DEFAULT_PREDICTOR_ID = "kalman-cv"

__all__ = ["DEFAULT_PREDICTOR_ID"]
