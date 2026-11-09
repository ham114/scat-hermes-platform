# Hermes 集成方案与开源实现对比

日期：2026-10-21。目标框架为 NousResearch Hermes Agent。

本报告基于当前项目源码、服务器环境检查，以及下列开源仓库与官方文档。开源参考仅做源码和文档核对，未在本项目中运行验收。开发量是工程估算，不是已完成工作或交付承诺。

## 1. 结论与两类接口

推荐组合：现有 FastAPI 作为认证与前端适配入口，通过 HTTP/SSE 使用独立 Hermes 服务；Hermes 通过受控 MCP 工具使用现有业务服务。模型推理由独立 llama-server 提供。

“把工具注册到 Hermes”和“FastAPI 调用 Hermes API”分别解决两条链路，可以同时使用：

| 链路 | 作用 | 选择 |
| --- | --- | --- |
| 平台 → Hermes | 创建对话、提交消息、接收事件、取消和查询历史 | HTTP/SSE、ACP、TUI gateway JSON-RPC、Python 进程内调用 |
| Hermes → 业务 | 查询数据、准备任务、确认提交、读取结果 | 原生工具插件、MCP |
| Hermes → LLM | 获取模型推理与工具选择输出 | 独立本地模型的 OpenAI 兼容 API |

Hermes Agent API 与本地 LLM API 不同。不能仅将现有 `COMPATIBLE_BASE_URL` 指向 Hermes，就认为完成了 LangGraph 到 Hermes 的迁移：原适配器预期的是模型响应，Hermes 运行的是完整 Agent 循环，会产生两层工具编排。

## 2. 开源参考

### 2.1 官方 hermes-example-plugins

- 仓库：https://github.com/NousResearch/hermes-example-plugins
- 核对源码：https://github.com/NousResearch/hermes-example-plugins/blob/main/plugin-tool-example/__init__.py
- 实现：JSON Schema、参数校验、确定性 handler，通过 `ctx.register_tool()` 注册；结构化成功与错误结果由工具辅助函数产生。
- 可借鉴：SCAT 工具的原生插件包装、结果封装、独立分发。
- 局限：参考示例不负责本项目的用户身份、确认码、运行台账、图表协议。
- 判断：适合快速验证已有业务工具可以被 Hermes 选择和执行。

### 2.2 1060392338/hermes-webui

- 仓库：https://github.com/1060392338/hermes-webui
- 核对源码：https://github.com/1060392338/hermes-webui/blob/main/app.py
- 实现：Python FastAPI、Bridge 抽象、Hermes ACP 子进程、JSON-RPC 请求与响应；聊天通过 StreamingResponse 输出 SSE。
- 可借鉴：Python 后端连接 Hermes 进程、事件转换、运行生命周期。
- 局限：规模较小，不能据此认定已满足团队多用户、跨用户资源隔离、后台作业恢复等要求；项目说明里的传输表述需结合实际源码判断。
- 判断：与本项目技术栈最接近的桥接参考，但应借鉴结构并重新落实会话隔离，而不是整体替换现有平台。

### 2.3 jozef-barton/the-kitchen

- 仓库：https://github.com/jozef-barton/the-kitchen
- 架构文档：https://github.com/jozef-barton/the-kitchen/blob/main/docs/specs/architecture.md
- 实现：Node.js bridge、Hermes CLI、SQLite；浏览器使用 Vite/React/Chakra，经 HTTP/SSE 访问 bridge；Zod 校验共享协议。
- 可借鉴：Agent 运行时与业务 UI 分离，显式协议、事件、持久化和失败状态。
- 局限：本项目已有 FastAPI/Vue，无需额外引入 Node/React 后端或复制其持久化表；文档标明 pre-1.0，协议可能变化，CLI 桥接须固定版本。
- 判断：借鉴独立适配层，不迁入整套技术栈。

### 2.4 tadata-org/fastapi_mcp

