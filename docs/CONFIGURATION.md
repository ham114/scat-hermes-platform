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
