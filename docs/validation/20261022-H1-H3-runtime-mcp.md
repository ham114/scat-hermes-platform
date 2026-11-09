# H1–H3：运行边界、MCP 与 DeepSeek 实测

日期：2026-10-22。项目：`scat-hermes-platform`。完整状态以本记录为准；H2/H3 的全部业务验收尚未完成。

## 实现

- Hermes v0.22.3，提交 `5b147ab036d6d19131bff8d20701b5001eab844a`，MIT；源码来源及校验见 `services/hermes/upstream.*.json`。
- 框架无关只读应用服务、5 个 stdio MCP 工具、Hermes HTTP/SSE 适配器、可信 user/thread 上下文和 SQLite 运行归属；RAG 服务由旧 Tool 与 MCP 共用。
- 精确工具白名单、启动前 MCP 发现、禁用默认工具搜索桥接；仅开放 health 与 runs 的创建、状态、事件和停止路由，服务绑定回环地址。
- `.env` 仅同步获授权的 DeepSeek Key 和默认值，0600；未传其他凭据。Hermes 内部 API 使用独立随机服务 Key，保存在服务器项目 `var/secrets/`。

## 环境与依赖

| 用途 | Conda 环境 | Python | 结果 |
| --- | --- | --- | --- |
| 本机相关测试 | scat-frontend | 3.12.14 | 52 passed，Ruff 通过 |
| 服务器平台、MCP、完整回归 | scat-agent | 3.12.13 | 715 passed / 1 warning；pip check 通过 |
| 服务器 Hermes | scat-hermes | 3.14.8 | pip check 通过，文本和工具实测通过 |

平台 MCP 1.28.1，Hermes MCP 2.3.0、OpenAI 2.24.0、aiohttp 3.14.3。完整实装快照见 `environments/scat-agent.linux-packages.json` 和 `scat-hermes.linux-packages.json`；仅记录包名版本，未导出凭据或私有索引地址。快照作为实际安装证据，不代替服务级依赖设计。

环境创建的唯一目录外写入为用户已批准的 `/home/h3c/anaconda3/envs/scat-hermes`。既有 scat-agent 未安装、升级依赖；没有创建项目 venv。GitHub clone 失败后使用官方提交归档，7,641 文件完成 Git blob 校验；缓存和源码均在新项目。

## 自动化验证

本机执行：

```powershell
C:/ProgramData/anaconda3/envs/scat-frontend/python.exe -B -m pytest tests/hermes_runtime tests/scat_mcp tests/agents/test_rag_tools.py tests/core/test_deepseek_config.py -q --basetemp .tmp/h1-dev/pytest-mcp-final -o cache_dir=.tmp/h1-dev/pytest-cache --tb=short
```

结果：52 passed，9.08 秒。覆盖 POST 不自动重试、结果未知语义、持久归属、跨用户/thread 拒绝、SSE 分块中文/重连去重/断流、输出上限、工具参数校验、超时及真实 stdio initialize/list/call 生命周期。

服务器使用 scat-agent 在新项目执行：

```bash
python -B -m pytest tests -q --basetemp .tmp/h1-dev/pytest-server -o cache_dir=.tmp/h1-dev/pytest-cache --tb=short
```

结果：715 passed、1 warning，24.04 秒。提示为既有 FastAPI TestClient 对 httpx 的弃用信息，没有为消除提示改动共用环境。完整输出保存在服务器项目 `var/validation/h1-regression.log`。

## 真实模型与工具

使用服务器 scat-agent 执行 `python -B scripts/probe_hermes_runtime.py` 与带 `--with-mcp` 的第二轮；脚本启动 scat-hermes 子进程，用回环 18081 访问，结束时关闭自建进程。

| 实测 | 内容 | 实际结果 |
| --- | --- | --- |
| 文本 | 真实 DeepSeek deepseek-flash -> Hermes -> SSE | 回复内容确认为 OK；run.completed；状态 completed；无工具 |
| 工具 | 真实 DeepSeek -> Hermes -> stdio MCP，合成 TEST123 查询 | search_scat_flights 调用 1 次；tool.completed 1 次；工具错误 0；回复明确 synthetic；run.completed |

两轮均报告 child_stopped=true。证据摘要见 [JSON](20261022-H1-H3-evidence.json)，服务端原始报告位于 `var/validation/hermes-{text,mcp}-probe.json`。耗时包含服务初始化和关闭，不能视为模型吞吐或线上延迟基线。

首测发现独立 API 不包含完整 Gateway 的 MCP 发现流程，随后又发现上游使用双下划线名称和默认延迟工具披露。均通过显式配置和固定版本 API 适配解决；工具集合校验始终保留，失败未被记为成功。

## 验收范围

已验证的是服务启动、真实外部模型、HTTP/SSE 和一次真实 MCP 工具协议闭环。合成结果不证明业务数据可用；五工具的真实 MongoDB/RAG 复核、取消/恢复实测、20 题评估、FastAPI/门户/Nginx SSE、artifact 和 Compose 尚待完成。

运行归属已按 user/thread 建模，但当前 MCP 固定单用户上下文。开放多用户前仍须处理 runtime/profile、文件/记忆隔离和可信 MCP 身份传递。

临时文件、测试 profile 与进程清理结果另记在开发日志；版本快照、源码、私有配置和验证记录为保留交付物。
