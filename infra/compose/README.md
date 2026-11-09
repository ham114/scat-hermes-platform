# Compose 实际部署入口

2026-10-27：`compose.yaml` 已在服务器以 `scat-hermes-h7b` 运行并验证，默认platform/hermes-main/actions/worker/hermes-worker五核心，`author` profile再启用author-api/hermes-author。三类镜像从已有Anaconda固定制品离线重建，未使用venv/uv、宿主Conda挂载或Docker socket。

RAG与GBAR继续复用现有服务，**没有新增GPU镜像上线**，迁移条件见 [GPU_MIGRATION.md](GPU_MIGRATION.md)。既有Nginx/Mongo与正式18081/18133保持；七CPU容器都只监听回环地址。候选采用host网络连接回环Mongo，这是单机过渡边界，不是桥接网络隔离。

完整命令、逐容器配额、挂载/Key边界、构建锁、故障/恢复证据和版本回滚见 [H7-B部署记录](../../docs/HERMES_H7B_CONTAINER_DEPLOYMENT.md)。使用服务器已有 `/home/h3c/anaconda3/envs/scat-agent/bin/python -B scripts/manage_container_deployment.py up --author` 启动，`status --author`检查，`down --author`只停止本项目且保留状态。首次准备必须先按专项文档生成私有配置和锁定镜像，不能直接拉占位latest镜像。

`compose.smoke.yaml`只用于隔离合成维护验收，默认不加载；验收后已移除，当前synthetic=false。`releases/20261027-f983406a8700`保留固定镜像身份、无凭据源码、Compose及宿主工具。新服务尚未接管正式网关，H8/前端/标准GPU验收单列。