- 仓库：https://github.com/tadata-org/fastapi_mcp
- 核对源码：https://github.com/tadata-org/fastapi_mcp/blob/main/fastapi_mcp/server.py
- 实现：从 FastAPI/OpenAPI 生成 MCP 工具，经 HTTPX ASGITransport 调用原应用，提供操作与标签筛选、认证配置和请求头转发设置。
- 可借鉴：将已有航班查询 REST 接口快速发布为 MCP，复用参数与响应模型。
- 局限：只能转换已有 API，不能自动发布 `agents/model_tools.py` 中尚未成为 API 的任务准备、确认、提交逻辑。HTTPX 客户端默认超时也须与业务一致。
- 身份注意：所核对源码的默认转发头只含 authorization；本项目的可信用户头和请求身份上下文需要显式设计，不能认为 Bearer 转发等于完成用户隔离。
- 判断：适合只读查询 POC；完整任务业务优先编写显式 MCP 工具。

### 2.5 Hermes 官方 API / Gateway

- 文档：https://hermes-agent.nousresearch.com/docs/developer-guide/programmatic-integration
- 接口：HTTP/SSE API、ACP、TUI gateway JSON-RPC；均驱动同一 AIAgent 核心，也可 Python 进程内嵌入。
- HTTP API 提供聊天，以及运行创建、事件、审批、停止等接口。具体功能需要按固定版本的 capabilities 验证。
- 判断：现有 Web 平台优先验证官方 HTTP 服务，避免自己解析 CLI 展示文本。

## 3. 可行组合与开发量

估算前提：一名熟悉现有代码的工程师；POC 为 5 个代表工具、单用户、新会话；完整业务版本覆盖当前 8 个模块的 52 个工具定义、用户隔离、跨轮确认、后台任务、图表与结果恢复及回归测试。不包含模型下载、推理环境故障处理、全新前端、完整旧会话迁移和生产发布审批。环境或版本兼容问题会额外增加时间。

| 方案 | 调用结构 | 技术栈 | POC | 完整业务版本 | 主要代价 |
| --- | --- | --- | --- | --- | --- |
| A 原生插件，使用 Hermes 自带入口 | Hermes → 插件 → 业务服务 | Hermes 插件、Python、Pydantic、现有 Service/Repository | 1–3 人日 | 6–10 人日，不含接回原对话 UI | 平台工具代码与 Hermes Python 环境耦合；无现有 UI 集成 |
| B FastAPI 进程内嵌入 | FastAPI → AIAgent → 插件/工具 → 业务 | FastAPI、Python AIAgent、异步/线程适配、SSE | 3–5 人日 | 12–18 人日 | 会话实例、同步与异步、配置全局状态、依赖与故障边界 |
| C 独立 HTTP 服务＋MCP | FastAPI → Hermes HTTP；Hermes → MCP → 业务 | FastAPI、HTTPX、SSE、Hermes API、Python MCP SDK、现有存储 | 4–7 人日 | 12–20 人日 | 用户身份传递、会话映射、事件转换、多服务托管 |
| D ACP/Gateway 桥接＋MCP | FastAPI → 结构化 RPC → Hermes；Hermes → MCP | FastAPI、ACP/JSON-RPC、子进程或 WebSocket、SSE、MCP | 5–8 人日 | 15–25 人日 | 子进程管理、RPC 请求关联、断线重连、生命周期 |
| E 保留 LangGraph，Hermes 处理特定任务 | LangGraph → Hermes 服务 → MCP | 现有 LangGraph、Hermes HTTP、MCP | 3–5 人日 | 8–14 人日，仅限选定任务域 | 双重编排、状态与超时链条更复杂；Hermes 尚非主入口 |

这些方案是替代估算，不能相加。A 不包含原聊天 UI，因此不能仅比较天数判断其比 C 更适合完整平台。C 的最终工期需先完成身份与结构化结果的小规模技术验证。

## 4. 对当前项目的具体改动

### 4.1 抽出任务应用服务

当前 `scat/`、`uav/` 已有 Service/Repository。模型与实验工具仍包含较多应用流程，应抽出工具无关的服务：

- 准备与校验任务、冻结参数。
- 签发、消费和撤销确认码。
- 创建运行记录、入队与取消。
- 保存并恢复 AnalysisResult / artifact。

LangChain 工具、MCP 工具和必要的 REST 接口共同调用该层。先可使用现有 Tool.ainvoke 作为临时桥接，但不能在多个进程中各自维护互不共享的确认状态。

### 4.2 定义业务执行上下文

