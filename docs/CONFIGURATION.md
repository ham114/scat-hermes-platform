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
