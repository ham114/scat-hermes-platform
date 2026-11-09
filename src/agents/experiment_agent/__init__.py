"""内部确定性实验编排子代理（M6 自研预测模型）。

本包按《Agent 拓扑与子代理接入规范》（计划 §5.3）建立：在 ``agents.agents``
注册为 ``experiment-agent``（``expose=False``），**不通过公开 Agent API 面向用户**，
由 ``scat-flight-agent`` 的边界工具在进程内调用（与 ``model_agent`` 同模式）。
"""

from agents.experiment_agent.graph import (
    ExperimentAgentState,
    build_experiment_agent,
    experiment_agent,
    public_experiment_agent_result,
)

__all__ = [
    "ExperimentAgentState",
    "build_experiment_agent",
    "experiment_agent",
    "public_experiment_agent_result",
]
