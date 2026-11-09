后续验收已完成：一次新的普通自然语言GBAR请求经页面确认和原生后台自动全链通过（无维护恢复），见 HERMES_PLATFORM_CLOSEOUT_20261108.md。本文件保留此前自然语言失败及直接表单验收原貌。

# 模型业务完整闭环开发与验收

2026-11-08，Asia/Shanghai。按功能完整性完成，沿用已有 workflow、持久任务队列、原生后台 Agent 和 AnalysisResult，不按用户描述强行新建七层模块。

本轮已完成并实测：页面参数提交、服务端校验、冻结方案与可信确认、持久队列、数据处理、真实 CUDA 推理、按真值条件评估、报告与二维/三维图表、刷新恢复。正式后端版本 `20261108-gbar-production-31196184dfd4`；前端为本机可写工作台及同步构建归档，并非新建公网网关。

## 实现与接口

- `DirectForecastForm.vue` 从真实模型目录读取身份和限制，填写批次、航班、最后 N 点及未来时距，先准备方案，再由任务卡明确确认。后台额度仅可收紧；只读预览禁止提交。
- `HermesAssistantView.vue` 复用 HTTP 工作流入口，不调用主规划器；结果不确定时保留同一 UUID 和参数，手动核对恢复。明确拒绝后允许修改参数、新 UUID 再准备。准备不会自动执行。
- `WorkflowProposal` 在 GBAR 未指定观测时长时采用350秒，CPU仍60秒；显式过短、非法时距、非整数点数仍拒绝。精确最后200点不会悄悄补更多点。
- 私有技能桥返回经过清理的字段路径与规则，GBAR窗口错误明确指向观测时长/时距，要求针对字段修正一次，仍失败即停止，不反复改无关字段。主提示对明确批次/航班的模型请求直接进入目录与方案准备。
- 任务卡显示实际模型/模型版本和正式GPU发布身份；workflow版本不再当作模型版本。中文步骤名，区分200个原始观测点与36个模型重采样步。

接口复用：`POST /api/v1/hermes/threads` 创建会话；`POST /threads/{thread}/workflow-tasks`（此前缀下）提交 `request_id, proposal, budget`；`POST /threads/{thread}/model-tasks/{task}/confirm` 提交 `confirmed:true,snapshot_sha256` 返回202；任务与制品均从同一受归属保护的GET入口读取。示例 proposal：

```json
{"kind":"forecast","batch":"scat20161015_20161021","external_ids":["100002"],"max_flights":1,"models":[{"model":"scat-gbar-v23","version":"23"}],"observation_last_n":200,"forecast_origin":"last_observation","horizons_s":[10,30,60],"max_cutoffs_per_flight":1}
```

页面入口：工作台 → 流程模板 → 直接准备预测方案 → 填参数 → 准备预测方案 → 核对任务卡 → 确认参数并执行。后续查看预测摘要/轨迹，二维默认街道底图，支持三维、定位、全屏；刷新读取持久结果。

## 真实验收与失败边界

本轮最初的一次自然语言请求失败，原运行 `run_4c2acad33ea94451a13eaebf73e9ed1f` 保留。模型未传 `min_observed_s`，通用默认60秒不符合GBAR350秒要求；旧桥只返回通用ValidationError，模型反复准备14组提案并改动无关字段。该次无新任务、无GPU调用，不能算自然语言初轮全链通过。

修复后14组原提案全部原参数重新校验通过，默认350秒，新增LLM/GPU均0；字段反馈另有隔离测试。为避免追加规划费用，本轮不再发新自然语言请求。完整真实链路通过新的直接参数表单验收；两种入口共享服务端契约、确认、队列和后台执行。

真实成功任务 `fde01f0767bc4fd38932a7b688c958f9`，会话 `871d7df4-9e1e-4d93-abc5-807089943808`：页面准备1次201、确认1次202、队列queued、1个独立native session，自动完成数据准备/推理/评估汇总/诊断/报告构建/报告读取，终态succeeded。未使用维护恢复或人工补写结果。

批准快照SHA、冻结输入SHA、5份步骤制品SHA、GPU请求/响应SHA及源代码指纹全部核验。恰好最后200原始点，GBAR预处理36步，从冻结末点预测未来10/30/60秒；无未来真值，误差样本0、误差指标0。发布2份分页结果（摘要、轨迹），图中36观测点加末点锚点及3预测点，共40点。仅1次真实GPU POST，calls=1；旧6份GPU回执完全未变。桌面/手机2D、3D及刷新GET回读通过，无写请求、页面异常或浏览器认证头。

