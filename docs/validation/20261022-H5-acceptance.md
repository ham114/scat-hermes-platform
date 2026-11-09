# H5 开发与测试验收

日期：2026-10-22。主计划 v1.8。H5 在固定单用户、受控模型范围完成；H6/H7 保持后续阶段。

## 最终结果

| 验证 | 结果 |
| --- | --- |
| 本机相关 Python | 47 passed；scat-frontend Conda / Python 3.12 |
| 服务器完整后端 | 811 passed、1 条既有 Starlette 弃用提示，18.21 秒 |
| 真实 Mongo | 创建随机 h5_validation_* 隔离集合，运行/结果持久化、归属校验、恢复通过，finally 清理通过 |
| 前端 | 96 passed；格式、ESLint、TypeScript、生产构建通过 |
| Edge 桌面/移动 | 10 passed，10.3 秒；人工确认、刷新不重提、unknown 无执行按钮及原对话恢复/停止通过 |
| 真实调用闭环 | DeepSeek → 固定 Hermes → prepare → 人工确认 API → 独立 worker → 原确定性任务图 → Mongo artifact → 平台重启恢复，通过 |
| 依赖 | scat-agent pip check 通过；未安装/升级依赖，均使用既有 Anaconda |

实际调用与持久状态都校验：准备后 pending 且 Mongo run 不存在；重复确认返回同一 run；连续两次独立 worker 领取后只有 1 条 run 与 1 份 artifact；重启 FastAPI 后仍能读取原结果，跨会话 404。模型准备、确认、领取时记录并核对模型/组件版本、数据范围、参数摘要与来源。

## 故障与边界

覆盖确认过期、owner/thread 交叉访问、参数摘要变化、额外身份/confirmed 字段拒绝、撤销重放、并发领取、worker 中断/租约过期、unknown 阻断、终态与 artifact 保存失败后禁止自动重复执行。人工确认不注册为 Hermes 工具；页面刷新和 SSE 关闭不触发提交。

## 原始记录与修复

- `20261022-H5-server-attempt1.json/.json.log`：19 项最初专项与工具 schema 检查。
- `20261022-H5-server-attempt2.json/.json.log`：808 passed/1 failed；Mongo 测试 await 生成器写法错误，创建集合前失败。
- `20261022-H5-server-attempt3.json/.json.log`：修复 Mongo 测试后的完整回归。
- `20261022-H5-server-attempt4.json/.json.log`：Hermes 实际分派兼容修复后的最终 811 项回归。
- `20261022-H5-local-attempt{1,2}.json/.json.log`：本机相关 Python 与完整前端检查。
- `20261022-H5-live-attempt1.json`：初步只验证工具完成事件，不能作为业务成功证据。
- `20261022-H5-live-attempt2.json`：真实 prepare 未产生 pending；固定版不向原生 handler 传 tool_call_id，原身份保护拒绝执行。
- `20261022-H5-live-attempt3.json`：使用实际传递的可信 session 和来源 run/参数摘要后，完整闭环通过。
- `20261022-H5-browser-attempt1.log`、H5 新前缀截图：10 项真实浏览器验证，业务 API 使用明确 mock。

失败证据保留，不回写为成功。最终链路是实际 API/进程和真实数据库，计算模型是明确标记的 CPU 夹具；无需生产 GPU 即可验证执行授权、状态持久化和恢复。

## 范围与后续

生产 `SCAT_GBAR_V23_ENABLED=false`，模型目录为空；本轮不宣称 V100/GBAR 实际推理通过。启用真实模型前仍需准备已审核的源码、权重、配置和后端环境，并独立执行 GPU 验收。没有修改 DeepSeek Key、数据库凭据或生产模型开关，没有发布 Nginx/Compose 或留下常驻测试服务。

页面仅增加 H5 所需确认、查询和 JSON 下载；完整通用 AnalysisResult 渲染、多轮展示调整和七类展示对等继续按 H6 计划实施。具体接口、启动环境、unknown 与 reconcile 规则见 [H5 指南](../HERMES_H5_MODEL_TASKS.md)。

两端清理、隐私扫描和当前文件 SHA256 同步详见 `20261022-H5-cleanup.json` 与独立 `20261022-H5-completion-manifest.json`。旧阶段快照保持原始字节。
