2026-11-08最新收口：自然语言GBAR全链、新管理接口与真实记忆复验已通过，正式独立HTTPS8444已上线（新入口仅HYN、旧站保留）。当前状态与剩余以 HERMES_PLATFORM_CLOSEOUT_20261108.md、PROJECT_BUSINESS_PARITY_20261108.md 为准；下方审计是历史停点。

后续进展（2026-11-08）：查询/展示与精确窗口修复已陆续发布；GBAR正式接入也已上线，production_gpu=true，真实两任务/两POST及持久结果回读通过。当前记录见 HERMES_GBAR_PRODUCTION_20261108.md；下文保持11月7日审计原貌，不将当时关闭GPU的结论当作当前状态。

# 新对话交接：实际业务可用性重新审计

更新日期：2026-11-07，Asia/Shanghai。

## 开发纪律与服务地址

- 本机项目：`C:\python\scat-hermes-platform`；旧项目：`C:\python\agent-service-toolkit-main`，只读作功能对照。
- 服务器：`h3c@172.26.67.148`；远程项目：`/home/h3c/project/python/scat-hermes-platform`。SSH 密码由用户在原对话提供，不在此文或脚本中复制。
- Python 仅使用已有 Anaconda 环境。禁止项目内 venv、uv、新环境、安装新依赖。本机 base Python `C:\ProgramData\anaconda3\python.exe` 用于 SSH/预览；`C:\ProgramData\anaconda3\envs\scat-frontend\python.exe` 用于已有检查工具。服务器业务 `/home/h3c/anaconda3/envs/scat-agent/bin/python`，原生 Agent 用既有 `scat-hermes`。
- 前端沿用已有 Node 24/npm 11 和 node_modules，不增加依赖。
- 本轮审计临时目录仅两端项目 `.tmp/project-availability`；正式证据 `var/validation/20261107-project-availability`。不得删除其他阶段的运行目录、账本或真实任务。
- 本机可写页面 `http://127.0.0.1:5175/`；Python 业务代理 18152、作者代理 18153。
- 服务器回环：18120 平台、18121 主 Hermes、18124 作者 API、18126 后台 Hermes、18128 后台服务、18133 RAG。GBAR 复用原 BentoML 私网 3000，不改驱动、模型权重或旧容器。
- 本轮只读审计：不调用 LLM、不提交预测、不重新计算、不重启服务、不读取私人记忆正文。认证留进程内存，不写前端、URL、文档或日志。网络不稳定时适当检查 ping/SSH。

## 当前用户要求与停点

用户使用本机工作台做了四轮真实对话后反馈：旧项目的大多数功能未实现或未上线，要求查看新对话并重新检查项目。随后因对话容量上限要求准备本交接文档。

当前进行的是**实际业务可用性审计**，尚未完成所有功能逐项验收，也尚未实施本轮修复。下一轮应先读此文和审计证据，接着完成缺口清单与整改规划，不能直接沿用“后端完成/全部迁移”的旧阶段结论。

## 核心结论：必须纠正完成口径

先前真实可写验收证明了一条 CPU 比较任务闭环以及确认、撤销、取消和刷新恢复；不证明旧项目全部业务已完整迁移、上线并可从自然语言触发。

当前不能认定平台已完成旧项目功能对等交付。至少存在：查询技能加载恢复缺陷、查询结果到专业图表的接线缺失、GPU 正式运行配置未接通、旧模型与新 workflow 入口并存且引导冲突、精确输入窗口语义缺失，以及新管理接口未全部发布。服务 healthy、源码存在、映射表列全和模拟测试通过均不能单独作为用户可用的证明。

## 已读取的最新真实会话

线程：`740c8a48-8353-4654-bdd3-1eee0cdb720f`。

通过已运行的本机 SSH 代理只读读取：

`GET /api/v1/hermes/threads/740c8a48-8353-4654-bdd3-1eee0cdb720f/messages?limit=100`

四轮问题与关键结果：

