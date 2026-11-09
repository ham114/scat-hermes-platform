# 前端展示能力核对与验收要求

2026-11-08 当前状态：通用渲染及回执投影已上线，metrics/table/timeseries/trajectory_2d有本轮真实HTTPS桌面/手机证据；七类真实同数据和旧Streamlit实机交互对照仍未全部完成。参见 [业务补齐记录](HERMES_BUSINESS_PARITY_ROUND_20261108.md)。下文“待接入”表为2026-10-22历史基线，不能作为当前功能状态。

2026-10-25：H6已恢复开发，七类基础渲染与受归属任务artifact预览接入，前端118/浏览器4/服务器契约14+1通过；地图/播放/三维交互对等、消息结果关联与多轮展示继续。实际范围见[H6实施](HERMES_H6_DISPLAY_AND_RAG.md)，下文保留原核查时点。

日期：2026-10-22。结论：现有 Vue 3 / TypeScript / ECharts / Cesium 技术路线可以覆盖旧项目的现有展示能力；当前 Hermes 对话页尚未完成通用结果渲染及 LLM 展示编排，不能将技术可行等同于已实现或效果已验收。

用户要求：最终展示效果和交互能力不得低于旧项目，仍在 H6 前端与业务质量阶段验收。2026-10-23 主计划 v2.0 已按新要求插入 S1–S6，使原生 Hermes Skills 主导核心执行；展示标准不降低，本轮仍不开发前端或制作原型。

## 旧项目基线与对应能力

核对旧目录与新项目：streamlit_ui/analysis_renderer.py 和 schema/analysis.py 内容完全一致，原渲染器及数据协议保留。旧渲染器按 AnalysisResult.blocks 分派七类结果，在所查 Agent 与 Streamlit 渲染源码中未发现执行 LLM 动态 Python 的 exec/eval 路径。

| 旧结果类型 | Vue 侧实现基础 | 当前 Hermes 对话入口状态 |
| --- | --- | --- |
| metrics 指标卡 | Vue 通用组件与数值格式化 | 待接入 |
| table 表格 | 通用表格，沿用列名、单位和空值语义 | 待接入 |
| timeseries 时序曲线 | BaseChart / ECharts 配置驱动；已有时间曲线组件 | 待通用适配 |
| trajectory_2d 轨迹 | 已有二维播放、三维 Cesium、时间游标和快照组件 | 待连接对话结果引用 |
| classification 分类 | 通用表格与分类结果组件 | 待接入 |
| prediction 预测 | 既有预测轨迹、曲线及 artifact 数据链路 | 待通用适配 |
| action_link 结果链接 | 既有 artifact API 与可视化路由 | 待适配 Hermes 会话归属及返回入口 |

现有 BaseChart 通过 option 驱动并监听更新，已注册折线、柱状、散点；其他图表需按需注册。已有专业组件不代表七类结果已能在聊天中自动组合。

## 动态组织方式

保留真实工具数据与 artifact；Hermes 输出受校验的展示描述，选择内容块、顺序、字段、图表配置和支持的布局，Vue 通用渲染器据此组合。用户自然语言修改展示时更新描述，数据分析变化则重新调用业务工具。无需为每次分析单独编写页面。

原 AnalysisResult 适合复用数据语义，但当前七类结构不包含任意页面布局或任意图表配置；后续须通过版本化契约或单独展示描述扩展，不能把“换框架”当作动态编排已实现。数据值、单位、时间和指标来源仍由真实结果决定。

LLM 临时编写任意 Python 绘图需要独立的执行与产物机制，本次源码核查未证明旧入口已具有该能力，也不将其加入本轮实现范围。

## H6 对照验收

1. 使用同一组真实 AnalysisResult/artifact 对照旧 Streamlit，覆盖七类结果、空数据和失败结果；数值、单位、时间与来源保持一致。
2. 对照二维轨迹的快照、序列、放大、底图和结果跳转；已有三维、播放及曲线联动保留。底图资源和浏览器 WebGL 以实际目标环境验证。
3. 同一数据支持自然语言改变内容顺序、选取字段和受支持的图表表达；不能为每个问题新增固定页面，也不能只返回图表文字描述。
4. 同一结果可从消息再次打开，刷新与恢复后不丢关联；显示异常、缺失产物或无权限时给出真实状态。
5. 在桌面与移动视口核对图表可读性、布局及交互；达到旧项目基线后再验收展示能力，不以组件存在或单元测试通过代替效果对照。

## 核查依据

- [旧结果协议](../src/schema/analysis.py)、[保留的 Streamlit 渲染器](../src/streamlit_ui/analysis_renderer.py)
- [图表基础组件](../frontend/src/components/BaseChart.vue)、[三维组件](../frontend/src/components/Trajectory3dView.vue)
- [当前 Hermes 对话页](../frontend/src/views/HermesAssistantView.vue)
- [Vue 动态组件](https://vuejs.org/guide/essentials/component-basics.html#dynamic-components)
- [ECharts 动态更新](https://echarts.apache.org/handbook/en/how-to/data/dynamic-data/)
- [Cesium Viewer](https://cesium.com/learn/cesiumjs/ref-doc/Viewer.html)

源码一致性摘要：analysis_renderer.py = 52401ecdcd21093a22ded6965ecb08cab9bbb9c0c528b8f7b0c1041f36b74634；analysis.py = 43985c94de906333c1c7d6ac14aacc75243991d27e46437dd9e6349939a89938。本次为源码与官方能力核查，仅更新本机文档，无前端代码、依赖或原型变更；效果对照测试留在原阶段执行。

新版执行流程和阶段顺序见 [Skills 方案与复用评估](HERMES_SKILLS_ARCHITECTURE_AND_REUSE_20261023.md)。
