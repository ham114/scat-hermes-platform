# Compose 部署

[`compose.yaml`](compose.yaml) 定义平台、主 Agent、任务服务、后台 Agent 和可选作者服务。服务使用已构建的固定镜像，并通过部署环境提供连接参数及持久卷。

运行前准备模型登记、数据连接、认证网关及角色环境。持久状态目录保存会话、任务、制品与活动技能包。GBAR 推理由已登记 GPU 服务提供。[部署指南](../../docs/DEPLOYMENT.md) · [GPU 登记示例](../../config/production-gpu.example.json)

自主绘图使用可选 [`compose.visualization.yaml`](compose.visualization.yaml) overlay 和 `visualization` profile，默认关闭。使用现有在用发布拓扑叠加该文件，保留后续认证、记忆及调用链挂载；不要覆盖实际生产基础配置。新增 worker 不挂 Docker socket，受信 Docker 启动器作为宿主 systemd 服务。模型凭据仅通过 [`visualization-worker.env.example`](visualization-worker.env.example) 对应的私有文件注入 worker。宿主环境模板为 [`visualization-sandbox.env.example`](visualization-sandbox.env.example)。上线前须按[P6报告](../../docs/VISUALIZATION_RELEASE_REPORT.md) 完成状态映射、备份、真实对话与灰度门禁；本仓库配置不代表生产服务已经启用。