| 用户请求 | run_id | 实际表现 |
| --- | --- | --- |
| 你目前可以检索哪些数据库 | `run_df70ec550dfa4154990ca20f2171d535` | 最初未加载技能就并行查询，收到 skill_required；UAV 查询又遇到 skill_operation_not_allowed，随后补加载后成功。 |
| 查看 SCAT 航班 100002，总结运动趋势 | `run_12e4c9ea2ff04300800a52321e6bef4c` | 只加载 scat-trajectory-analysis；轨迹、阶段、飞行计划成功，但摘要、搜索被拒绝，未补加载 scat-flight-query；最后用轨迹元数据和抽样点回答。 |
| 绘制它的二维轨迹图 | `run_fc1e30e785ed423c8d34593b9e9e4beb` | Agent 猜测 scripts/plot_track.py，实际 skill_file_not_published、script_not_published，然后声称不能绘图。 |
| 取轨迹最后 200 点，用 GBAR 预测 | `run_d719e5b251b54df3b3b8986cd1f77e23` | 先走 scat-model-analysis/scat_task_catalog，目录空；后查 workflow 仅两 CPU 模型、production_gpu=false，无法准备 GBAR；精确末尾点数切片也未支持。 |

没有将完整用户会话、原始轨迹或私人记忆复制成新的本机文件。审计只保存调用名称、参数键、错误码、技能版本与部署摘要。

## 已验证的缺口与原因

### 1. 正常业务查询的技能加载恢复不完整

正式技能权限守卫按当前 run 的加载回执判定权限；上一轮加载不自动成为下一轮授权，这是当前实现的边界。第二轮只加载轨迹技能，随后调用查询技能所属的摘要/搜索，因此被拒绝。守卫没有证明数据库缺失；Agent 应显式补加载适用技能再查询。

证据：`src/hermes_runtime/skill_guard.py`、审计 `traces.skill-loads.sqlite` 与 `conversation_tool_trace`。不要通过全开放权限或跨 run 复用旧加载回执解决。

### 2. 二维绘图主要缺在结构化结果链路，不能归因于必须写脚本

- `src/application/readonly_extensions.py` 直接绑定 `flights.get_track_points` 等服务，返回原始业务数据；没有沿旧工具包装器挂接对应 AnalysisResult。
- 旧项目及当前保留的 `src/agents/scat_tools.py` 会调用 `attach_analysis_result(result, build_scat_track_analysis(result))`；当前旧 builders 仍在 `src/agents/analysis_results.py`。
- `frontend/src/components/RunEvidencePanel.vue` 读取只读回执后只用 pre 展示 JSON。
- `AnalysisResultRenderer.vue`、`AnalysisTrajectoryWorkspace.vue`、ECharts/Cesium 的 2D/3D/播放/曲线组件已经存在；但不意味着这次自然语言轨迹查询已产出可展示制品。
- 轨迹技能正文没有绘图/标准结果输出步骤；Agent 尝试未发布脚本属错误引导。

建议补可信业务数据 → 确定性 AnalysisResult builder → 受归属控制结果引用 → 页面图表 → 对话展示调整的完整链。脚本不是画普通轨迹图的必要前置，不能为补二维图引入任意代码执行。

### 3. GBAR 后端能力存在，但正式运行没有加载登记配置

- 实际主平台、actions、worker 等配置仍有 `SCAT_GBAR_V23_ENABLED=false`。
- 更直接的依据：`SCAT_GPU_CONFIG_FILE` 未在所审计正式角色中配置，默认 `config/production-gpu.json` 不存在。
- 服务器 `var/gpu/production.json` 确实存在且 enabled=true，固定 release 为 `scat-gbar-v23:20261010-485de7947678-deps7d416e4aac0f-sec3`；宿主有文件不等于容器挂载并使用。
- `GET /api/v1/hermes/workflow-catalog` 实测 enabled=true、synthetic=false，model_ids 仅 constant-velocity-heading、kalman-cv，production_gpu=false、gpu_registration=null。
- GPU 注册函数在 `src/workflow_runtime/gpu.py:registration`，真实开关以其配置读取结果为准，不能只改一个旧 GBAR 布尔变量。
- 当前镜像中的 workflow catalog/contracts 源码与最新服务器源码 SHA 相同，已有 GPU schema 与接入逻辑；不是所有 GPU 代码都缺在镜像里。
- 已发布活动技能包仍 `s5.0.0`，比较/实验技能仍 0.1.0；本机最新 GPU 比较技能是 0.2.0，不能把磁盘技能文档等同活动版本。
- 10 月 27 日 GPU 的四次真实 CUDA 调用与回执是历史独立后端验收，文档明确正式 CPU 入口没有启用；不要重跑成功预测作健康检查。

本轮尚未重新确认既有 GPU 服务当前 metadata/health：审计脚本最初按 transport.base_url 读取，但真实字段为 transport.endpoint，因此这一支未发出 GET。若后续需要核对，应修正字段并只做 GET `/metadata`；不得以本轮结果宣称当前 GPU 推理已验证成功。

