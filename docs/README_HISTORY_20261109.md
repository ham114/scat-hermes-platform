# 首次 GitHub 同步前的 README 历史记录

归档日期：2026-11-09。下文保留原根目录 README 的阶段记录，相对文档链接已调整。当前状态以根目录 README 和最新上线审查为准。

2026-11-09 上线审查与修复：当前后端20261108-readiness-4eabaed6f3cd、身份门户已开放，GBAR已迁移标准CDI与固定Conda镜像；HYN/ZYJ独立、admin管理。普通自然语言分页/空后改查/复杂筛选、任意as_of真实实验/物化/报告、合成记忆与3用户GPU串行队列均有真实证据。最终1525后端通过/2跳过，169前端与真实桌面手机回读通过。服务器未重启。结论为具备3用户内网受控试运行基础，完整旧业务对等、独立RAG/模型科学质量、长期SLA与最终分发许可尚未全部准入；100002在现有训练CSV分区，功能验收不能当独立泛化成绩。最新审查 docs/RELEASE_READINESS_REVIEW_20261109.md，实施与页面指令 docs/HERMES_RELEASE_READINESS_20261108.md。以下为历史记录。

2026-11-08 业务展示补齐已上线：后端20261108-parity-f187d1d95013、门户20261108-portal-cb3e549d33ae，17种只读操作投影、输入质量六特征表、UAV轨迹优先展示。五条普通自然语言通过；达到继续准入阈值后第六条未提交。1505后端/2跳过、167前端、36普通浏览器、10真实HTTPS桌面手机回读通过；新增GPU0，原9份成功回执保持，服务器未重启。完整业务和七类真实交互仍未全部验收，剩余质量/运行时/身份项目保持独立。最新记录 docs/HERMES_BUSINESS_PARITY_ROUND_20261108.md，页面试用指令已列出。

2026-11-08 入口/管理/记忆收口完成：后端20261108-management-3f6ee1370128，正式门户 https://172.26.67.148:8444/ ，复用旧登录且新入口仅HYN，原站保留。普通自然语言→页面确认→原生GPU/评估/报告全链通过，新增GPU仅1次；新管理接口/挂载上线，Honcho容器DNS修复，2合成用户召回/更正/隔离/自动删除通过（3模型请求）。对话展示调整与刷新、17真实只读操作、正式HTTPS桌面/手机通过。1498后端/2跳过另3门户专项、166前端、36普通浏览器。服务器/GPU/三个原生Agent未重启；所有失败保留。完整记录 docs/HERMES_PLATFORM_CLOSEOUT_20261108.md；当前业务剩余以 docs/PROJECT_BUSINESS_PARITY_20261108.md 为准，旧“前端暂停/GPU关闭”是历史记录。H8全量/科学质量/标准GPU迁移仍未完成。

2026-11-08 模型功能完整闭环已完成：最终后端20261108-gbar-production-31196184dfd4，页面直接参数→服务端校验/可信确认→持久队列→真实CUDA→条件评估/报告→二维三维/刷新恢复通过，实际GPU仅1次、无维护恢复。修复GBAR默认350秒与字段反馈，初次自然语言规划失败保留，14组原提案复验通过，未追加自然语言收费重跑。1495后端/2跳过、162前端、36普通浏览器、4旧结果GET、新GPU1+新GET2通过。失败规划与新后台合计1921671保守单位，未提额。服务器/GBAR/作者未重启，七核心健康。详见 docs/HERMES_MODEL_FLOW_20261108.md。

2026-11-08 GBAR正式workflow已接通并上线（20261108-gbar-production-e87fd9c4237b）：五核心共享只读登记配置/持久GPU账本，主后台活动技能一致，81实现指纹一致。最后200点未来预测与历史回测各1次真实CUDA、各calls=1、各两制品；修复Mongo冷启动和全空真值占位误判，未来案例沿原参数/成功GPU回执恢复，不冒称初轮全链成功。最终1492后端通过/2跳过；23LLM、1293388保守单位，未提额或重算。七容器健康，服务器及既有GBAR未重启，标准GPU镜像迁移未做。详见 docs/HERMES_GBAR_PRODUCTION_20261108.md。

