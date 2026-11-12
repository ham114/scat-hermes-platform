# Compose 部署

[`compose.yaml`](compose.yaml) 定义平台、主 Agent、任务服务、后台 Agent 和可选作者服务。服务使用已构建的固定镜像，并通过部署环境提供连接参数及持久卷。

运行前准备模型登记、数据连接、认证网关及角色环境。持久状态目录保存会话、任务、制品与活动技能包。GBAR 推理由已登记 GPU 服务提供。[部署指南](../../docs/DEPLOYMENT.md) · [GPU 登记示例](../../config/production-gpu.example.json)
