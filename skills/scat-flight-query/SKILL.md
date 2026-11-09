---
name: scat-flight-query
description: 发现 SCAT 批次、按呼号或航班标识检索航班、查询单航班及机场概览；为其他技能提供实际批次和航班标识。
---

# 航班发现与查询

1. 识别用户目标、批次、呼号和数量；缺少必要条件时补问。批次不明确时使用 list_scat_batches，不猜造标识。
2. 读取 references/query-contract.md，按 schema 调用 search_scat_flights；仅在需要时读取数据库、批次、单航班或机场概览。
3. 将实际返回的 source.batch、source.external_id 或工具 query 中的真实标识用于后续操作。空结果时如实说明，不自行换样本。
4. 检查 status、分页、数量和范围；所有统计来自工具。合成数据必须标为合成测试数据。
5. 质量检查显式加载 scat-data-quality；轨迹、飞行计划和已有预测分析显式加载 scat-trajectory-analysis；术语解释加载 aviation-knowledge。按依赖版本使用相同批次/航班。
6. 后续修改查询条件时重新查询；展示调整可以复用仍有效的结果。返回可复核的来源、数量和查询范围。

模型执行、写入及身份更改不在此技能范围。
