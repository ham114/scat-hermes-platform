# 配置说明

应用设置定义在 `src/core/settings.py`。将根目录 `.env.example` 复制为 `.env` 后，仅在部署机器填写实际值。Pydantic 配置忽略空字符串，因此空白配置项可能回落到代码中的默认值；空白不表示已经连接生产服务。

## 必要设置

| 配置 | 用途 | 配置方式 |
| --- | --- | --- |
| `HOST`、`PORT` | 完整后端的监听地址和端口 | 模板使用回环地址；uvicorn CLI 入口使用命令行参数 |
| `FLIGHT_MONGO_URI` | SCAT/UAV 业务数据库连接 | 在本机填写；代码缺省连接本机 MongoDB |
| `FLIGHT_MONGO_DB` | 业务数据库名称 | 根据自己的数据导入结果填写 |
| `DATABASE_TYPE` | 对话检查点后端 | 默认 SQLite；其他后端需相应连接配置 |
| `SQLITE_DB_PATH` | 对话检查点持久文件 | 生产设置为发布目录以外的绝对路径 |
| `AUTH_SECRET` | 服务端共享 Bearer 凭据 | 团队部署前生成并仅保存在受限配置中 |
| `ANALYSIS_IDENTITY_MODE` | 结果与任务的用户身份来源 | 本机可用 `single_user`；团队使用 `trusted_proxy` |
| `ANALYSIS_SINGLE_USER_ID` | 本机固定身份 | 单用户模式必填；工作台与后端使用相同值 |
| `ANALYSIS_LEGACY_OWNER_ID` | 无历史 owner 记录的迁移归属 | 仅在受控数据迁移时配置 |

当 `AUTH_SECRET` 为空时，API 的共享 Bearer 校验不启用。`single_user` 也不提供团队账号隔离。生产环境必须完成网关认证和服务端身份配置后再开放访问。

业务 MongoDB 与对话检查点是两个不同用途的存储。选择 SQLite 仅决定对话检查点后端，不能代替业务 MongoDB。SQLite 模式下的长期 Store 使用进程内存；如需要持久长期记忆，应另外选择受支持的存储并验证。

## 语言模型

完整 Agent 服务需要一个可用的语言模型提供方，语言模型文件与部署凭据不在仓库内。