### 4. 模型入口和系统提示仍有旧阶段冲突

- `GET /api/v1/hermes/model-catalog` 实测 items=[]，workflow-catalog 却有两 CPU 模型。空旧目录不能解释为平台完全无模型。
- 新请求被 scat-model-analysis 引到 scat_task_catalog；本轮 GBAR 请求真实走了这个旧路线。
- `scripts/run_hermes_api.py` 当前源代码约 116–126 行仍硬编码“模型任务使用 scat-model-analysis”和“仅注册 CPU，生产 GPU 未启用”。
- 后续应以运行时能力目录引导新单模型/对比任务，保留历史兼容读取，并核对活动技能包、system hints、工具集和实际目录一致。

### 5. “最后 200 点”与从末点向未来预测的业务语义未完整迁移

- `WorkflowProposal` 目前主要是批次、航班、采样种子、时距、stride、min_observed_s、最大截止点数等；没有精确 observation_last_n / forecast_origin / as_of 等用户窗口契约。
- `cutoff_times` 明确排除轨迹最后一点，当前比较流程面向历史截止点评估，不能直接等同“用最后 200 点，从最后观测点向未来预测”。
- 原项目 `src/agents/prediction_tools.py:predict_scat_flight_trajectory` 有 as_of，缺省为末点盲预测、compare_actual 可选；当前映射把它归到比较 workflow，不代表语义等价。
- 需要区分纯预测与有真值的回测；点数不是秒数。没有未来真值时可以返回预测，不能编造误差。GBAR 自身预处理长度/时间窗要求仍必须明确校验。

### 6. 管理页面早于相应正式接口版本

- 正式平台旧 memory 路由存在，但缺本轮新 GET `/api/v1/hermes/memory/corrections` 等管理增强。
- 正式作者 API 通过运行镜像 AST 核对：已有草稿、diff、validate、trial、release preview/confirm 等旧端点；缺新 GET `/v1/skills/catalog`，也缺新的 `/v1/settings*` 管理路由。
- memory_api、skill_authoring_api、agent_settings 的磁盘源码与实际镜像 SHA 不同；详情见 runtime-audit.json。
- 作者 API `/openapi.json` 未暴露路由，不能据此说整个作者服务不存在；已用实际运行源码的路由装饰器补核对。
- Honcho 最近一次真实效果测试被 DNS/出网阻塞，详见 11 月 7 日 memory-settings 验收；主/后台容器的 DNS 修复不证明 Honcho/宿主代理出口也已恢复。
- S7 草稿/发布基础已经有部分正式能力；不能把技能管理整个模块说成未实现，也不能把新目录/设置页称为已经全量可用。

## 原项目 52 工具映射核对

已对旧项目 `src/agents/*tools.py` 做 AST 静态盘点，并与 `services/hermes/legacy-tool-mapping.v1.json` 比较：

- 实际 52 项、映射 52 项，无漏名或多名。
- native_readonly 25，scoped_merge 15，http_control 7，native_control 4，retained_http 1。
- 新只读目录另有预测快照发现等操作，不能机械用 25/52 算完成率。
- “列全映射”只证明职责有安排，不证明 scoped_merge 等价保留输入范围、即时预测、图表输出和批次查询语义，更不证明已在正式入口自然语言验收。
- 各域真实页面可用性尚需逐项列证据：SCAT 查询/质量/轨迹、UAV、知识检索、即时预测、GBAR、比较/实验、物化、CFPL/M6 评估、报告、技能管理、记忆/Agent 设置。

不要用测试总数或映射覆盖率计算未经验证的产品完成百分比。

## 当前实际部署及先前有效成果

- 活动核心 release：`20261028-s7b-3ad5eac48b08`。日期前缀沿用旧构建器，不据前缀判断源码的新旧。
- 七个核心/作者容器在本轮审计时均 healthy。健康不代表功能全开。
- 上轮按用户明确授权“允许修复并继续真实联调”，仅为 hermes-main/hermes-worker 重建 DNS=223.5.5.5/119.29.29.29；原镜像/状态目录/预算不变，学习调度恢复。授权不应扩张为本轮任意生产发布。
- 真实 CPU 成功线程 `a22b1c42-eccd-41b6-8e04-486d27f05404`，任务 `7362bfa40e5f4b509b67dfadac8d2a51`；一航班、两 CPU、10/30 秒、一个截止点，三计算单元各 calls=1，真实指标+五表。
- 撤销任务 `7c57e5e114c54ebdb586398d3f2a4782`、取消任务 `641c64bc608d45cf91bc828cfd830a83`，实际计算均 0；页面刷新不重提。
- 上轮 137 前端单元、36 普通浏览器、6 代理专项及三个真实/GET 回验通过。不是本轮重新跑的测试，不拿来证明此次报告的新缺口已经修好。
- 前端 H6-B 的既有 2D/3D 组件与合成契约测试有效，但不能替代真实轨迹查询→图表产出这一接线验收。