2026-11-07 精确观测窗口与未来预测契约已修复上线（20261107-forecast-contract-2efaab19633e）：支持最后200点、从冻结末点预测未来10/30/60秒；历史回测独立，无真值不生成误差指标。1486后端/2跳过、105候选镜像、153前端单元、38普通浏览器及最终2专项、2真实GET通过。自然语言验收修复守卫映射及Mongo时间/多页发布问题；页面确认一次，原任务恢复发布，真实CPU只执行1次、GPU0。保留首次回退与全部失败记录。服务器未重启、七容器健康。详见 docs/HERMES_FORECAST_CONTRACT_20261107.md。

2026-11-07 四项查询/展图缺口已修复并切换生产，活动版本20261107-query-display-96d80a6a76af：技能恢复与操作元数据、回执核验分析范围、明确绘图自动展图、全程有界分页与刷新缓存。151前端单元、42普通浏览器及最终2专项、服务器/最终镜像各131专项、5唯一真实自然语言请求、最终6桌面手机GET回读通过；真实524/393点完整展图，模型已分析范围单独标明。仅重建五核心，服务器未重启，七容器健康，预测任务增量0。另记录进近被描述为落地的科学解释措辞限制，不将四项通过冒称全平台完成。详见 docs/HERMES_QUERY_DISPLAY_COMPLETION_20261107.md。

2026-11-07 第2、3项五轮真实复验完成，结论部分通过：查询/模型workflow目录/两轮真实绘图可用，桌面手机刷新回读6项通过，候选源码95专项通过。发现现网缺补加载recovery与allowed_operations，一轮分析将224已读点误称524全量，绘图尚需手动展开来源。新增5唯一请求、21LLM/22工具、3008662保守单位，预测0；没有切换生产或重启服务，不将未修复缺口标为通过。详见 docs/HERMES_QUERY_DISPLAY_ROUNDS_20261107.md。

2026-11-07 绘图分析多结果展示修复：每轮仅一个主展示区，兼容轨迹分页按源序号合并去重，原始来源折叠并标明范围/采样；失败或停止不冒称完整分析。148前端单元、40普通浏览器、6真实GET回读通过：三份重叠抽样合为108/524点，六页合为524点，已停止100200显示100/393点。新增模型/预测0，无服务器或容器重启。工作台5175刷新生效，记录 docs/HERMES_RESULT_SUMMARY_20261107.md。

2026-11-07 新指令无回执故障确认：用户两次失败运行及一次独立会话复核均为模型供应商HTTP500，旧成功100点回执仍可在桌面/手机读取。已修正页面按执行失败/停止/无独立回执分别提示；143前端单元、40普通浏览器及2真实只读回读通过。独立新绘图复核仍失败，供应商故障未恢复，不将UI通过当整体恢复；没有重启服务器/容器或扩预算。详见 docs/HERMES_RECEIPT_FAILURE_20261107.md。

2026-11-07 轨迹地图修复完成：地理二维默认OpenStreetMap街道底图；缺有效时间的轨迹保留静态几何，三维按轨迹包围范围立即定位，新增定位按钮；二维/三维支持全屏及退出，手机尺寸同步适配。前端142单元、40普通桌面手机浏览器、2真实只读回读及2合成坐标在线底图核对通过。真实100点可见，新增模型/预测/API写请求0；服务器及容器未重启。当前5175工作台刷新即可使用，源码与构建归档同步见 docs/HERMES_TRAJECTORY_MAP_VIEW_20261107.md。

2026-11-07 经用户明确授权已完成二维绘图最小修复生产切换，活动版本20261107-track-display-3ee814b9c60b。仅重建五个项目核心容器，服务器启动标识未变，七容器健康，10份服务器私有备份检查通过，学习调度恢复。原失败对话六页轨迹在桌面/手机均可回读图表，原failed状态保留。唯一一次普通绘图请求成功，一页100/524点、370字回复；主Agent 3次LLM/2工具、225378运行预留单位，无预测任务。新结果手机GET回读通过，本轮4个真实浏览器验收通过；浏览器无认证头、无mock，无预算扩大或GPU启用。前次自动审批拒绝已由用户明确授权解决，历史证据保留。详见 docs/HERMES_TRACK_DRAWING_FAILURE_20261107.md。

