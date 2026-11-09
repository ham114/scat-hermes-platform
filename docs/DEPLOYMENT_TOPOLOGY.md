# 服务部署结构与默认模型

2026-11-09当前状态：新HTTPS8444身份门户已开放，七核心仍为固定Conda镜像；HYN/ZYJ独立业务空间，admin管理。GBAR已切换至`scat-gbar-conda-production-1`，固定Conda Python3.11.17、Docker原生CDI、只读权重与驱动/CDI启动守卫，旧GPU服务保留回滚；服务器和Docker守护进程未重启。RAG18133仍沿用宿主既有Conda，未完成其容器迁移。详情与准入边界以[最新审查](RELEASE_READINESS_REVIEW_20261109.md)及[本轮实施记录](HERMES_RELEASE_READINESS_20261108.md)为准，下面是历史选型与目标。

更新：2026-10-27。H7-B CPU路线已经实际部署五核心+两可选作者Docker容器，三类Conda镜像、共享持久卷、权限、真实主子Agent合成流程及删除重建恢复通过；详细当前配置以 [H7-B部署记录](HERMES_H7B_CONTAINER_DEPLOYMENT.md) 为准。新入口回环18120–18128，当前host网络连接回环Mongo，未接管正式Nginx。RAG18133沿用既有Conda服务，GBAR沿用现有BentoML容器；标准GPU迁移与H8切换、前端仍单列。下文保留初期选型和长期目标，不能用它覆盖专项中的实际部署状态。

## 1. 第一阶段：外部 DeepSeek，Nginx 统一入口

先用 DeepSeek 外部 API 完成业务集成与本地开发验证。本地 LLM 不作为首版开发或验收前置条件，已下载权重保留供后续替换和对比。

```text
客户端 -> Nginx -> Vue / FastAPI（身份、对话、确认、状态、结果）
                       +-> Hermes 原生 Skills -> DeepSeek
                       |       +-> 受控操作 / MCP / 技能脚本
                       |                    +-> 共享业务模块 / Mongo / RAG / 模型后端
                       +-> 持久技能队列 -> worker（生命周期）
                                          +-> 独立 Hermes 后台技能会话 -> 同一执行资源
```

客户端只连接 Nginx，不直接访问 Hermes、MCP、DeepSeek、数据库或模型服务。DeepSeek API Key 只存在于需要调用模型的服务端进程中；不使用 VITE_* 配置密钥。Nginx 负责入站请求，服务端到 DeepSeek 的出站请求不必绕回 Nginx。

## 2. 默认配置

根 `.env.example` 是版本管理中的模板；根 `.env` 是忽略提交的本机配置。只需补入：

```dotenv
DEEPSEEK_API_KEY=
```

其他模型默认值：`DEFAULT_MODEL=deepseek-flash`、`DEEPSEEK_BASE_URL=https://api.deepseek.com`。现有 LangGraph 过渡入口的 DeepSeek 请求使用流式输出、60 秒请求超时、最多 2 次 SDK 重试，Flash/Pro 默认关闭思考模式，以便先验证工具循环。重试不代表业务任务可以重复提交。

Hermes 已通过 `scripts/run_hermes_api.py` 独立启动，平台入口为 `scripts/run_platform_api.py`；配置、实际环境及端口见 [API 与门户指南](HERMES_API_AND_PORTAL.md)。后续仅将 DEEPSEEK_API_KEY 等所需变量注入该服务，不自动读取所有应用凭据。空 Key 不进行自动付费测试，也不回退使用其他厂商的 Key。

开发后端默认监听 `127.0.0.1:18080`，避免与旧平台 8080 冲突；该端口是上游地址，客户端仍经 Nginx 访问。本机未配置业务数据库时，填写 LLM Key 不等于整个业务平台已经可运行。

