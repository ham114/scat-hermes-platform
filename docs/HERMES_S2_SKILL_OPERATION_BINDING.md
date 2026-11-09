# S2 第一步：运行级技能与业务操作绑定

> 日期：2026-10-23。开发纪律、解释器和服务地址以 [主计划顶部](../SCAT_HERMES_PLATFORM_DEVELOPMENT_AND_TEST_PLAN.md#development-discipline) 为准。服务器项目固定 `/home/h3c/project/python/scat-hermes-platform`；只使用已有 Anaconda 环境，不使用项目 venv/uv。所有临时文件、缓存和探针 profile 都在项目内，结束后清理本轮文件和进程。
>
> 当前状态：S2 首步在本文件限定范围验收通过。两端相邻回归各 62 项通过，服务器完整后端 864 passed/1 skipped；固定版原生分派与真实 DeepSeek+合成 MCP 链路通过。本步不代表 S2 全部完成，也不切换默认 H5/现网入口。

## 1. 本步交付与边界

- 新增 `src/hermes_runtime/skill_guard.py`，在固定 Hermes 的原生 `registry.dispatch` 入口校验技能使用，适配由平台安装，不修改 vendor 源码。MCP 重新注册实际 handler 不会替换这一分派入口；原生行为仍需服务器探针验收。
- 仅在 `--with-skills` 模式启用。五类只读 MCP 和 `scat_task_prepare` 作为新业务操作受检查；技能目录/读取及四类任务目录/历史读取继续走各自原有授权。未启用 Skills 的默认 H5 不受本步影响。
- 当前发布包仍只有 `scat-flight-query` 和 `aviation-knowledge`。只有加载适用正文后，发布清单允许的航班/批次/轨迹查询或知识检索才可进入 handler。质量检查和模型准备尚无匹配技能，Skills 模式明确拒绝，后续技能迁移再开放。
- `services/hermes/skills.lock.json` 发布包版本更新为 `s2.0.0`，补固定 `tools/registry.py` 与 `model_tools.py` 的源码摘要；技能正文未改。S1 历史验收、原始日志和旧 H5 启动方式保留。
- 本步是只读操作与准备入口的技能准入/审计。后台效果操作授权、批准 job、租约代次、步骤幂等和恢复仍按 S3/S4 开发；容量、归档与预算配置仍按主计划 14.7 实施，当前记录容量仍是保守固定上限。

## 2. 调用与审计契约

1. 平台为实际 native session 绑定可信 scope 与 run；模型传入的 owner/run/skill 参数不会产生授权。
2. 调用业务操作时，先检查参数对象、运行快照与技能目录完整性，再从 `skill_loads` 查询同一 scope/run/session 的 `SKILL.md` 加载回执。仅列表、仅参考、上一运行或其他会话的加载均不授权。
3. 回执中的技能版本、发布包与正文摘要必须匹配本 profile 的固定发布。多个技能可组合使用，每个操作选取实际允许它的技能正文回执，保留具体来源。
4. 在转发前写入 `skill_operations`：可信归属/运行/会话、操作名、规范化参数摘要、技能/版本/包/正文摘要、load_id、起始时间。不能保存参数原文、模型对话或业务数据。审计存储不可用时不转发。
5. handler 返回后保存结束时间、结果摘要和 `returned`；该状态仅表示已返回，不代表业务成功。是否成功仍检查工具实际 status 与平台可信业务结果收据。异常保存 `dispatch_error`；调用后回执无法持久化时返回明确错误，不能伪造成功。
6. 当前记录上限为 10000，满额明确拒绝；后续统一接入用户容量/归档配置。重启不能靠模型声称之前加载过来恢复使用关联，每个新运行都要有本次的可信加载记录。

| 错误码 | 含义 |
| --- | --- |
| `trusted_skill_context_required` | 无可信 native session 绑定 |
| `skill_required` | 本次运行没有正文加载记录 |
| `skill_operation_not_allowed` | 已加载技能不允许该操作，或操作不在运行白名单 |
| `skill_snapshot_mismatch` / `skill_snapshot_changed` | 加载身份或实际文件与固定快照不符 |
| `invalid_skill_operation_arguments` | 操作参数不是对象 |
| `skill_operation_admission_unavailable` | 参数规范化或审计存储等准入设施不可用 |
| `skill_operation_trace_capacity` | 操作记录达到当前容量上限 |
| `skill_operation_dispatch_unavailable` | 实际分派抛出异常，不返回内部错误细节 |
| `skill_operation_receipt_unavailable` | 分派后未能完成回执持久化 |

## 3. 测试范围与当前证据

新增 23 项用例覆盖未加载/仅列表/仅参考/错误技能、合法加载与 load_id 关联、跨 session/scope/run、重新绑定、技能组合、不允许操作、原控制面读取、失败状态、快照/加载摘要变更、审计写入失败、分派入口完整性、并发隔离和异常脱敏。

| 验证 | 当前结果 | 证据 |
| --- | --- | --- |
| 服务器代码格式与 Ruff | 初次与最终均通过；冗余 noqa 修正已在服务器应用 | [首次源码检查](validation/20261023-S2-server-initial.json)、[最终 Ruff](validation/20261023-S2-server-ruff.log) |
| 本机首轮技能与加载边界 | 54 passed，3.41 秒 | [首轮日志](validation/20261023-S2-local.log)、[XML](validation/20261023-S2-local.xml) |
| 本机相邻最终回归 | 62 passed，1.87 秒；Ruff check 通过 | [最终日志](validation/20261023-S2-local-final.log)、[XML](validation/20261023-S2-local-final.xml) |
| 本机异步等待诊断 | 已定位 Windows 沙箱阻塞 socketpair，允许本机回环后通过；未改 asyncio/业务代码 | [诊断日志](validation/20261023-S2-local-adjacent2.log) |
| 服务器固定版原生分派 | 通过；6 次拒绝均未触发 handler，2 次合法调用分别绑定两次运行；handler 重新注册后仍受拦截 | [原生探针](validation/20261023-S2-native-5c50d60a.json) |
| 服务器相邻回归 | 62 passed，0.87 秒 | [日志](validation/20261023-S2-targeted.log)、[XML](validation/20261023-S2-targeted.xml) |
| 服务器完整后端 | 864 passed/1 skipped，26.59 秒；沿用一条 Starlette 弃用提示 | [日志](validation/20261023-S2-server-full.log)、[XML](validation/20261023-S2-server-full.xml) |
| 两套环境依赖 | scat-agent / scat-hermes 的 pip check 均通过，无新增依赖 | [应用环境](validation/20261023-S2-server-app-pip-check.log)、[Hermes 环境](validation/20261023-S2-server-hermes-pip-check.log) |
| DeepSeek + 合成 MCP + 绑定回执 | 通过，模型运行 10.16 秒；1 条成功业务收据与当前技能正文加载匹配；无真实数据库查询 | [真实模型报告](validation/20261023-S2-real.json)、[有限驱动记录](validation/20261023-S2-server-driver.json) |
| 服务器收尾 | 本轮活动进程 0，18088/18089 关闭，项目内本轮临时目录清理；固定源码摘要一致 | [清理与环境审计](validation/20261023-S2-server-cleanup.json) |

本机首轮扩大回归因异步事件循环创建被沙箱阻塞而中止；诊断轮保留等待堆栈，不算测试失败或通过。初次本机 Ruff 发现一条多余的 noqa，删除后通过。SSH 上传首轮独占写模式不正确，仅产生空暂存文件；该文件已在后续连接中核实清理，原文档未受损，修正后同步成功。所有原始记录与最终结果分别保存。服务器跳过项为需显式开启的隔离 Mongo 写入验收，本轮未改该写入流程，沿用历史 H5 证据，不把跳过算通过。

## 4. 服务器续测入口

以下为本轮已验证测试的复跑入口，全部在服务器项目目录执行。复跑前检查端口和 Conda 解释器，证据文件另起名称，避免覆盖历史记录。

```bash
cd /home/h3c/project/python/scat-hermes-platform
mkdir -p .tmp/s2dev/tmp
export TMPDIR="$PWD/.tmp/s2dev/tmp" TEMP="$PWD/.tmp/s2dev/tmp" TMP="$PWD/.tmp/s2dev/tmp"
export PYTHONDONTWRITEBYTECODE=1 RUFF_CACHE_DIR="$PWD/.tmp/s2dev/ruff"
/home/h3c/anaconda3/envs/scat-agent/bin/python -B -m pytest \
  tests/hermes_runtime/test_skill_guard.py tests/hermes_runtime/test_skills.py \
  tests/hermes_runtime/test_task_bridge.py tests/hermes_runtime/test_results.py \
  -q -p no:cacheprovider --basetemp=.tmp/s2dev/pytest-targeted
/home/h3c/anaconda3/envs/scat-hermes/bin/python -B scripts/probe_skill_guard.py
/home/h3c/anaconda3/envs/scat-agent/bin/python -B -m pytest tests \
  -q -p no:cacheprovider --basetemp=.tmp/s2dev/pytest-full
```

原生探针仅用合成 handler，经固定版 `handle_function_call` 验证加载、拒绝、跨运行、handler 重新注册与审计关联；不调用 LLM、Mongo 或模型。真实模型链路另用合成 MCP 验证。需要人工启动交互服务时使用新 profile，例如 `var/hermes/skills-s2`，不覆盖已有不同配置的 S1/H5 home；先选择空闲回环端口并明确托管/停止方式。

## 5. 收尾要求与剩余任务

- [x] 原生、后端、依赖检查与合成模型验证完成，最终服务器源码及证据已取回；代码摘要见服务器清理报告。
- [x] 服务器本轮临时目录及空 MCP/父目录已清理；373 个文本文件未发现实际私有值，.env 保持 0600，无项目 venv。原平台/RAG/Mongo 监听保持，未改现网。
- [x] 本机本轮临时目录已清理，385 个文本文件未发现配置中的实际私有值；正式源码、依赖、日志与私有配置保留，见 [本机清理](validation/20261023-S2-local-cleanup.json) 和 [扫描记录](validation/20261023-S2-local-scan.json)。
- 最新文档与本机证据按基线摘要保护同步，两端最终文件一致性以本轮 `20261023-S2-sync.json` 清单为准；文档/收尾变更不重复执行未改动的业务测试。
- S2 全域技能覆盖、组合契约、配置基础和创作样例仍未完成；本步验证通过后继续按主计划推进，不提前开始 H6。
