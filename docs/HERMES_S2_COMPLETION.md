# S2 收口：配置、额度、模板、引用契约与评测

> 日期：2026-10-23。开发纪律和完整端口表见 [主计划顶部](../SCAT_HERMES_PLATFORM_DEVELOPMENT_AND_TEST_PLAN.md#development-discipline)。服务器项目 `/home/h3c/project/python/scat-hermes-platform`；仅使用已有 Anaconda scat-agent/scat-hermes，本机轻量检查用 scat-frontend，禁止项目 venv/uv。本轮临时文件仅在两端各自 `.tmp/s2c`，回环探针 `127.0.0.1:18091`，结束清理；不修改既有服务。

本轮收口 S2。此前已实现的六个文档技能、26 项只读操作和原生分派绑定继续沿用，详见 [S2 第二步](HERMES_S2_READONLY_COMPOSITION.md)。本文件描述当前实现与阶段边界，验收结果以本轮 completion/原始证据为准，不把 S3–S7 的后续目标提前计为功能。

## 用户配置和有效值

维护者编辑 [skills-policy.yaml](../config/skills-policy.yaml)，机器可读契约为 [JSON Schema](../config/skills-policy.schema.json)。未知字段、非法类型、零/负额度、非有限值、期限倒置和越权提额均拒绝。配置合并为部署默认→类别策略→用户偏好→单次显式参数；后续层只能在当前获授权额度内减少，超额明确拒绝。当前为固定单用户入口，不开放模型修改配置文件。

```bash
conda activate scat-agent
python -B scripts/run_hermes_api.py --show-skills-policy
python -B scripts/skills_tool.py policy --budget '{"max_tool_calls":16}'
python -B scripts/skills_tool.py schema
```

以上命令只校验/显示配置，不启动服务或模型。实际服务使用已有 Hermes Conda 和新的独立 home：

```bash
conda activate scat-hermes
python -B scripts/run_hermes_api.py --with-mcp --with-model-tasks --with-skills --home var/hermes/skills-s2-final --readonly-budget '{"max_tool_calls":16}'
```

`--skills-policy` 可指定项目内策略文件。home 保存 `effective-skills-policy.json`，每个运行的持久预算记录保存策略摘要和额度快照。服务重启/默认配置调整不能重置同 scope/run 的已用额度；配置发生变化的独立 profile 应使用新 home。

| 配置 | S2 实际行为 |
| --- | --- |
| trace.max_hot_records | 正文/参考加载与操作回执共用总上限；事务检查，容量满拒绝新记录，不删除活动证据 |
| sessions.max_active / idle_ttl_seconds | 限制绑定容量；只有已完成且闲置超时的绑定可回收，活动运行受保护 |
| composition.max_depth | 校验发布依赖树深度；依赖仍须逐项加载，不自动继承权限 |
| budgets.readonly | 实际限制 LLM 请求次数、保守 token 准入、全部工具尝试数和墙钟；失败/重试也计入 |
| trace 保留/归档字段 | schema 和有效值已提供，自动归档/长期保留验收属于 S6；S2 容量满时明确拒绝 |
| cache | 策略预留；S2 不启用目录/业务结果缓存，每次仍核查快照 |
| budgets.background / resources | 策略预留；后台累计预算与共享任务准入由 S4 实施 |

默认只读额度为 16 次 LLM 请求、1000000 保守计量单位、32 次工具尝试、240 秒。输入按实际请求 JSON 的 UTF-8 字节预留，输出上限随剩余额度缩减；成功返回后结算，失败或缺少 usage 保留预留量。供应商实际输入、输出、缓存 token 分开记录，缓存 token 不重复加到总 token。该保守准入可能提前拒绝，不等同供应商账单精确封顶。严格货币限额尚无可靠定价/计量验证，非 null 的 max_cost 明确拒绝，不默默忽略。

预算绑定固定 DeepSeek Chat Completions 实际请求客户端及原生每次新建/复用的请求客户端；禁用 SDK 隐藏重试。辅助压缩在该 profile 中关闭，避免派发未纳入额度的独立压缩请求；上下文过长时失败并保留证据。新调用前检查时间，计时器请求中断在途对话；只读工具已有内部超时，不能把请求中断写成所有远端操作立即停止。达限的运行保留原收据并报告 `budget_exhausted`，不报告全部成功。固定原生接入文件摘要在 [budget.lock.json](../services/hermes/budget.lock.json)，不修改 vendor。

## 同一调查模板切换参数

[A 配置](../config/investigation-synthetic-a.json) 选择查询/质量/轨迹/知识；[B 配置](../config/investigation-synthetic-b.json) 换批次和呼号，仅选择查询/轨迹，并降低工具额度。两者使用相同调查技能及精确依赖版本。

```bash
conda activate scat-agent
python -B scripts/skills_tool.py render --path config/investigation-synthetic-a.json
python -B scripts/skills_tool.py render --path config/investigation-synthetic-b.json
```

输出包含受校验参数、选定技能、包/策略摘要、生效额度和可审阅请求文本。它不执行任意代码、不授予新工具权限，也不是自动 DAG 执行器；Hermes 仍须实际加载子技能并执行相应操作。模型和页面的任意新参数不能绕过业务 schema、身份或技能准入。H6 再提供模板选择/编辑、技能关系、步骤证据与专业图表界面。

## 只读结果引用契约

`ReadonlyReference` 固定 kind/schema、输出契约名、owner_scope、producer_run、result_id、操作、payload/query/发布包摘要及操作单位/时间协议版本。解析时重新查受归属控制的结果存储，验证期限、摘要、类型/协议和发布契约；不能把跨用户、跨 run、篡改或错误类型的引用交给下一个步骤。真实模型探针也通过该接口验证实际收据。

本契约只承载只读结果引用，payload 摘要不是数据库全量快照或模型样本版本；单位/时间按具体操作 schema 解释。模型张量、样本清单、模型/评估协议和生产步骤制品仍由 S3–S5 实现。相同运行可复用完整性有效的引用；更换批次/航班必须重新查询，不能直接复用旧查询的数值。

## 创作材料与题集

[authoring](../authoring/README.md) 包含 11 类统一模板和一个隔离调查候选，位于发布目录外。模板覆盖适用性、参数、契约、前置条件、允许操作、流程、完成、失败、示例和评测；未接入领域的模板是设计材料。离线校验验证目录/资源、frontmatter、依赖/契约、已注册操作和文件摘要，返回候选报告，不发布、不执行脚本、不开放作者业务 API。候选变化必须重新校验/评测，正式创作发布与回滚留 S7。

```bash
python -B scripts/skills_tool.py check-draft --path authoring/drafts/scat-investigation-candidate
```

[S2 题集](../evaluations/hermes_s2_v1.json) 为查询、质量、轨迹、知识和 UAV 各 5 个确定性场景：成功、非法/缺失参数、空结果、工具故障、未授权。执行真实应用 schema、技能分派和收据/引用检查，后端数据为合成夹具。按域报告分子/分母；该结果不等于模型技能选择准确率。保留原 H2 题集，模型/对比/评估/物化/实验/制品报告按 S3–S5 扩展，S6 再按代表题各至少 3 次验收稳定性。

[52 项覆盖清单](../evaluations/skills-coverage-v1.json) 逐项记录当前目标和未迁移原因：25 个旧 SCAT/UAV/知识查询加 1 个预测截面发现构成当前 26 项；H5 控制/历史读取保持单独授权。带数值回退和物化口径的阶段/CFPL 指标诊断、模型任务和物化/实验域继续按 S3–S5 迁移。S2 收口指本阶段只读基础门槛，不能解释为全部旧业务已迁移或已切换默认主链。

```bash
python -B scripts/evaluate_skills_s2.py --report docs/validation/new-s2-scenarios.json
python -B scripts/probe_skills_s2_final.py --case budget
python -B scripts/probe_skills_s2_final.py --case full
python -B scripts/probe_skills_s2_final.py --case reuse
python -B scripts/probe_skills_s2_final.py --case uav
```

真实模型探针只传合成数据，服务器实际数据库读取另验；不执行生产 GPU。每份报告唯一命名，原始失败保留。探针启动前核查 18091，临时 home、进程和本轮缓存结束后清理。本轮验收与清理结果如下，逐文件最终同步记录单独保存。

## 最终验收结果

- 服务器完整后端 **947 passed/1 skipped**，28.71 秒；本机相邻 **100 passed**，4.46 秒。跳过的是未显式启用的 H5 隔离 Mongo 写入验收，沿用原证据；既有 Starlette 提示 1 条。
- 五域确定性题集各 **5/5**；原生分派 7 次非法调用无后端执行、2 次合法调用及 handler 刷新校验通过，预处理/依赖安装均 0。实际 SCAT/UAV 独立 7 个读取操作与两 Conda pip check 通过，无安装。
- [预算拒绝](validation/20261023-S2C-real-budget-230df93d.json)：1 单位额度，真实 run.failed，LLM/工具调用均 0。
- [完整调查](validation/20261023-S2C-real-full-2765621f.json)：23.53 秒，6 次 LLM/11 次工具，4 个有效引用与正文关联，范围/来源及最终合成标识检查通过。
- [参数复用](validation/20261023-S2C-real-reuse-69bae6e1.json)：17.12 秒，6 次 LLM/7 次工具；切换 SYNTH-B/TEST456，关闭质量/知识分支，重新查询并验证 2 个引用，工具额度 16。
- [UAV](validation/20261023-S2C-real-uav-91e80991.json)：19.60 秒，5 次 LLM/4 次工具，2 个有效引用；最终文本中的合成标识和两个采样值与工具结果一致。
- [汇总](validation/20261023-S2C-completion.json)保留供应商 usage 与保守计量，失败报告不改写。上述每条是单次明确提示验收，不是 S6 三次稳定性或通用意图选择准确率。
- [服务器审计](validation/20261023-S2C-server-cleanup.json)：无本轮活动进程、18091 关闭、既有服务继续监听，452 个文本/日志扫描无实际私有值，两既有 Conda、原生锁摘要和私有配置权限通过；没有 venv/uv、新依赖或生产 GPU。
- 发现原生退出回调会在 finally 后重建空浏览器缓存目录，已给探针增加先注册的退出清理并单独复测；[补充审计](validation/20261023-S2C-late-cache-cleanup.json)确认本轮两个临时命名目录均不存在。该末次变更仅影响验收探针清理，不重复付费模型或完整后端。

首轮保守额度 300000 不足，实际按达限失败保存；依据输入预留实测将部署默认/上限校准为有限 1000000，再通过全部最终链路。模型不能运行中自行提额。业务探针同名保护、UTF-8 解码及 Windows 沙箱失败均保留原记录，未覆盖历史。S2 按阶段门槛完成，下一步 S3，H6 展示按原排期。