2026-11-07 新会话二维绘图失败定位：航班100002摘要与六页524点均已读取成功，失败终态来自供应商输出长度上限；正式API仍缺上轮未部署的图表投影。已完成最小修复镜像（20261107-track-display-3ee814b9c60b），服务器及镜像内各59专项通过，候选API只读核对六页真实回执全部可绘图，原摘要及跨用户保护保持。本轮新增LLM/预测0。自动审批拒绝重启五核心并切active release，要求明确授权短暂生产中断；该切换未执行，修复候选与回滚方案已准备。详情 docs/HERMES_TRACK_DRAWING_FAILURE_20261107.md。

2026-11-07 查询与轨迹展示修复（计划第2、3项）：已实现本轮技能补加载指引、按实际workflow目录引导模型任务，以及受归属/摘要校验的只读轨迹回执→AnalysisResult→页面图表。最终冻结源码服务器完整1440 passed/2 skipped，前端139单元、38普通桌面手机通过；真实native分发与航班100002的100点只读图表核对通过，分页边界保留。候选s5-query-display.1.0尚未切入正式catalog/镜像；自然语言实测与失败、预算、同步清理详见 docs/HERMES_QUERY_DISPLAY_REPAIR_20261107.md。GPU、精确末尾窗口和管理接口发布不包含本轮。

# SCAT Hermes Platform

2026-10-27：H6后端收尾及H7-B CPU部署完成，服务器运行五核心+两可选作者Docker容器；完整回归1302 passed/1 skipped，真实主Agent方案→可信确认→后台技能工作流及容器重建恢复通过。RAG沿用18133已有Conda服务，GBAR继续复用既有BentoML容器，标准GPU迁移与正式网关切换单列。当前部署配置、端口、启停/版本回滚及证据见 [H7-B容器部署](HERMES_H7B_CONTAINER_DEPLOYMENT.md)。前端仍暂停。

标准角色现使用项目内原生命名 profiles：scat-main、scat-worker、scat-author；统一 `scripts/profile_manage.py` 管理，独立作者Agent可创建/校验技能草稿。初始化、启动、Key放置与验收见 [标准 Profiles](HERMES_STANDARD_PROFILES.md)。现有DeepSeek Key可复用，无需额外申请。

2026-10-25按新指示优先H6，恢复前端并完成七类基础结果渲染/任务artifact预览：前端118项、桌面/手机4项、服务器契约14 passed/1 skipped。地图/播放对等、多轮展示和新RAG准入继续开发，H7-B后置。替代检索及多Agent方案、合成截图见[H6开发记录](HERMES_H6_DISPLAY_AND_RAG.md)。

H7-A已完成无凭据Conda/pip输入核验、候选后端统一启停与健康检查、日志轮转/磁盘门槛及真实重启恢复。完整后端 **1147 passed/1 skipped**，15项真实服务边界检查通过；测试服务已停止。运行命令、证据及后续Compose/实际制品重建范围见 [H7-A后端运行准备](HERMES_H7A_BACKEND_OPERATIONS.md)。

