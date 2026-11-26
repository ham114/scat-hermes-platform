# 本地与部署配置

从 [`.env.example`](../.env.example) 创建本机 `.env`。配置类型和默认值定义在 [`src/core/settings.py`](../src/core/settings.py)。

| 配置                                                                      | 用途                               |
| ------------------------------------------------------------------------- | ---------------------------------- |
| `FLIGHT_MONGO_URI`、`FLIGHT_MONGO_DB`                                     | SCAT 与 UAV 的业务数据库连接       |
| `AUTH_SECRET`                                                             | 服务端 Bearer 凭据，由部署环境保存 |
| `ANALYSIS_IDENTITY_MODE`、`ANALYSIS_SINGLE_USER_ID`                       | 开发身份或可信网关模式             |
| `DEFAULT_MODEL`、`DEEPSEEK_BASE_URL`、`DEEPSEEK_API_KEY`                  | 原生 Agent 的语言模型连接          |
| `HERMES_BASE_URL`、`HERMES_API_KEY_FILE`                                  | 平台与原生 Agent 服务的内部连接    |
| `HERMES_STATE_FILE`、`HERMES_RESULT_STATE_FILE`、`HERMES_TASK_STATE_FILE` | 会话、只读回执和任务状态位置       |

团队使用时由认证网关提供可信身份，并将内部服务保留在网关后。Vite 的 `SCAT_DEV_PROXY_TARGET` 是开发代理目标，服务端密钥保存在 `.env`。

模型任务需要与部署登记相符的模型源码、权重、配置及运行时。[GPU 登记模板](../config/production-gpu.example.json) · [Hermes 服务](../services/hermes/README.md)

## RAG 生产组合与开关

生产检索配置在 [`qwen3-production-v1.json`](../services/rag/qwen3-production-v1.json)：BGE-M3／BM25／标识符召回权重2∶1∶1，各路最多200条，候选100条；原始Qwen3-Reranker-0.6B评分后，以重排／召回名次权重4∶1、k=20做最终RRF。返回的`score`仍是原模型相关性分数，最终顺序以`rank`为准。

当前该文件与 [`real-agent-production-v1.json`](../services/rag/real-agent-production-v1.json) 都设为`"enabled": false`。前者阻止检索服务加载模型，后者阻止主Agent注册原生RAG操作。Compose及本机`.env`使用`SCAT_RAG_BASELINE_ENABLED=false`，关闭兼容知识检索入口。两个项目RAG专用systemd服务同时停用自动启动；启用需要显式调整配置和部署服务，普通重启不会开启RAG。

模型权重、向量缓存、语料、评测输出与本机凭据保留在服务器，不纳入Git。部署备份和状态由`scripts/deploy_rag_rank_rrf_disabled.py`记录在`var/rag/deployments/weighted2-rank-rrf-20261126/`及`output/rag-production-disabled-20261126/`。

## Langfuse 调用链排查

调用链使用自托管 Langfuse 与 Hermes 官方 `langfuse` 插件。插件源码版本、两个文件的 SHA256、Hermes 核心提交和 SDK 版本固定在 [`langfuse.lock.json`](../services/hermes/langfuse.lock.json)。本轮使用插件 2.0.0、Python SDK 4.14.0，实际自托管服务版本 4.86.0；升级须重新核验采集与读取接口。本项目通过 `/api/public/v2/observations` 按原生会话读取事件，每条返回事件再次核对会话归属。

| 配置 | 用途 |
| --- | --- |
| `SCAT_LANGFUSE_CONFIG` | 项目目录内的私有 JSON，包含 `base_url`、`public_key`、`secret_key`、`plugin_path`，可设 `enabled: false` |
| `SCAT_DIAGNOSTICS_STATE_FILE` | 本地调用链、任务映射及回执读取事件的 SQLite 路径；生产使用共享状态目录 |
| `HERMES_LANGFUSE_CAPTURE` | 启动时固定为 `metadata`，官方插件采集结构和模型用量元数据 |

配置缺省时不启用采集。生产私有配置在 `var/deploy/langfuse/client.json`，权限600，浏览器不接触 Langfuse 密钥。普通用户通过已有签名身份查看自己的记录，管理员身份由现有身份目录核验；不能通过请求参数切换所有者。Langfuse 原始管理服务只绑定 `127.0.0.1:18160`。

页面提供以下直接路由，不增加导航按钮：`/diagnostics`（列表）、`/diagnostics/traces/:traceId`（调用树）、`/diagnostics/sessions/:sessionId`（平台会话筛选；此参数是平台 thread_id）、`/diagnostics/tasks/:taskId`（后台任务）。列表也支持 `thread_id`、`run_id`、`state` 查询参数。接口前缀为 `/api/v1/hermes/diagnostics`，沿用 HTTPS 站点登录。

展示内容包括请求摘要、角色、状态、调用父子关系、真实起止时间、工具参数和结果的受限结构摘要、校验失败字段、回执引用和校验摘要、关联任务、模型名称及已上报的用量/费用。缺失数据标为未记录或无法估算；历史任务没有开始时间时不从创建时间推测。原始轨迹点、完整工具正文、提示词和回答不进入本轮 Langfuse 采集。页面展示事件只发送结果引用、摘要、事件ID和成功/失败状态，并在服务端重新验证所属运行和摘要。

本地补充记录失败不会中断工具执行。读取页区分等待同步、服务不可用、达到分页上限；失败时仍可查看已有本地记录。读取每个原生会话最多500条远端事件、总超时15秒。页面展示事件属于辅助观测，不能作为浏览器性能指标或最终业务正确性的证明。

错误详情现在单独显示标准原因说明、处理建议、字段级约束及错误代码。例如轨迹参数 `limit=101` 会显示 `limit 必须小于或等于100`。内部异常附带可与服务日志关联的错误编号、异常类型，以及最多5个代码文件/函数/行号；代码位置默认折叠，不采集异常原始消息、源代码行、局部变量或完整输入。模型请求失败可显示HTTP状态说明和原有重试元数据。旧记录只能按已有状态和字段补充说明，不能补造当时未记录的范围或异常栈。

用量显示区分无模型调用记录、模型调用未上报完整用量、部分调用已知和全部已知。只有 `generation` 计入模型用量；普通工具步骤不产生模型token。总览和单次模型步骤均可查看输入、输出及缓存明细。固定插件的 `input` 是未缓存输入，因此输入合计包含缓存读取和缓存写入；推理token属于输出明细，不再次加入总量。部分已知只显示已知部分，不将缺失值填零。费用继续标为估算，不代替服务商账单。
