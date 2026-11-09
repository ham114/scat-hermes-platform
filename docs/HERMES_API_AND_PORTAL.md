# Hermes API 与门户开发指南

标准Profiles补充（2026-10-24）：原生scat-main/scat-worker/scat-author已创建，profile_manage统一管理；作者Agent现可通过独立对话创建/校验草稿，只有草稿工具和只读profile_manage，发布确认仍需独立用户入口。新启动默认scat-main，旧显式home兼容；先运行初始化，再启动服务。三角色Key可以共用现有一个，具体.env路径、命令及1120 passed/1 skipped验收见 [标准Profiles](HERMES_STANDARD_PROFILES.md)。前端继续暂停，测试后新服务均关闭。

S7-A 新增独立作者 API（按需回环18104），支持草稿修订/差异、静态校验、隔离异步试跑、发布预览/确认和历史回滚；业务入口不增加作者路由，发布另需独立凭据。技能包按运行固定，前端仍暂停。完整请求格式及配置见 [S7-A 作者后端](HERMES_S7A_AUTHORING.md)；历史展示地址不表示当前常驻。

> 开发纪律与最新服务端口见 [主计划顶部](../SCAT_HERMES_PLATFORM_DEVELOPMENT_AND_TEST_PLAN.md#development-discipline)。服务器项目为 `/home/h3c/project/python/scat-hermes-platform`；Python 使用已有 Anaconda 环境，禁止项目 venv/uv；临时 profile、TMP/TEMP/TMPDIR、缓存和测试临时文件只放项目内并在本轮结束后清理。

S6 当前后端配置（2026-10-23）：新平台 `HERMES_SKILL_JOBS_ENABLED` / `HERMES_WORKFLOWS_ENABLED` 默认 true，native 不指定能力时默认 MCP、Skills、任务和工作流；升级使用独立 `--home var/hermes/skills-s6`，不覆盖旧 home。历史 H5 必须显式 `--legacy-h5`，平台回滚同时显式关闭两个开关；旧 worker 不领取 Skills 快照。S6 只推进后端，前端暂停，下面的 S2/S5 展示说明保留为历史记录，限时演示地址不代表当前服务常驻。最新验收与维护命令见 [S6 后端说明](HERMES_S6_BACKEND_ACCEPTANCE.md)。

历史入口说明（2026-10-22）：固定单用户入口已接入只读结果收据/SSE，以及 H5 模型任务准备、人工确认、独立 worker 和 artifact 恢复。动态结果编排、完整历史浏览和 Compose 仍按后续阶段推进。H5 细节见 [模型任务指南](HERMES_H5_MODEL_TASKS.md)。

2026-10-23：主计划 v2.5 的 [S2 已收口](HERMES_S2_COMPLETION.md)。可选 --with-skills 提供六个文档技能/26 项只读操作、运行级授权、有效策略和实际只读额度；模型任务同时启用时精确工具集合 33，默认 H5 仍为 10。质量技能可用，模型准备仍无匹配发布技能，按 S3/S4 迁移。使用新 home 如 var/hermes/skills-s2-final，以 --show-skills-policy 查看配置，CLI 参数与计量边界见专项。下一步 S3，H6 后续提供配置套用页面。

## 运行环境与入口

历史 S5 限时展示使用本机 `http://127.0.0.1:5173/assistant`，服务器服务仍限定回环。界面包含工具组目录、合成双模型示例、固定样本/试验摘要、持久步骤与累计预算、部分成功和原任务核实/取消。展开“技能与工具组”，填入示例后发送，待助手准备方案，再点“确认参数并执行”。实际数据/计算/后台 worker 位于服务器；用户浏览器不接收认证密钥。

展示为默认4小时、最多8小时的开发预览：服务器以已有 `scat-agent` 运行 `python -B scripts/run_workflow_demo.py --hours 4`，自动派生已有 `scat-hermes` 主服务和业务 worker；本机以已有 Anaconda base 运行 `scripts/preview_remote_frontend.py`，SSH 密码只经 stdin JSON 输入，脚本启动现有 Node24/Vite 并代理认证。不要把密码写进命令文件或文档。两个控制器均记录自身 PID、子进程与有效期到各项目 `var/demo/.../services.json`；服务器收到 SIGTERM、或时长结束时关闭归属服务并清理 `.tmp/s5-demo`。本机到期关闭 Vite/转发并清理同名临时目录。展示依赖内网和 SSH 连接，不等同 H7 常驻部署。

服务器展示使用18080（API）、18081（主 Hermes）、18099（独占后台）；本机18080是私有转发，5173是前端。S5执行须 `HERMES_WORKFLOWS_ENABLED=true` / 原生 `--with-workflows --with-skills --with-model-tasks --with-mcp`，默认普通 H5 配置未被切换。历史 H5 worker 命令必须显式加 `--legacy-h5`；当前默认 worker 领取经批准的 Skills 任务。详细契约、证据及边界见 [S5 专项](HERMES_S5_WORKFLOWS.md)。

| 服务 | 环境 | 当前入口 |
| --- | --- | --- |
| 平台 FastAPI | Anaconda scat-agent / Python 3.12 | 回环 18080，service.hermes_app |
| Hermes | Anaconda scat-hermes / Python 3.14 | 回环 18081，固定 v0.22.3 |
| 只读 MCP | Hermes 子进程，scat-agent | 私有 stdio，默认 5；Skills 扩展 26 个工具 |
| H5 模型任务工具 | 私有 scat-agent 子进程 | 5 个查询/准备工具，无确认或提交工具 |
| model-worker | 独立 scat-agent / Python 3.12 | scripts/run_model_worker.py，持久队列并发 1 |
| Vue 门户 | Node.js 24 / npm 11 | /assistant；开发时 Vite 代理 /api |
| MongoDB | 既有数据库服务 | 本机 27017，需要认证 |
| RAG | 既有检索服务及权重环境 | 本机 8091，独立于平台 Conda |
| Nginx | 既有宿主服务 | 客户端统一入口；本轮提供配置示例与隔离测试，未切换现网 |

Python 开发、测试和运行统一使用 Anaconda，不创建项目 venv。Conda 外部前缀仅沿用已获批准的 scat-hermes；源码、缓存、运行状态与日志位于新项目。Node 的 node_modules 为前端依赖目录。

在服务器 `/home/h3c/project/python/scat-hermes-platform` 分别开三个终端：

```bash
conda activate scat-hermes
python -B scripts/run_hermes_api.py --with-mcp --with-model-tasks --home var/hermes/single-user-h5
```

```bash
conda activate scat-agent
python -B scripts/run_platform_api.py --port 18080
```

```bash
conda activate scat-agent
python -B scripts/run_model_worker.py --legacy-h5
```

先启动 Hermes，使 `var/secrets/hermes-api.key` 就绪，再启动 FastAPI。业务数据和 RAG 的地址放在服务器私有 `.env`；本轮已在服务器本机复用原项目的 FLIGHT_MONGO_URI / FLIGHT_MONGO_DB，并启用已验证的本机 RAG。没有将数据库凭据传回本机，权限为 0600。

本机前端通过同机后端或 SSH 转发联调。需要转发时另开终端，使用自己的 SSH 认证：

```powershell
ssh -N -L 127.0.0.1:18080:127.0.0.1:18080 h3c@172.26.67.148
$env:SCAT_DEV_PROXY_TARGET = 'http://127.0.0.1:18080'
npm --prefix frontend run dev -- --host 127.0.0.1
```

SSH 转发需在独立终端保持运行；前端 npm 命令在本机项目根执行。打开 Vite 显示的地址并进入“分析助手”。启动脚本固定监听回环，正式入口采用 Nginx；客户端不直接访问 Hermes，也不获取内部服务密钥。启用 AUTH_SECRET 时由服务端代理配置认证，不把秘密放进 VITE_*。

## HTTP 契约

统一前缀 `/api/v1/hermes`，保留既有 `/api/v1/scat`、UAV、analysis 和 materialization 路由。

| 方法与路径 | 输入 / 用途 |
| --- | --- |
| POST /threads | `{}`，服务器生成 thread_id |
| GET /threads | 当前用户的最近会话，limit 1–100 |
| POST /threads/{thread_id}/runs | message 与稳定 UUID request_id，返回 202 |
| GET /threads/{thread_id}/runs | 当前会话的运行索引 |
| GET /threads/{thread_id}/runs/{run_id} | 状态和最终输出 |
| GET /threads/{thread_id}/runs/{run_id}/events | SSE，Last-Event-ID 续接 |
| POST /threads/{thread_id}/runs/{run_id}/stop | `{}`，请求停止对话，最终状态以事件或轮询为准 |
| GET /threads/{thread_id}/runs/{run_id}/results | 当前 run 的有效只读结果引用列表 |
| GET /threads/{thread_id}/runs/{run_id}/results/{result_id} | 按 owner/thread/run 校验后读取真实工具结果及摘要 |

可信 user_id 由固定单用户配置产生。接口不接受模型/客户端提供用户身份、Hermes session_id 或任意历史注入。所有运行访问先校验 thread 与 run 归属；HTTP 共享凭据按现有 AUTH_SECRET 规则处理。当前 profile 拒绝 trusted_proxy 模式，不能据 SQLite owner 字段声称已经支持多用户。

平台转发文本、工具状态、business.result 引用及终态字段，过滤原生 session、内部错误详情、推理内容和工具参数预览。业务结果当前页面仍显示文字，H6 再接通用 AnalysisResult/图表；本轮仅改停止说明。business.result 只含结果标识、operation、payload_sha256、created_at、expires_at、kind。平台必须按当前 scope/run 回查收据，以持久记录为准，不信任上游自行声称的归属或操作名。GET run 也返回 result_references，刷新后可查询上述结果列表。

## 并发、幂等与恢复

SQLite 保存线程归属、运行映射、请求摘要和待决意图，不保存原始提示词。BEGIN IMMEDIATE 使不同连接对同一 thread 的准入互斥；不同 thread 独立。

- 一个 thread 同时保留一个未结束或结果未知的请求。新的 request_id 返回 409；旧运行已结束时会先轮询确认并释放准入。
- 相同 UUID / 相同内容可恢复原请求。上游仍验证原始载荷，改内容不能冒用原请求号。
- POST 超时、收据损坏或 5xx 可能已经执行；返回 outcome_unknown，并保留待决意图。不得更换 UUID 静默重发。
- 明确拒绝的 4xx 会释放未开始的准入；完成、失败、取消或中断释放与该 run 对应的准入。
- 断开浏览器事件连接不等于停止模型。恢复使用事件序号；显式停止后等待真实 cancelled/interrupted，不能标为 completed。停止响应显式包含 cancellation_scope=conversation_only、business_tasks_cancelled=false；stopping 只是确认接收，继续等待终态。已有业务任务须独立查询/控制，不能从对话取消推断作业已取消。

门户将当前标签页的消息、thread、待决请求号和事件序号存入 sessionStorage。刷新时已知 run_id 只轮询/续接；未确认 POST 要由“恢复本次请求”显式触发，并复用原 UUID。完成正文替换增量草稿，避免重连后重复显示；输出通过 Vue 文本插值展示。

此机制覆盖当前标签页恢复。完整跨设备历史、会话删除/分页、长期保留策略，以及跨用户 profile/MCP 身份隔离仍需实现；旧 LangGraph 历史继续由旧入口访问。

## Nginx 与测试

统一入口示例为 `infra/nginx/hermes-platform.locations.conf.example`。保留 /api 原路径转发，关闭 SSE 缓冲/缓存，透传 Last-Event-ID，并提供 Vue history 回退。示例需要与目标 server 配置合并，尚未安装到现网。

可重复验证脚本：

```bash
# scat-agent 环境，真实业务只读查询
python -B scripts/probe_readonly_business.py
# 真实 DeepSeek + FastAPI + Hermes + MCP，多轮/重连/取消/降级
python -B scripts/probe_platform_api.py
# 独立 Nginx + 定时合成 SSE，仅验证传输，无 LLM 费用
python -B scripts/probe_nginx_sse.py
```

脚本拥有并关闭自己创建的进程；结果在项目 var/validation，临时目录在 .tmp。真实集成脚本会调用付费 DeepSeek API。测试脚本的 profile 可清理，实际业务 profile 与平台 var/platform/hermes.sqlite 应持久保留。

前端验证采用 npm run lint、npm run test、npm run build；`playwright.hermes.config.ts` 使用已安装 Edge，在桌面和手机视口测试交互。浏览器测试使用模拟接口，真实服务器链路由上述集成脚本验证。详细结果见 [本阶段记录](validation/20261022-H3-H4-business-api-portal.md)。


## H1/H2 收尾补测

H1/H2 当前只读范围已完成验收：20 题通过，五项边界通过，服务器完整回归 765 项通过。最终证据见 [验收记录](validation/20261022-H1-H2-acceptance.md)。新启动器显式限制常规 API 重试并关闭额外自动恢复；已有 home 配置不同时需用新的项目内 home，例如 var/hermes/business-h12。完整说明和复跑命令见 [H1/H2 收尾说明](HERMES_H1_H2_ACCEPTANCE.md)。本轮未切换 Nginx，未部署新常驻服务。


## H3/H4 结果收据与输出边界

HERMES_RESULT_STATE_FILE 默认 var/platform/hermes-results.sqlite，启动器和平台必须指向同一项目内文件。该文件与只保存绑定的 hermes.sqlite 分开，保存经过白名单工具返回的业务 payload、scope 摘要、run/call 标识和完整性摘要；不保存提示词、API Key、数据库凭据或任意模型生成的结果。仅实际工具完成回调可生成 readonly_tool_result，模型回答中写一个 ID 不会授予访问权。

默认单条 64 KiB、最多 1000 条、保留 24 小时；写入时回收已过期行，满额或写入失败时不发布成功引用，日志仅记错误类型，不能删除未过期结果来伪装存储成功。结果端点仍执行平台鉴权、thread/run 归属与过期/完整性校验；Hermes 离线时有效收据仍可读取。它不是 H5 模型作业 artifact，也不承诺长期保留；后续部署须将此状态文件与平台绑定状态一起持久化。

轨迹工具仍默认 20 点、最多 100 点。超过 64 KiB 时返回 representation=page_summary、scope=fetched_page、points_omitted 和原有分页信息，明确没有点明细，不将本页摘要冒称整条航迹；摘要仍超预算则 output_too_large。其他超大结果继续明确拒绝并要求分页。MCP 错误结果设置 isError=true，not_found 作为有效的无记录结果保留，不伪装正常数据。

新增真实验证（服务器 scat-agent Conda）：

```bash
python -B scripts/probe_h3_h4_completion.py --report h34-completion-rerun.json
```

报告同名时拒绝覆盖；脚本独占 18084/18085，使用项目内唯一 profile、结果库和绑定库，退出关闭自己创建的进程并清理唯一目录。验证真实 DeepSeek 查询、MCP 结果/SSE 引用、REST 数值对照、同 UUID 重放、跨会话拒绝、真实对话停止及 Hermes 离线后结果读取/确定性 API 可用性。未提交 GPU 作业；业务任务独立性另外由受控测试验证，H5 仍需完整作业链路验收。

## H5 模型任务

页面仅增加 H5 必需的参数核对、确认/撤销、任务刷新与完整 JSON 下载。确认由受认证客户端显式 POST，聊天确认文字不提交任务。任务独立于 SSE 和 Hermes 生命周期；unknown 会阻止新的 worker 领取，不自动重跑 GPU。完整 API、环境与恢复规则见 [H5 指南](HERMES_H5_MODEL_TASKS.md)。生产 SCAT_GBAR_V23_ENABLED 仍为 false；目录为空时助手须如实说明，不能声称已有模型可执行。


S5本机展示转发已专项验证一次强制断连：Vite保持监听，SSH有界重连后API恢复；不重发业务请求或自动触发模型计算。连接凭据只留本机进程内存，退出清除。运行证据见 `validation/20261023-S5-preview-reconnect.json`；跨内网持续稳定性仍受实际网络影响。
