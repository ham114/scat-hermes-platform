# 前端能力基线与技术选型确认

2026-10-25：用户恢复H6前端开发，七类基础结果/任务预览已接入；完整专业交互与动态编排仍按本基线验收。当前状态见[H6实施](HERMES_H6_DISPLAY_AND_RAG.md)，下文为历史选型记录。

基线日期：2026-10-22；2026-10-23 同步新版规划。技术与展示对等要求保持，本轮不开发前端或原型；主计划 v2.0 先做 S1–S6 Skills 执行迁移，再继续 H6/H7/H8。

## 结论

继续使用 Vue 3 / TypeScript + ECharts / Cesium + FastAPI + Hermes。经现有代码及官方文档核查，该组合具备覆盖旧项目结果展示、模型动态组织内容及多轮对话所需的技术能力。最终展示效果必须通过同样业务样例与旧项目对照验收；当前助手页仍以文字和工具状态为主，通用结构化结果渲染及动态编排尚待 H6 开发，不能将技术可行性记录为已完成效果验收。

## 核查依据与能力对应

旧项目 `src/schema/analysis.py` 定义 AnalysisResult 与 blocks；`src/streamlit_ui/analysis_renderer.py` 按类型通用渲染，下列七类是必保能力：

| 旧结果块 | 目标实现基础 | 后续验收要求 |
| --- | --- | --- |
| metrics | Vue 指标组件 | 数值、单位和空值语义一致 |
| table | Vue / Element Plus 表格 | 字段、记录、顺序与空结果一致 |
| timeseries | ECharts，复用 BaseChart | 时间轴、单位、曲线与选择状态一致 |
| trajectory_2d | ECharts 与现有轨迹组件 | 观测/预测/真实未来的角色、分组和查看操作保留 |
| classification | Vue 表格/图表组件 | 类别、数量或概率等原结果语义一致 |
| prediction | Vue / ECharts 预测展示 | 原始预测值、时间与上下文对应 |
| action_link | 路由及 artifact 引用 | 结果查看、2D/3D 跳转、返回上下文可用 |

已有 Vue BaseChart 接收 option 并通过 setOption 更新，已有轨迹播放、曲线和 Cesium 可视化代码可复用。上述是技术与代码基础，尚未把七类结果都接入 Hermes 对话页。

## 动态组织与对话要求

- LLM 可选择结果块、顺序、说明、标题以及已支持的图表类型/字段/分组/布局配置；前端按版本化协议统一渲染，无须为每个问题写独立页面。
- 数据和指标来自真实工具或受归属控制的 artifact，保留来源、单位与结果标识；对后续“换图形、调整顺序、增加对比”等请求保持同一分析上下文。
- 当前旧结果协议覆盖七类块；新增布局和图表选择仍需要协议扩展及校验，不能宣称现有 schema 已支持任意布局。
- 对话使用 Hermes + FastAPI HTTP/SSE。此前 H3/H4 已验证真实多轮、文本流、工具事件、停止、断流续接和当前标签页恢复；完整历史浏览、模型任务取消语义及结果联动按原阶段继续完成。
- LLM 任意编写并执行 Python/JavaScript 不是本次核查出的旧结果协议能力。若以后确需任意新图生成，另行设计执行与产物服务，不预先把它加入本轮开发。

## 开发阶段与对照验收

H1–H5 旧架构基线已验收。按用户 2026-10-23 新要求，先完成 S1–S6，使原生 Hermes Skills 主导核心执行；H6 继续接入通用结果渲染、报告技能生成的受校验展示组织与多轮调整，H7/H8 完成部署和交付。已生成 artifact、状态与历史展示仍直接读取，不为每次重绘重新执行分析。本次不提前开始前端开发。

H6 必须用同一问题、同一底层数据和同一结果契约，与旧 Streamlit 逐项对照七类块；检查数值、单位、图形语义、缺失/空结果、引用跳转以及原有交互，不能只看页面截图。另验收多轮修改展示、结果与对话关联、刷新恢复、桌面/手机可用性。没有完成对照的功能保留旧入口，不计入迁移完成。

## 参考

- [Vue 动态组件](https://vuejs.org/guide/essentials/component-basics.html#dynamic-components)
- [ECharts 动态数据与配置更新](https://echarts.apache.org/handbook/en/how-to/data/dynamic-data/)
- [已验证对话链路及边界](validation/20261022-H3-H4-business-api-portal.md)
- [开发与测试计划](../SCAT_HERMES_PLATFORM_DEVELOPMENT_AND_TEST_PLAN.md)

本次为文档核查，不改运行代码、依赖、页面或部署，不重复执行未受影响的后端/前端完整测试；对话能力证据沿用既有真实链路和浏览器测试，展示对等验收仍为后续工作。

新版执行架构和复用盘点见 [Skills 方案](HERMES_SKILLS_ARCHITECTURE_AND_REUSE_20261023.md)；技能化不降低此文七类结果与原有交互的验收要求。