默认模型名和接口已于本日核对 [DeepSeek 官方文档](https://api-docs.deepseek.com/zh-cn/)，模型名仍需在首次带 Key 集成时验证。无 Key 阶段只做离线配置和请求契约测试。

## 3. 理想结构中的容器划分

| 服务 | 目标放入 Docker | 容器内环境 / 持久化 | 初期处理 |
| --- | --- | --- | --- |
| Nginx / Vue 成品 | 是，入口容器 | Nginx；Vue 在 Node 构建阶段生成静态文件；证书和配置独立管理 | 复用服务器现有 Nginx，不同时争用 80/443 |
| 平台 FastAPI | 是，独立容器 | 固定 Conda Python 3.12；检查点和结果目录持久化 | 本机/服务器先在 scat-agent 调试，随后容器化 |
| Hermes | 是，独立容器 | 独立 Conda 环境；持久化独立 HERMES_HOME | S1–S6 接入并验证原生技能、前后台隔离及任务执行，再容器化 |
| SCAT MCP Server | 是，独立容器 | 可复用平台业务镜像与依赖，使用不同启动入口 | 先以独立进程验收，再容器化 |
| 后台任务执行器 | 是，独立容器 | 调度部分可复用平台镜像；明确执行并发和租约 | H5 基础保留；S4 改为调用独立 Hermes 技能 job，容器化留 H7 |
| RAG 服务 | 是，独立容器 | 独立 Conda / Torch 环境；权重只读、索引独立持久化 | 先复用现有服务；若需 GPU，按 GPU 条件验收 |
| GBAR 等模型服务 | 是，按模型后端分别隔离 | 对应独立模型运行环境、GPU 和只读权重 | 复用已有后端，不把模型依赖装进 Web 镜像 |
| 本地 llama-server | 后续可选 GPU 容器 | 原生 C++ / CUDA 运行时；只读挂载 GGUF | 第一阶段不启动，不申请 LLM GPU 资源 |
| Redis | 需要共享协调时单独容器 | Redis；按其用途决定持久化策略 | 单实例 POC 不强制增加 |
| MongoDB | 独立数据库基础设施；新建部署可容器化 | 独立持久卷、认证与备份 | 复用现有宿主数据库，不为统一 Compose 迁库 |
| Vite 开发服务器 | 开发时可选，部署时无须常驻 | 本机 Node.js 24 / npm 11 | Nginx 代理开发服务器；发布后只提供静态成品 |
| Streamlit | 可选独立容器 | 固定 Conda；沿用平台认证与结果 API | 仅作过渡工作台，按需保留 |

FastAPI、MCP 和 worker 可以使用同一个业务镜像，但分别配置 command、权限、资源和健康检查。Hermes、RAG 和模型服务使用各自镜像，避免把所有依赖打进一个镜像。共享源码只说明逻辑复用，不表示共用内存状态；确认与任务状态要使用已验证的共享存储。

## 4. 开发环境与容器的关系

本机与远端开发统一使用 Anaconda；Python 服务镜像内同样使用固定 Conda 环境。镜像构建时从环境定义重建依赖，不直接挂载 `/home/h3c/anaconda3/envs`。项目中不创建 venv。

初期用宿主 Conda 进程方便调试；完成契约后转入 Compose 管理。Compose 负责生命周期、网络、挂载与健康检查，Conda 负责 Python 环境，两者不冲突。MongoDB、Nginx、Redis 和原生 llama-server 不是 Python 服务，不需要 Conda。

## 5. Nginx 与网络边界

- 页面、REST 和聊天流都通过 Nginx。平台聊天路径转发到 FastAPI，由 FastAPI 调用 Hermes；不直接把 Hermes 对公网开放。
- API 路径保留服务实际前缀；SSE 关闭代理缓冲并配置合理读取超时。开发时代理 Vite 热更新所需 WebSocket，Streamlit 启用时也需 WebSocket。
- 长期桥接网络方案使用服务名访问，不把容器localhost当宿主机。当前已验收的单机H7-B显式采用host网络，所有服务绑定127.0.0.1，允许复用宿主回环Mongo和RAG；没有桥接容器间网络隔离，不对外发布内部端口。
- 需要调用 DeepSeek 的容器保留必要 DNS / HTTPS 出站能力；不能因设置完全隔离网络而切断模型 API。
- 现有MongoDB继续只监听宿主回环；桥接容器不能直接使用宿主回环地址。H7-B已验证上述host网络过渡方案，不为连通性将数据库开放到所有网卡；改桥接/多机需另外设计受控连接和状态存储。
- 身份由可信认证流程产生，Nginx 覆盖伪造身份头，FastAPI 再强制授权；统一入口本身不等于已有登录与用户隔离。

## 6. 实施顺序

1. 填写 DeepSeek Key，验证现有对话入口的外部模型调用；本轮不进行带 Key 请求。
2. Hermes 接外部 DeepSeek，完成 MCP 五类只读工具，再接 FastAPI 与前端。
3. 容器化 FastAPI / Hermes / MCP，复用现有 Nginx、数据库及外部业务服务。
4. 加入任务执行器、RAG 和模型服务的容器管理与故障恢复；按需启用 Redis。
5. 如需全新独立部署，再安排 Nginx 容器接管与数据库部署。已有环境切换须有维护和回滚方案。
6. 最后接入本地 llama-server，另做模型协议、质量、GPU 共存与性能验收。

原有本地推理记录与 GPU 容器限制仍然有效，但不再阻塞 DeepSeek 路线的首版开发。详见 [开发计划](../SCAT_HERMES_PLATFORM_DEVELOPMENT_AND_TEST_PLAN.md) 和 [Compose 说明](HERMES_COMPOSE_DEPLOYMENT_NOTES_20261022.md)。

H5 当前实现与最终目标的对应关系：宿主 Conda 上已有独立 worker、人工确认和结果恢复；Hermes 通过私有 Conda CLI 接入任务准备。未来 Compose 必须同时提供平台、工具桥、worker 可见的同一状态卷；当前 SQLite 队列仅适合同一主机部署，不能直接推广为跨主机调度。见 [H5 运行指南](HERMES_H5_MODEL_TASKS.md)。

## Skills 架构补充（v2.0 目标）

业务执行统一由原生 Skills 组织，数值与数据访问复用已有服务，worker 管理租约/回执而不另建业务调度图。宿主 Conda 开发继续使用现有 CLI 工具桥；H7 分容器时必须把依赖宿主绝对 Conda 路径的桥接改为私有 MCP/HTTP 动作服务，让业务脚本在业务环境执行，不能直接把宿主路径写进 Hermes 容器，也不能挂载整个宿主 Conda 代替可重建镜像。两种环境都要共享同一技能发布身份和持久回执，历史 H5 任务先排空/核实再切换。

完整环境矩阵、逐步骤执行与代码复用见 [Skills 方案](HERMES_SKILLS_ARCHITECTURE_AND_REUSE_20261023.md)。
