---
name: scat-data-quality
description: 检查指定 SCAT 批次的缺失、越界与跳变，分页列出问题或合格航班，检查单航班模型输入质量；不执行模型推理。
---

# SCAT 数据质量调查

1. 使用用户指定或查询工具实际返回的 batch。批次、航班标识不足时加载 scat-flight-query 进行发现，禁止猜造。
2. 批次质量概览使用 inspect_scat_collection_data_quality；问题/合格航班用对应分页列表，保留筛选策略、offset 和 limit。禁止以 batch=null 发起全库扫描。
3. 仅在需要模型窗口适用性时调用 inspect_scat_model_data_quality。model_ready、质量策略与异常位置来自实际结果；数据不合格也应如实报告。
4. 所有缺失率、样本数量与质量结论以工具结果为依据。需要额外数值而工具未提供时说明缺口，不由 LLM 手算伪造指标。
5. 检查失败、无数据、分页或部分结果；未检查的航班不推断为合格。只读质量入口不修改 Redis 或数据库缓存。
6. 需要轨迹定位时加载 scat-trajectory-analysis；解释术语时加载 aviation-knowledge，并分别标出数据证据与知识出处。

不清洗原始数据，不创建模型或物化任务。合成测试结果必须标为合成。
