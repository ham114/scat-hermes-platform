# Hermes C 方案的 Docker Compose 部署建议

日期：2026-10-22。本文为部署设计与当前环境核查，不代表已经部署 Hermes 或迁移生产服务。

## 当前默认路线

第一阶段采用外部 DeepSeek API，Nginx 统一接收客户端请求，本地 llama-server 不启动。目标容器划分及初期复用方式以 [服务部署结构](DEPLOYMENT_TOPOLOGY.md) 为准；下文 GPU 内容保留作为后续本地模型阶段的前置条件。

## 建议

建议单服务器的 C 方案采用 Compose 管理服务生命周期、网络、持久目录、健康检查和固定版本。第一阶段保留现有数据库、入口网关和业务服务，在隔离端口验证新增组件；待 GPU 容器运行条件验收后再统一管理推理服务。

| 组件 | 职责 | 初期处理 |
| --- | --- | --- |
| 平台 FastAPI | 认证、业务接口、会话映射、Hermes 事件适配 | 保留现有服务，新增验证入口 |
| Hermes | 对话编排、工具选择、会话与技能 | 独立服务、独立环境、独立状态目录 |
| SCAT MCP | 发布受控业务能力、验证执行上下文 | 可独立进程；与平台共用应用服务层 |
| llama-server | 本地 LLM 推理 | 先隔离验证，之后构建固定 CUDA 12.x / sm_70 镜像 |
| MongoDB | 现有业务、运行、结果存储 | 先复用，不在本次迁移数据或另起空库 |
| Redis | 需要时共享确认码、缓存、协调 | 按实际多实例需求启用，不作为默认新增项 |
| Nginx | HTTPS、鉴权入口、流式代理 | 初期保留现有部署，SSE 路径关闭缓冲 |

原项目的 compose.yaml 是 PostgreSQL、agent_service、Streamlit 的模板组合，未迁入新项目；新项目尚无可执行的 Compose 服务定义。应新增独立的 Compose 项目并固定项目名，不在当前生产端口直接执行替换。

## 当前服务器的实测限制

- Docker Compose 命令可用，版本输出为 v5.5.1。
- Docker 注册运行时只有普通 runc 类运行时。
- 标准 `docker run --gpus all ... /bin/true` 返回 `failed to discover GPU vendor from CDI: no known GPU vendor found`。因此不能认为现有 Docker 已具备标准 GPU 请求能力。
- 宿主 NVIDIA 内核模块和 NVML 用户态库版本不一致；此前及本轮 PyTorch CUDA 检查可用，但 nvidia-smi 的问题仍需维护窗口处理。
- Docker RootDir 为 `/var/lib/docker`，所在系统盘之前实测使用率 95%。模型放到 /mnt 不会自动把镜像层与容器日志迁过去。
- MongoDB 与现有业务 API 监听回环地址。桥接网络中的容器不能仅通过 host-gateway 就访问宿主 127.0.0.1 服务；需要明确认证与连接边界，可选受控代理或经设计的过渡期 host 网络。不要为接入直接开放数据库到所有网卡。

## GPU 镜像与本次诊断的区别

官方 llama.cpp b11796 CUDA 12.8 Linux 包要求 GLIBC_2.38，宿主 Ubuntu 22.04 不满足。本轮使用隔离 Ubuntu 24.04 基础镜像，并只读挂载已校验运行包、CUDA 驱动库和现有 OpenMP 库；通过显式设备映射识别 V100。

该手动设备与宿主库映射只是兼容性诊断，不作为生产部署模板。正式镜像应自带明确的 C++/OpenMP/CUDA 用户态依赖，固定镜像摘要，保留 sm_70 支持，并通过维护与验收完成标准 NVIDIA Container Toolkit / CDI 接入。

## Python 环境纪律

本机、服务器和后续 Python 服务镜像统一使用 Anaconda / Conda 环境，明确解释器路径，不在项目目录或镜像启动过程中创建 venv。Hermes 安装方式须先核查，不能默认运行自动建环境的安装脚本。原生 llama-server 不依赖 Python 环境。具体以 [开发与测试计划](../SCAT_HERMES_PLATFORM_DEVELOPMENT_AND_TEST_PLAN.md) 为准。

## 配置要点

1. 模型以只读 bind mount 引入，不 COPY 到镜像；Hermes 会话、模型缓存、分析结果与运行日志分别持久化。
2. 配置、令牌通过受限配置文件或 secrets 注入，不写入镜像、前端变量或 Git。
3. 内部服务使用稳定的服务名和容器端口；只把入口端口发布到需要的地址。容器内 localhost 指向容器本身。
4. LLM 健康检查应确认模型已加载并可接收请求，启动宽限期覆盖实际加载时间；使用 depends_on 的 service_healthy 条件。
5. Compose 启动顺序不替代运行中的重试与断线恢复；healthcheck 变为 unhealthy 也不等于自动重启。分别设计进程退出重启策略和服务退化行为。
6. 固定版本与摘要，使用单实例 Hermes/MCP 验证用户与会话上下文，再考虑横向扩展；进程内确认状态不能在多个副本间自动共享。
7. 一张 V100 同时承担 LLM、RAG 和 GBAR。Compose 的 GPU 设备选择不提供显存配额或跨服务排队；继续使用应用层并发控制，并实测共存显存峰值。不能通过多开容器增加 GPU 容量。
8. 设置日志轮转与磁盘告警；Docker 数据目录迁移需要单独方案和维护窗口，不在推理验证中修改 daemon 配置或重启 Docker。

## 分步落地

1. 先验证外部 DeepSeek 文本与工具协议，完成 CPU 业务服务闭环；本地模型验证后置。
2. 固定 Hermes、MCP、推理镜像与协议版本，在隔离入口建立对话到业务工具闭环。
3. 完成 GPU 驱动组件统一、标准 GPU 容器请求、磁盘与镜像存储方案。
4. 验证健康检查、重启、会话持久化、用户隔离、任务确认、图表恢复和 GPU 资源竞争。
5. 在维护窗口逐服务切换，保留原服务配置和回滚路径。

参考：

- Docker Compose GPU：https://docs.docker.com/compose/how-tos/gpu-support/
- Compose 启动与就绪顺序：https://docs.docker.com/compose/how-tos/startup-order/
- llama.cpp 发布：https://github.com/ggml-org/llama.cpp/releases/tag/b11796
