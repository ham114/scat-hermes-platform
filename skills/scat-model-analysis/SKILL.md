---
name: scat-model-analysis
description: 对话中准备版本与范围固定的模型任务并等待页面确认；在受批准的独立后台会话中组织输入、预测、评估、保存和读取步骤。未知执行先核实，主子共享任务预算。
---

# 注册模型分析

1. 识别当前角色及工具能力。主对话提供 `scat_workflow_catalog` 时，新单模型预测/比较优先读取该实际目录并加载 scat-model-comparison，单模型使用 models 仅一项的 comparison；实验加载 scat-experiment。模型、GPU状态和输入限制以运行目录与准备校验为准。仅在没有workflow入口的兼容模式中查询 `scat_task_catalog`、用 `scat_task_prepare` 准备任务；旧目录为空不代表workflow无模型。展示实际快照与预算后等待页面确认；聊天确认不授权执行，主对话不能直接调用计算步骤。精确末尾点数/从末点未来预测必须核对契约，不能用历史回测替代。
2. 若当前是平台授予可信 job/租约/委托的独立后台会话，按下列五步组织任务，不再请求聊天确认，不准备新任务。所有子会话使用同一任务累计预算，不能自行提额或重置；父对话规划的实际/保守用量在后台启动前计入。S3 显式 CPU 兼容探针也按五步执行。
3. 调用 `scat_model_prepare_input`，无需参数；输入、模型/预处理/评估版本均来自可信任务。不得自行编造输入引用或传 confirmed。
4. 用返回的完整 input reference 调用 `scat_model_run`。检查 status；失败停止，unknown 不得换引用重跑。worker/聊天断开不代表后端停止，先由平台按原执行号核实。
5. 用实际 prediction reference 调用 `scat_model_evaluate`。指标、单位和时间轴由工具计算，禁止自行估算。
6. 用实际 evaluation reference 调用 `scat_model_save_result`；再用实际 result reference 调用 `scat_model_read_result`。只有读取标准 AnalysisResult 成功后才能报告完成。
7. 简短解释实际结果，注明模型版本、评估器、单位、时间范围、结果引用及合成标识。没有单位/指标的数据不得补造；所有数组和数学变换都留在工具内部。

重复成功操作复用原回执；过期、归属/运行不匹配、摘要或类型错误必须停止。子 Agent 只能使用明确委托给本会话、属于同一已批准 job 的引用；其他 job 或未经授权的跨 run 引用拒绝。模型可用性由运行目录和可信任务决定，加载本技能不启用GPU、不扩大样本或预算。
