# Hermes 运行契约与开发入口

日期：2026-10-22。覆盖 H1–H3 契约及已实现的 H4 API；启动与门户使用详见 [API 与门户指南](HERMES_API_AND_PORTAL.md)。

2026-10-23 目标调整：以下运行契约仍描述已实现的 H1–H5，Skills 尚未启用；新版要求原生技能主导全部核心业务，旧模型图仅作迁移对照。实施前先做固定版本 S1 验证，详见 [Skills 执行方案](HERMES_SKILLS_ARCHITECTURE_AND_REUSE_20261023.md)。

## 版本与环境

上游固定为 [NousResearch/hermes-agent v0.22.3](https://github.com/NousResearch/hermes-agent/releases/tag/v0.22.3)，提交 `5b147ab036d6d19131bff8d20701b5001eab844a`，MIT 许可。源码身份见 `services/hermes/upstream.lock.json`、`upstream.source.json`。

服务器平台/MCP 使用现有 `scat-agent`（Python 3.12）；Hermes 使用独立 `scat-hermes`（Python 3.14）。上游声明的较宽 Python 范围包含旧解释器升级桥接，其实际依赖和运行基线要求 3.14。Hermes 的 OpenAI/MCP 等依赖与旧平台不同，不能直接升级旧环境。MCP SDK 分别为平台 1.x、Hermes 2.x，通过协议通信。

`environments/hermes-conda.yml` 定义独立环境；`services/hermes/setup_environment.py` 为本服务器的安装入口。Conda 前缀 `/home/h3c/anaconda3/envs/scat-hermes` 已获用户明确例外授权；源码 `vendor/hermes-agent`、缓存、配置、状态和日志均放在新项目。禁止 `uv sync`、`uv run` 或上游自动安装流程创建项目 venv。

GitHub 克隆在服务器失败时使用官方提交归档的选定运行文件：7,641 个普通文件均与 Git tree 的 blob SHA1 校验，22 个 PowerShell 文件仅还原 Git 规范 LF 后校验。运行归档及上游归档 SHA256、排除目录均保留在 source 记录中；不把缺少 `.git` 的副本误报为 Git checkout。

## 服务边界与启动

```text
目标：Nginx -> FastAPI -> Hermes HTTP/SSE -> MCP stdio -> 应用服务 -> 数据/检索
当前：独立 FastAPI、Hermes、MCP 与 /assistant 门户已实现并分别验证；现网 Nginx 切换和 Compose 尚待发布。
```

在服务器新项目根目录，先确认端口未占用，再执行：

```bash
conda activate scat-hermes
python -B scripts/run_hermes_api.py --port 18081
# 接入五类真实业务工具时：
python -B scripts/run_hermes_api.py --port 18081 --with-mcp --home var/hermes/business
```

以上示例二选一。服务仅监听 `127.0.0.1`，不切换现有 Nginx。启动器只从根 `.env` 提取 DeepSeek Key、模型和 URL；自动生成项目内 `var/secrets/hermes-api.key` 作为内部服务密钥。API Key 不写入 YAML、文档或结果报告。HERMES_HOME 放在项目 `var/hermes/`，禁用后台记忆和用户画像；该设置不构成完整的多租户隔离。

不带 `--with-mcp` 时模型工具集合必须为空；带该参数时必须精确等于 5 个 `mcp__scat__*` 工具。独立 API 启动前显式执行 MCP 发现，并使用固定版本命名函数生成白名单；启动器在创建 Agent 后再次核对实际工具名，不匹配则拒绝执行；禁用默认 tool_search 延迟披露桥接，五工具直接向模型公开；终端、文件、浏览器等内置工具不进入业务配置。

## HTTP 与 SSE

| 操作 | 固定版本接口 | 平台处理 |
| --- | --- | --- |
| 创建 | `POST /v1/runs`，JSON `input` | UUID 请求号构造 Idempotency-Key；不自动重试 POST |
| 状态 | `GET /v1/runs/{run_id}` | 先检查平台运行归属，再访问 Hermes |
| 事件 | `GET /v1/runs/{run_id}/events` | Last-Event-ID 恢复、序号去重、逐帧校验 |
| 停止 | `POST /v1/runs/{run_id}/stop` | 仅允许本用户和本 thread 的运行；前轮已实测 cancelled，未误标 completed |

可信 `ExecutionContext(user_id, thread_id)` 由服务端产生，使用摘要 scope 作为 `X-Hermes-Session-Key`；运行映射保存于项目内 SQLite，不保存提示词或密钥。不能让模型提交 user_id、thread_id 或原始数据库查询。

SSE JSON 字段为 `event`，文本增量为 `message.delta` 的 `delta`；最终正文在 `run.completed.output`。工具事件使用 `tool`，`tool.completed.error` 必须检查，不能仅凭事件名判成功。SSE id/JSON seq 用于重连。只有明确终态才能结束运行；EOF、传输失败或 `partial/interrupted` 不能包装成成功。单帧上限 256 KiB，上游错误仅向调用者返回脱敏代码。

POST 传输超时代表执行结果可能未知，应保留原 UUID，后续显式恢复；不得换新 UUID 静默重复提交。前轮已验证跨连接原子准入；完整进程崩溃恢复仍须独立验收。

## MCP 与应用服务

`src/application/readonly.py` 复用旧 Service/Repository，`src/scat_mcp/server.py` 仅负责工具协议。RAG 逻辑抽至 `application/knowledge.py`，旧 LangChain Tool 也调用该服务。`src/agents/__init__.py` 延迟加载注册表，单独导入 RAG 时无需拉起完整 Agent 依赖链。

| 工具 | 主要边界 |
| --- | --- |
| search_scat_flights | 结构化检索、既有分页约束 |
| get_scat_batch_overview | 指定批次的确定性统计 |
| get_scat_track_points | 默认 20 点、最多 100 点 |
| inspect_scat_collection_data_quality | 必须指定批次，首阶段禁用全库扫描 |
| search_scat_knowledge | 独立 HTTP 检索，不要求 MongoDB 可用 |

工具默认超时 30 秒，质量查询 60 秒；JSON 输出上限 64 KiB；轨迹大页优先返回明确的已读取页摘要，其他超限或摘要仍超限时明确拒绝。H5 模型任务 artifact 通过独立可信任务通道接入，详见下方补充。错误不回显后端异常及输入凭据。stdio 模式严格保持 stdout 为 MCP 协议，诊断写 stderr。

当前 stdio 进程仅服务一个固定单用户身份，thread 为私有只读工具上下文。平台客户端已有 user/thread 运行归属，但不能据此声称 MCP、Hermes 文件/记忆已支持多用户。后续须实现每用户 runtime/profile、MCP 可信上下文注入和并发隔离，再开放登录。

## 可重复实测

```bash
conda activate scat-agent
python -B scripts/probe_hermes_runtime.py
python -B scripts/probe_hermes_runtime.py --with-mcp
```

实测脚本使用真实 DeepSeek；第二条通过真实 Hermes/MCP 协议访问明确标注的合成数据，不查询实际航班。脚本自行启动并终止子进程，拒绝占用已有服务端口，报告在 `var/validation/`。这些检查不代替真实数据库、RAG、取消/恢复、20 题评估和门户 E2E 验收。


## H1/H2 验收补充

审批能力、DeepSeek 协议、重试/上下文核查、20 题判分与后续执行命令见 [H1/H2 收尾说明](HERMES_H1_H2_ACCEPTANCE.md)。本机启动器已将单次主模型调用常规尝试上限显式设为 3，auto_recovery_cycles 设为 0；启动器与修复已校验同步服务器，20 题及五项独立边界实测通过。固定版另有一次主连接恢复；本轮无工具故障夹具上限为两轮各 3 次，SDK 重试为 0。最终结论见 [验收记录](validation/20261022-H1-H2-acceptance.md)。既有 home 配置严格比对保持有效，必要时新建项目内 profile，不覆盖旧状态。


## H3/H4 结果引用

实际工具完成回调生成独立只读结果收据，通过 business.result 和 GET run 的 result_references 提供引用；平台按 owner/thread/run 回查后才转发，读取端点保留鉴权和完整性校验。默认预算与状态文件见 [API 指南](HERMES_API_AND_PORTAL.md)，验证见 [H3/H4 最终记录](validation/20261022-H3-H4-completion.md)。停止回执明确为 conversation_only，stopping 不代表已结束；独立业务任务按 H5 链路处理。


## H5 人工确认与工具分派

启用 `--with-model-tasks` 时，在原五只读 MCP 之外注册五个受控查询/准备工具，实际工具集合必须精确等于十项白名单。人工确认 API 不暴露给模型。固定 v0.22.3 原生 registry 分派传递可信 session_id，但不传 tool_call_id；任务桥用服务器绑定的 session/scope/run 和规范化参数摘要作准备幂等键，不能把 tool.completed 事件当成业务成功证明。

H5 验收核对实际持久化 pending、确认后单次执行、原图生成 artifact、平台重启恢复及跨会话拒绝。独立 worker 的租约过期/终态不明进入 unknown，不自动重跑。契约与启动命令见 [H5 指南](HERMES_H5_MODEL_TASKS.md)。