第一阶段默认使用外部 DeepSeek。根 `.env` 已预留空的 `DEEPSEEK_API_KEY=`，由你填写；模板默认 `DEFAULT_MODEL=deepseek-flash`、`DEEPSEEK_BASE_URL=https://api.deepseek.com`，无需启动本地 LLM。Flash / Pro 在现有过渡入口默认关闭思考模式。模型与接口名称已核对 [官方文档](https://api-docs.deepseek.com/zh-cn/)；填写 Key 后仍需首次真实调用验收。

Hermes 独立服务由 `scripts/run_hermes_api.py` 启动，从私有 .env 提取 DeepSeek 模型配置；真实业务与 FastAPI 链路已通过。参考 YAML 仅作说明，实际 profile 由启动器生成。客户端经 Nginx 统一访问平台，密钥不下发浏览器。部署边界见 [服务部署结构](DEPLOYMENT_TOPOLOGY.md)。

- 使用受支持的托管提供方时，在 `.env` 配置该提供方的 API key；`DEFAULT_MODEL` 的可用值见 `src/schema/models.py`。
- 使用 OpenAI 兼容服务时，设置 `COMPATIBLE_MODEL`、`COMPATIBLE_BASE_URL`、`COMPATIBLE_API_KEY`，可使用 `DEFAULT_MODEL=openai-compatible`。
- 使用 Ollama 时，填写自己部署的 `OLLAMA_MODEL`，并按需配置 `OLLAMA_BASE_URL`。
- `USE_FAKE_MODEL` 仅用于本机连通性检查，不产生真实模型结果。

未配置语言模型时，可以运行 `service.scat_only:app`。完整服务会校验默认 Agent 所需的模型配置，缺失时拒绝启动。

## 预测模型

`SCAT_GBAR_V23_ENABLED=false` 为缺省状态。应用只提供模型注册、输入输出契约、通信和加载适配代码，不包含外部模型架构、权重、训练数据、模型专用配置或运行镜像。

启用 `bento_http` 后端，需要在私有配置中填写 `SCAT_GBAR_V23_BENTO_ENDPOINT` 和与部署制品对应的 release ID、源码树、运行时及 schema 摘要。应用执行模型身份检查；任意填写摘要不能替代实际制品准备。

启用 `subprocess` 后端，需要另行提供隔离 Python 运行环境、外部模型源码目录、模型配置、权重、worker 入口、来源说明及相关 SHA-256。对应键以 `SCAT_GBAR_V23_` 开头，参见 `.env.example` 与 `src/scat_model_runtime/production.py`。GPU/CUDA 依赖在模型环境独立安装。

未配置真实模型时，数据查询和确定性基线能力可按各自依赖使用，不能将其结果当作 GBAR 模型预测。

## 知识检索与可选服务

- `SCAT_RAG_BASELINE_ENABLED` 默认关闭；启用前独立准备检索服务、模型、语料和索引，并设置 `SCAT_RAG_BASELINE_URL`。
- `REDIS_URL` 留空时使用单进程后备方案；MongoDB 仍是业务记录的权威存储。多实例扩容需单独验证共享协调行为。
- 第三方集成、语音和观测平台的密钥均留空，按需配置。
- `SCAT_EXPERIMENT_ARTIFACT_DIR` 指定不可放入 Git 的可写结果目录。

## 前端与工作台

`AGENT_URL` 是工作台访问后端的服务地址；`PORTAL_URL` 和 `ANALYSIS_VISUALIZATION_BASE_URL` 是页面跳转地址。本机使用默认端口时可沿用默认值；同域生产部署通常设置后两项为 `/`。

前端通过同域 `/api` 访问后端。开发时可设置 `SCAT_DEV_PROXY_TARGET`，Vite 代理在服务端读取 `AUTH_SECRET`。生产静态构建不提供这层开发代理，需要由网关转发。

`VITE_STREAMLIT_URL` 仅允许公开导航 URL，本机可设置为工作台地址，旧工作台同域路径应与 Hermes 的 `/assistant` 区分，例如 `/legacy-chat/`。所有 `VITE_*` 变量都可能进入浏览器资源，不得存放密码、Token 或数据库连接凭据。

## Hermes 平台接口

| 配置 | 默认值 | 作用 |
| --- | --- | --- |
| HERMES_BASE_URL | http://127.0.0.1:18081 | FastAPI 访问独立 Hermes |
| HERMES_API_KEY_FILE | var/secrets/hermes-api.key | 内部随机服务密钥，启动 Hermes 时生成 |
| HERMES_STATE_FILE | var/platform/hermes.sqlite | 平台线程、运行映射和待决准入 |
| HERMES_RESULT_STATE_FILE | var/platform/hermes-results.sqlite | 只读工具结果收据 |
| HERMES_TASK_STATE_FILE | var/platform/model-tasks.sqlite | H5 不可变准备快照、人工确认与 worker 队列 |

密钥与状态路径必须在新项目内。service.hermes_app 当前要求 single_user；trusted_proxy 的多用户语义尚未开放。运行指南见 [API 与门户](HERMES_API_AND_PORTAL.md)。服务器已在本机复用原项目的数据库认证，并启用 SCAT_RAG_BASELINE_ENABLED=true / 本机 8091；本机开发配置没有获得服务器数据库凭据。

H5 平台、Hermes 私有工具桥和独立 model-worker 必须指向同一状态文件，详见 [任务配置与恢复](HERMES_H5_MODEL_TASKS.md)。H5 确认使用持久 SQLite 事务，不依赖旧工具的进程内确认回退；生产模型仍须另行提供已审核制品与后端配置。