服务端从认证信息建立用户身份、平台会话 ID、请求 ID，明确映射到 Hermes session。用户身份不是模型可填写的工具参数；会话标识本身也不构成授权证明。确认码继续绑定原用户和会话，并保留 TTL 和一次性消费。

可选实现是短时、限定受众的服务端授权上下文，或受控的会话到主体映射；需验证所固定 Hermes/MCP 版本能否正确传递每次请求上下文。单个静态 MCP 账号不能天然代表所有平台用户。

### 4.3 平台增加 Agent 运行时适配接口

让 FastAPI 路由面向应用自己的 invoke、stream、history、cancel 语义，分别实现 LangGraph 与 Hermes 适配器。不要要求 Hermes 伪装成 CompiledStateGraph，也不要让已有 get_model() 承担整套 Agent 生命周期。

`src/service/service.py` 当前依赖 aget_state、LangChain 消息和 LangGraph 流式事件；`src/service/agui.py` 使用 ag-ui-langgraph。Hermes 的 HTTP 事件不能直接交给这些转换器，需补新的映射。

### 4.4 保留图表与任务的权威数据

完整图表仍通过 AnalysisResult 和 artifact API 提供；Agent 侧优先返回紧凑摘要与 artifact_id。任务 run_id、Hermes 对话 run_id 分别存储。LLM 不负责重新生成预测数组或评估指标。

### 4.5 选择性发布 MCP

自动转换只读 REST 时使用操作白名单；不自动发布 /invoke、/stream、管理接口等，避免工具反调对话入口形成递归。任务能力使用显式、有界的工具接口。

## 5. 推荐落地顺序

1. 固定 Hermes 与 MCP SDK 版本，启动隔离验证入口。
2. 先实现航班搜索、概况、轨迹、数据质量、知识检索 5 个代表工具。
3. Hermes CLI 经 MCP 验证自然语言工具选择，先解决业务链路。
4. FastAPI 通过 Hermes HTTP 服务接回现有 UI，验证流式事件和会话映射。
5. 加入模型 prepare/submit/status/artifact 闭环，先验证两用户、跨轮、重复确认和结果恢复。
6. 逐域扩展剩余工具；内部模型/实验 LangGraph 可继续作为确定性执行组件。

若当前目标只是最快展示 Hermes 开发能力，可先做 A 的只读 POC，再复用同一应用服务推进 C。若目标是正式替换现有对话入口，直接按 C 设计，避免先将业务大量绑定到 Hermes 私有实现。

## 6. 本地模型文件任务

现有文档与服务器目录一致：embedding、reranker 均位于 `/mnt/h3c/scat-rag/models/`，其中 llm 子目录可写。

本轮已启动独立下载：

- 目录：`/mnt/h3c/scat-rag/models/llm/Qwen3.6-35B-A3B-GGUF-7878dc9a905c/`
- 文件：`Qwen3.6-35B-A3B-Q4_K_M.gguf`
- 仓库版本：`7878dc9a905c7c9379e5e8d6a1aa9affdd6839c5`
- 预期大小：20,419,565,568 字节。
- 官方 SHA-256：`671e47e0ec53c665d048b98c3ecbfd5236b5ca9c3e02ed19fc8f81f7b85140c7`
- 来源校验页面：https://huggingface.co/ggml-org/Qwen3.6-35B-A3B-GGUF/blob/main/Qwen3.6-35B-A3B-Q4_K_M.gguf
- 官方源连接超时，已改用 hf-mirror 下载同一固定版本；镜像元数据中的大小、SHA 与官方页面一致。
- 支持断点续传；使用 .part 暂存，只有大小、GGUF 文件头和 SHA-256 全部通过后才重命名为正式文件。
- 进度：同目录 `download-status.json`；错误：`curl.log`、`download.log`。

任务已实际传输数据；本报告写入时尚未完成整文件校验。不修改已有 RAG 权重、驱动、运行配置或生产进程。模型下载不等于推理服务已经部署。

2026-10-22 更新：已恢复中断下载，整文件 SHA-256 通过，并完成隔离 V100 推理与模拟工具调用测试。具体结果和接口兼容边界见 [推理验证](LOCAL_LLM_INFERENCE_20261022.md)；C 方案的分阶段容器管理建议见 [Compose 部署说明](HERMES_COMPOSE_DEPLOYMENT_NOTES_20261022.md)。