## 新对话建议顺序与验收方式

1. 完成并向用户报告统一缺口表：原功能、当前源码、正式镜像/配置、活动技能、自然语言触发、实际制品/页面、证据、未完成项。把“代码完成”“配置未开”“接口未发布”“接线缺失”“尚未对等实现”分开。
2. 优先补本轮直接失败的用户路径：正常技能依赖恢复、查询结果结构化展示、统一模型任务路由/提示、精确窗口与预测/回测契约。不要仅改回复文案或取消权限校验。
3. 准备并核对 GPU 配置、挂载、登记 provenance、活动技能、双角色执行环境、制品集合/索引及持久回执，再按适用授权处理正式切换；不是简单把 false 改 true。
4. 发布已完成但未加载的新管理接口，核对作者/发布凭据独立；真实 Honcho 效果单独复验。
5. 用用户原四问作普通自然语言验收，测试不得提示“先加载某技能”、不得指定内部工具清单替模型完成路由。新增窗口/预测要有可信确认和实际图表；检查真实返回、账本与制品，不靠成功文字。
6. 补旧项目代表场景对等矩阵：所有真实失败保留；已有成功结果只读复查，不为截图/页面反复提交模型/GPU；真实调用需有限预算。

以上是接续建议，不代表本轮已实施这些修复。当前用户最后指令仅要求交接文档，下一轮结合用户新指令继续。

## 文件与证据入口

- 本文：`docs/PROJECT_AVAILABILITY_HANDOFF_20261107.md`。
- 本轮可复用只读审计脚本：`scripts/audit_project_availability.py`，已在服务器既有 scat-agent 执行两次成功；未做最终 Ruff 检查，且 GPU health 字段待修正。脚本只打印少量元数据，不输出 token。
- 本轮正式运行证据：`var/validation/20261107-project-availability/runtime-audit.json`。包括目录完整返回、正式源码 SHA 对照、技能加载/操作回执、四轮工具调用错误码、活动技能包清单与作者路由。
- 旧工具盘点：`var/validation/20261107-project-availability/legacy-mapping-audit.json`。
- 主开发计划：`SCAT_HERMES_PLATFORM_DEVELOPMENT_AND_TEST_PLAN.md`；开发日志 `docs/DEVELOPMENT_LOG.md`。本轮纪律已置顶，审计结论以下方交接状态为准；旧历史完成记录未抹去。
- 上轮可写验收：`docs/HERMES_WRITABLE_WORKSPACE.md`、`docs/validation/20261107-writable-live-acceptance.json` 及 sync/cleanup JSON。
- GPU 历史验收：`docs/HERMES_PRODUCTION_GPU_TASKS.md`、`docs/validation/20261027-production-gpu-acceptance.json`。
- 新管理接口状态：`docs/HERMES_H6B_AND_SKILL_MANAGEMENT_UI.md`、`docs/HERMES_MEMORY_AGENT_SETTINGS_UI.md`、`docs/validation/20261107-memory-settings-ui-acceptance.json`。

## 启动、凭据与本轮收尾

- 用户双击根目录 `start-workspace.cmd`，已有预览则复用；否则交互输入 SSH 密码，无需复制模型 Key。`stop-workspace.cmd` 正常关闭本机三端口并清理该预览的独有暂存。
- 上轮预览记录 `var/preview/writable/services-5175.json`，控制器 PID 16872、Vite PID 11140，仅是上轮记录，下一轮需重新核对，不能假定仍存活。默认四小时，可能已到期；正常到期自行清理预览目录。
- 远程读取认证可以沿已授权代理，或 SSH 在进程内读取运行平台认证；不把生产 Key 保存本机文件，不下载生产 DB 或备份。
- 本轮本机没有新增任务临时目录；正式证据保留。服务器同步桥产生的 `.tmp/project-availability` 是空传输暂存，收尾应核对后删除。同步/清理最终结果见相邻 `handoff-sync.json`；若该文件缺失，应先检查收尾是否完成，不假定已同步。
- 不读取或修改其他项目、原有任务和历史 DB；不要关闭用户正在使用的预览来清理目录。
