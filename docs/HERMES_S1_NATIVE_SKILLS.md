# S1 原生技能接入与验收

> 日期：2026-10-23；原生加载及首批只读 POC 范围已验收；全业务迁移仍待 S2–S6。

## 1. 本阶段实现

固定 Hermes v0.22.3 / 5b147ab036d6d19131bff8d20701b5001eab844a。新增 --with-skills 运行配置，只注册原生 skills_list/skill_view 的受控适配，工具组为 scat_skills；不启用包含 skill_manage 的整个 skills 组。不修改 vendor。

services/hermes/skills.lock.json 是明确的发布清单，包含版本、固定提交、所核查原生源码摘要、允许引用的操作和逐文件摘要；运行时不会从任意文件自动生成批准清单。首批为 scat-flight-query 与 aviation-knowledge，航班技能附有查询契约参考。

启动时校验源码及技能包，将已校验字节复制到独立运行 home/skills/；已有内容不同则拒绝复用该 home。源技能修改不改变已运行快照，新版本需独立 profile/发布切换。加载前后核对快照与原生发现目录，拒绝额外目录、未发布文件、路径逃逸及链接。文件上限 64 KiB、包合计 512 KiB、最多 16 个技能，每技能最多 64 个文件。

原生 skill_view 显式 preprocess=False；准入限制 frontmatter 为 name/description/空 deps，非空 deps 与主动配置字段直接拒绝。底层实现保留原生 skill_view/skills_list 的实际解析和读取，只在结果校验后提供内容。读取脚本不代表允许执行脚本。

技能加载按服务端绑定的 session/scope/run 记录技能版本、发布包摘要、文件摘要与时间，最多 10,000 条后明确拒绝新增加载；不会把模型声称“使用技能”当作证据。此阶段不暴露历史加载记录查询 API，后续按归属增加访问与保留策略。

## 2. 启动

服务器使用已有 scat-hermes Conda Python 3.14；业务 MCP/任务 CLI 仍为 scat-agent Python 3.12。使用项目内新运行目录，避免覆盖 H5 已存在的不同配置：

```bash
/home/h3c/anaconda3/envs/scat-hermes/bin/python -B scripts/run_hermes_api.py \
  --home var/hermes/skills-s1 --port 18081 --with-mcp --with-model-tasks --with-skills
```

仍仅监听 127.0.0.1。启用后实际工具必须恰好为原五只读 MCP、五任务目录/准备/读取工具及两技能读取工具，共 12 个；集合不一致则拒绝创建 Agent。--with-skills 必须与 --with-mcp 一起使用。旧 H5 默认启动方式保持可用，正式入口切换留后续阶段。

## 3. 验证方式

- 本机 scat-frontend Conda：S1 边界测试及相邻 H5 桥/协议/结果回归；pytest 临时目录显式放 .tmp/s1 下。
- 服务器 scat-hermes：scripts/probe_native_skills.py，实际调用固定版列表、两技能正文和参考读取，捕获并禁止预处理与依赖安装入口，验证错误路径、未绑定会话、变更快照等拒绝；无 LLM 或 GPU 调用。
- 服务器 scat-agent：完整后端 pytest；scripts/probe_hermes_skills.py 启动独立 Hermes 子进程，用真实 DeepSeek 和明确标记的合成 MCP，必须有原生列表、正文/参考的可信加载记录及成功查询回执。真实 Mongo/航班/轨迹另由 scripts/probe_skills_business_readonly.py 在服务器内验证，不调用 LLM。
- 单次真实验证最多 8 轮、流式等待 240 秒；记录实际耗时，不把这个限制当作已测得的 API 账单金额。
- 探针只清理自己创建的临时 profile/进程，保留脱敏证据和日志；没有常驻测试服务、无 GPU 任务、无额外依赖安装。

## 4. 验证记录

首次本机 54 passed / 13 failed：新增夹具在 Windows 写 CRLF、却按 LF 字符串计算摘要，发布校验正确拒绝；夹具改为明确 LF。后续相关回归 67 passed。首次服务器原生验证发现 Hermes 环境没有 PyYAML，改为复用现有 ruamel.yaml 安全解析器，未安装依赖。失败和后续成功证据分别保存。

最终验收：

| 项目 | 结果 | 证据 |
| --- | --- | --- |
| 本机相邻回归与新边界 | 69 passed；Ruff check/format 通过 | [本机最终 XML](validation/20261023-S1-local-completion.xml) |
| 服务器完整后端 | 841 passed / 1 skipped，25.93 秒；沿用一条 Starlette 弃用提示 | [完整回归](validation/20261023-S1-server-final-pytest.log) |
| 固定版原生功能 | 两个正文与一个参考读取，共 3 份可信记录；预处理和依赖安装均 0 次 | [原生探针](validation/20261023-S1-native.json) |
| 真实模型链路 | DeepSeek 依次 skills_list → skill_view → 参考 skill_view → 合成航班查询；模型运行 12.95 秒 | [真实模型与合成 MCP](validation/20261023-S1-real-a10713ed.json) |
| 真实业务独立读取 | 数据库计数与 MCP 总数一致，读取 1 航班/1 轨迹点；LLM 调用 0 | [服务器内验证](validation/20261023-S1-business-local.json) |

跳过项为需显式 SCAT_H5_REAL_MONGO=1 的 H5 隔离 Mongo 写入验收；此次未改写入流程，沿用 H5 原证据，不把跳过算通过。源码与技能配置修改后已再跑完整回归。

真实模型首轮在健康检查前失败：Hermes 初始化预建空 skills/，适配器未填充；修复后允许初始化空目录，对混杂或部分目录仍拒绝，并新增两项回归。首次失败 [记录](validation/20261023-S1-real.json) 保留，成功探针停止子进程并删除隔离 profile。

自动审批拒绝了内部航班数据发送至 DeepSeek 的实测，原因为缺少具体业务数据外发的明确授权；被拒操作未执行。采用上述合成模型链路与服务器内真实读取作为 S1 验证组合，不宣称完成内部真实数据的外部模型全链路验收。当前没有为此扩大权限。

清理和最终文件摘要同步结果见本轮独立 cleanup/sync 记录。

## 5. 阶段边界

S1 是固定版原生加载与首批查询的验证入口。现有 H5 模型 worker 仍走旧图，尚未将全部业务迁为技能；S2–S6 保持原计划。提示词要求先加载技能，S1 验收验证实际加载；对全部业务操作强制绑定适用技能、逐步骤效果权限和后台恢复分别在 S2–S4 完成。

本轮不开放自主编辑/发布工具，不实现 S7 草稿 API，也未完成 H6 图表通用展示、H7 Compose 或真实 V100/GBAR 验收。自主创作及完整功能划分见 [技能生命周期与工具映射](HERMES_SKILLS_LIFECYCLE_AND_TOOL_MAPPING.md)。
