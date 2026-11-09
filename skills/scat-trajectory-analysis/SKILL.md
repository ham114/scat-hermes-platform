---
name: scat-trajectory-analysis
description: 分页分析 SCAT 实际轨迹、飞行计划、阶段及数据中已有预测，比较既有预测误差；不发起新模型推理。
---

# 轨迹与既有预测分析

1. 使用用户给定或 scat-flight-query 返回的 batch/external_id；只有呼号或需要航班摘要/搜索时，显式加载 scat-flight-query 后调用其工具。每轮都重新加载本轮需要的正文，不沿用上一轮授权。
2. 按目标选择 get_scat_track_points、飞行计划概览或飞行阶段统计。轨迹默认20点、最多100点，缩小时间和字段范围。
3. 只在用户需要时读取已有预测概览。需快照时先分页 list_scat_prediction_snapshots 获取真实时间，再取 get_scat_prediction_snapshot，不猜造时间戳。
4. 误差使用 get_scat_prediction_error_stats 的真实计算结果，保留匹配窗口、时间轴和单位；不能将已有预测查询描述为新模型推理。
5. 页摘要只代表已读取页，不能当作完整航班；保留分页 next_start。空结果、超限和失败如实说明。
6. 综合质量时显式加载 scat-data-quality，术语解释加载 aviation-knowledge。只依据实际结果描述异常和差异，不凭轨迹点生成未计算的指标。
7. 用户要求二维轨迹图时，读取 get_scat_track_points，保留 position，按需要同时包含 motion；页面在本轮“查看来源→查看数据”提供标准二维轨迹及高度/速度曲线，可打开轨迹交互。无需自建绘图脚本，不猜测未发布文件。必须说明这是本次分页范围；只有确认 has_more=false 且 start=0 才可称完整窗口，降采样需说明 every_nth。没有点明细的 page_summary 不能声称已绘图。
8. 收到 skill_required/skill_operation_not_allowed 表示该调用未执行，按返回 recovery 或目录 allowed_operations 加载适用正文后重试原查询。不可去掉权限校验或借用其他run回执。
9. 用户问整批CFPL计划与实飞的汇总或最差样本时，使用 get_prediction_metric_summaries 或 get_worst_prediction_cases。它们读取该批次已有物化结果，按offset/limit和next_offset翻页，不生成新预测、不写物化。保留scat-prediction-metrics-v2协议、六档时域、样本/航班覆盖和来源；不能混为GBAR/M6指标，也不能声称所有原始航班都有可比CFPL。过期结果需明确准备刷新任务，空结果不换批次。

输出说明来源、航班、时间范围、单位及限制；合成结果标为合成。