> 开发前先读 [开发纪律和服务地址](../SCAT_HERMES_PLATFORM_DEVELOPMENT_AND_TEST_PLAN.md#development-discipline)。服务器项目为 `/home/h3c/project/python/scat-hermes-platform`，优先在服务器开发测试；Python 仅使用已有 Anaconda 环境，项目中禁止 venv 与 uv。临时文件仅在两端各自项目目录创建并及时清理。

面向 SCAT/UAV 航空数据的智能分析平台，项目目录名为 `scat-hermes-platform`。

S7-A 后端新增独立技能创作 API：版本化草稿、静态校验、固定候选合成试跑、差异预览、独立凭据确认发布及回滚；主对话和后台任务固定各自的技能包。服务按需启动，前端继续暂停，未向现网发布候选。配置与验收见 [S7-A 技能创作](HERMES_S7A_AUTHORING.md)。

本项目从原平台选择性迁入业务源码，作为 Hermes Agent + MCP + FastAPI 的独立开发基线。旧 LangGraph 对话、内部模型任务图与 Streamlit 仅保留为显式对照/历史兼容；新平台使用 Hermes Skills 主链；已新增固定版本 Hermes 独立服务、HTTP/SSE 客户端及五类只读 MCP 工具。已通过五工具真实业务及 FastAPI/DeepSeek/MCP 链路验证，门户 `/assistant` 支持流式查询、停止与恢复。H5 已接入模型任务准备、人工确认、持久队列、独立 worker 和受归属控制的结果恢复；Hermes 可查询与准备任务，执行由用户点击确认。服务器生产 GPU 模型开关仍关闭，完整闭环已用受控 CPU 模型验收。动态展示、Compose 与正式入口切换按后续计划推进。详见 [API 与门户指南](HERMES_API_AND_PORTAL.md)。

2026-10-23 开发方向调整：目标改为 **Hermes 原生 Skills 主导全部核心业务执行流程**，共享 Python 业务模块继续作为技能执行资源，旧 LangGraph 主/内部流程在 S1–S6 中退出新请求主链。历史 S1 时点保留 H5 默认入口（现已按 S6 切换）；S1 可选配置已完成受控原生技能读取、首批技能和真实 DeepSeek 合成链路验收，全部业务迁移继续按 S2–S6。详见 [新版架构与复用评估](HERMES_SKILLS_ARCHITECTURE_AND_REUSE_20261023.md)。

## 当前保留的能力

- SCAT/UAV 查询、统计、质量检查、轨迹预处理和业务数据访问。
- 模型注册、推理后端、评估协议、任务确认、运行台账与结果恢复。
- FastAPI API、认证、用户归属、流式协议与客户端。
- Vue/TypeScript/Cesium/ECharts 门户，以及 Streamlit 过渡工作台。
- RAG 检索代码、模型 API 检查工具，以及对应的可移植业务测试。

## 目录

```text
src/scat/、src/uav/          业务服务、数据契约与 Repository
src/scat_model_runtime/     模型运行协议、执行与评估
src/analysis_runs/          任务与结果持久化
src/agents/                保留的 SCAT 工具和过渡编排
src/application/           只读业务、可信上下文、模型任务确认与队列
src/hermes_runtime/        Hermes HTTP/SSE 客户端及运行归属
src/scat_mcp/              只读 MCP Server（默认 5 / Skills 26 项）
services/hermes/           上游版本、环境安装与服务说明
environments/              Conda 环境定义
src/service/               FastAPI、认证、结果与流式接口
src/rag_retrieval/          检索实现
frontend/                  现有门户与单元测试
scripts/                   接口导出、检索服务及本地 LLM 检查
infra/compose/             已验收CPU Compose与GPU迁移边界
```

## 默认模型与统一入口

第一阶段使用外部 DeepSeek，默认模型 `deepseek-flash`。根 `.env` 已预留 `DEEPSEEK_API_KEY=`，自行填写；`.env.example` 为无密钥模板。本地 llama-server 暂不作为开发前置条件。Nginx 统一接收客户端请求，内部 Hermes / MCP 不直接暴露。

[服务与容器部署结构](DEPLOYMENT_TOPOLOGY.md) 说明目标容器划分及初期复用方式。

## 开发与测试入口

先阅读 [开发与测试计划](../SCAT_HERMES_PLATFORM_DEVELOPMENT_AND_TEST_PLAN.md)。本机和服务器的 Python 开发、测试、运行统一使用 **Anaconda 环境**，不在项目目录创建 `.venv` / `venv`。Node.js 使用 24，npm 使用 11，依赖按 package-lock 安装。

服务器 `/home/h3c/project/python/scat-hermes-platform` 可先复用已有 `scat-agent`（Python 3.12.13）。本机目前已有 `scat-frontend`（Python 3.12.14），尚无 `scat-agent`；需要本机完整后端时先按计划准备相应 Conda 环境。服务器 Hermes 已安装到独立 `scat-hermes`（Python 3.14），与平台环境隔离。所有临时文件、源码和日志位于新项目内；该 Conda 前缀是用户明确批准的唯一目录外例外。

以下是**已准备好 scat-agent Conda 环境之后**的 PowerShell 示例，先确认解释器，不会自动建立环境：

```powershell
conda activate scat-agent
python -c "import sys; print(sys.executable); print(sys.prefix)"
python -m pip check
npm --prefix frontend ci
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

填写自己的数据库、身份和模型配置。开发验证使用未占用端口，例如 18080；先确认端口状态，并让前端 API 地址指向本轮测试后端，配置键见 [配置说明](CONFIGURATION.md)。

```powershell
python -m uvicorn service.scat_only:app --app-dir src --host 127.0.0.1 --port 18080
npm --prefix frontend run dev -- --host 127.0.0.1
```

完成语言模型配置后，过渡对话服务入口为 `python src/run_service.py`，启动前设置独立的 HOST/PORT。Streamlit 可选入口为 `python -m streamlit run src/streamlit_app.py`。每轮结束只清理本轮启动的测试服务。

## Hermes 助手

服务器启动与前端代理命令见 [运行指南](HERMES_API_AND_PORTAL.md)，本阶段结果见 [验证记录](validation/20261022-H3-H4-business-api-portal.md)。当前采用单用户配置，页面入口为 `/assistant`。

## 后续集成

采用 C 方案：平台 FastAPI 通过 HTTP/SSE 调用独立 Hermes；Hermes 通过受控 MCP 工具使用业务服务；模型调用先使用外部 DeepSeek，后续可切换独立 llama-server。按主计划 v2.6 先完成 S1–S6 的原生技能、步骤提取、后台执行与全业务覆盖，再继续 H6 展示和 H7 部署。

- [开发与测试计划](../SCAT_HERMES_PLATFORM_DEVELOPMENT_AND_TEST_PLAN.md)
- [H1/H2 验收、固定评测与复跑说明](HERMES_H1_H2_ACCEPTANCE.md)
- [H3/H4 结果引用、输出边界与最终验收](validation/20261022-H3-H4-completion.md)
- [迁移范围与边界](PROJECT_BOOTSTRAP.md)
- [Hermes 方案与开源对比](HERMES_INTEGRATION_COMPARISON_20261021.md)
- [Compose 部署建议](HERMES_COMPOSE_DEPLOYMENT_NOTES_20261022.md)
- [本地模型推理记录](LOCAL_LLM_INFERENCE_20261022.md)
- [配置说明](CONFIGURATION.md)

源码不携带实际凭据、业务数据或权重。私有配置仅保存在各环境根 .env；服务器本轮在本机复用必要数据库认证，凭据未传回本机。既有权重继续通过部署配置引用。

## 来源与许可

业务源码迁自既有 SCAT 航空分析平台；其基础为 [Agent Service Toolkit](https://github.com/JoshuaC215/agent-service-toolkit)。保留原 [MIT LICENSE](../LICENSE)，更多说明见 [NOTICE](../NOTICE.md)。

H5 运行入口、任务 API 和 unknown 故障处理见 [模型任务指南](HERMES_H5_MODEL_TASKS.md)，本轮证据见 [H5 验收记录](validation/20261022-H5-acceptance.md)。


### 原生技能与自主创作规划（2026-10-23）

[S1 原生技能接入与验收](HERMES_S1_NATIVE_SKILLS.md)记录首批技能、运行方式和实际测试状态；[技能生命周期与工具映射](HERMES_SKILLS_LIFECYCLE_AND_TOOL_MAPPING.md)说明业务拆分及后续 Agent 自主创建/修改的草稿、测试、发布、版本与回滚流程。自主创作尚未开放；全业务迁移仍按 S2–S6 推进。

v2.2 的 [七项优化详细计划](../SCAT_HERMES_PLATFORM_DEVELOPMENT_AND_TEST_PLAN.md#skills-detailed-plan) 明确运行级操作绑定、技能组合、粗粒度模型操作、可恢复 CPU 闭环、任务评测和分阶段创作，并规划由用户配置容量、归档、会话、缓存与调用预算。完整 [创建/调用流程图](HERMES_SKILLS_LIFECYCLE_AND_TOOL_MAPPING.md#skills-workflows) 为后续目标，配置项和作者服务尚未实现。

v2.3/v2.4 的 [首步](HERMES_S2_SKILL_OPERATION_BINDING.md)和[第二步](HERMES_S2_READONLY_COMPOSITION.md)保留历史范围。[S2 已收口](HERMES_S2_COMPLETION.md)：六个文档技能/26 项只读操作、实际配置额度、调查参数复用、只读引用、11 类离线模板和分域题集；历史服务器 947 passed/1 skipped、本机相邻 100 passed。

历史 [S3 验收](HERMES_S3_MODEL_STEPS.md)完成五个中性模型步骤、版本化制品与三类旧数值路径对照。历史 v2.7 的 [S4 后台任务](HERMES_S4_BACKGROUND_JOBS.md)已验收批准快照、独立 Hermes 子会话、租约代次、持久 CPU 执行号、共享累计预算及明确修订。服务器 1005 passed/1 skipped、本机新增 21 passed、七类真实进程故障通过；真实 Hermes/DeepSeek 在关闭聊天服务并杀死 worker 后恢复回读标准结果，205.51 秒且 CPU 只执行一次。下一步 S5，DNS 波动和全部失败记录详见开发日志。Skills profile 七技能/38 工具，后台七工具；默认 H5 入口保留，S4 显式启用。H6 展示主子任务和复杂配置，生产 GPU 尚未验收。

S3 原 [pending 同步清单](validation/20261023-S3-sync.json)保留，后续已补齐同步、契约复验和服务器清理，见 [解决记录](validation/20261023-S3-sync-resolution.json)。S4 实现、配置、复跑、失败与最终证据见开发日志和专项文档；默认入口切换继续按 S6。


历史 v2.8 已完成 [S5业务工作流](HERMES_S5_WORKFLOWS.md)：五个新技能、12份发布文档和52旧工具用途映射，固定样本CPU对比、有限配置/重复实验、五类物化、评估与可回查报告。最终1042 passed/1 skipped，完整回归、格式与既有Conda依赖检查见 [驱动摘要](validation/20261023-S5-164ed36302-driver.json)。原生合成委托链路和服务器内真实数据路径已验；生产GPU仍关闭，真实数据验证材料只留服务器。

可在本机 [分析助手](http://127.0.0.1:5173/assistant) 查看限时开发展示：展开工具组目录，填入合成双模型示例，发送并核对后确认；后台独立执行，页面展示步骤、累计预算、部分成功和结果下载。前端97项及桌面/手机12项通过，实际健康检查和展示见 [API与门户指南](HERMES_API_AND_PORTAL.md)。完整配置编辑器、技能关系图与通用报告图形仍按H6，下一阶段为S6。

当前 v2.9 已完成 [S6 后端验收](HERMES_S6_BACKEND_ACCEPTANCE.md)：1063 passed/1 skipped，真实 Hermes/DeepSeek 合成留出21/21，十一业务域各3/3；事务归档、在途任务引用保护、缓存/会话容量、维护CLI、新默认与显式H5切换均有证据。组合域共享实际运行，样本不足以声明SLA。原始失败保留，候选仅作独立计划对照、未发布。

新平台 Skills/工作流开关默认 true；native 升级使用独立 `--home var/hermes/skills-s6`，不覆盖旧配置；历史 H5 使用 `--legacy-h5`。发布包、实现和回滚规范见 [冻结基线](../services/hermes/releases/s6-backend-v1.json)。本轮只推进后端，前端源码未改动，H6暂停；先前限时展示不代表当前常驻服务。生产GPU、S7正式创作及H7部署继续独立验收。