预算：初稿按100万家庭预算描述与实际主角色配置不符，后续只读核对为既有48轮/200万保守单位/96工具/600秒，未修改或提高配置。失败规划17次LLM、17工具、1569959单位；直接后台上限收紧为剩余430041单位，实际9次LLM、10工具、351712单位。两次合计26次LLM、27工具、1921671单位（供应商输入507114、输出4325），低于200万。两次为独立账本，失败规划未混入直接任务，未重置或增额；保守单位不是费用。

## 测试与证据

| 验证 | 最终结果 |
| --- | --- |
| 服务器既有scat-agent全后端 `python -B -m pytest tests -q --basetemp=.tmp/model-flow/full-tests` | 1495 passed / 2 skipped，280.85秒 |
| 新HTTP闭环测试 | 非法点数422、数据不足409、确认摘要冲突409、重复准备/确认幂等、单次计算、重开读取、跨会话404 |
| 前端 `vitest run --maxWorkers=2` | 35文件，162 passed |
| 前端格式、ESLint、类型及生产构建 | 通过；保留既有大块构建告警 |
| 普通桌面/手机Playwright | 36 passed，30.4秒，隔离模拟接口 |
| 旧未来预测/回测真实GET-only | 4 passed |
| 新页面真实GPU全链 | 1 passed，1.6分钟，推理仅1次 |
| 新任务桌面/手机GET-only | 2 passed，12.4秒 |
| 服务端审计、Ruff、候选/线上配置验证 | 通过 |

真实脚本 `model-flow-direct-live.spec.ts` 需显式环境变量SCAT_MODEL_FLOW_DIRECT=1，结果哨兵存在即拒绝重跑；不要删除哨兵重复验收。回读脚本使用SCAT_MODEL_FLOW_READBACK=1、SCAT_MODEL_FLOW_NEW_READBACK=1，只允许GET/HEAD。直接任务默认额度100万，本次通过表单明确降低到430041。

保留失败日志：空会话页缺“新建会话”按钮的预提交脚本失败（新任务/GPU均0）；Edge沙箱启动失败；默认过多Vitest worker导致EPERM；一次错误工作目录的Vitest别名/测试收集失败；专项测试夹具漏external_ids导致先触发另一校验，补夹具后6专项通过；自然语言默认值失败；发布前SSH断线。断线后只读核实最终切换命令未执行，再独立发起切换，不盲重放部署或GPU。审计脚本的导入顺序Ruff问题已修正。最终通过不覆盖或改写首次失败。

正式证据 `var/validation/20261108-model-flow`，汇总 `docs/validation/20261108-model-flow-acceptance.json`。不包含原始轨迹/私有SQLite；私有备份和冻结原始数据仅保留服务器。

## 发布与运行纪律

沿用已有Anaconda scat-agent/scat-hermes、本机base传输和scat-frontend检查；前端已有Node24/npm11，无装包、新环境、venv/uv。前端包锁SHA256为 `e0434b056f4173684e64742bbf73c3de48ffe5e1711827bf6a7f88b59c1b4e37`，未改变。

两次空闲门槛切换：先2f44399976c0提示修复，再31196184dfd4完整修复；各有回滚Compose/状态备份，最终仅重建五核心。6份补丁在业务及native镜像均验证；批准快照中81份实现指纹与两角色当前文件一致。活动技能 `2b640dc9f6788b35748d313e147ad7852526206de87d9ae3bd953b23757fb0cc` 保持。GPU发布 `scat-gbar-v23:20261010-485de7947678-deps7d416e4aac0f-sec3`，device=cuda、model_load_count=1。

启动标识一致，服务器未重启，GBAR与两作者容器ID未变，七核心健康，自动学习调度恢复active；不改驱动、权重、主后台预算或系统DNS。正式平台18120、主Agent18121、后台18126/18128、作者18124及现有检索/GBAR端口复用。本机工作台5175、内存凭据代理18152/18153保留供查看；可运行start-workspace.cmd续开。最终前端构建归档 `var/deploy/frontend/20261108-model-flow`，源码/公开证据与服务器逐SHA同步；本轮暂存仅两端`.tmp/model-flow`，收尾删除，独立活动workspace生命周期目录保留。

本轮功能闭环通过不表示科学精度、负载SLA、独立RAG人工准入或标准GPU容器迁移也通过。自然语言修复做原提案复验，未追加付费规划全链。
