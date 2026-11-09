---
name: uav-flight-analysis
description: 发现 UAV 机型与架次，读取通道、样本及阶段，比较正常和异常样本的确定性通道统计。
---

# UAV 只读分析

1. 需要时列出机型并 search_uav_flights，使用真实 external_id；SCAT 航班标识不能替代 UAV 架次标识。
2. 先读取架次摘要和 list_uav_channels，确认真实通道键、中文名称、单位和组别。
3. 普通统计优先使用通道/阶段统计；每次最多六个通道。需要时序或轨迹时再读取 get_uav_samples，默认20最多100，遵循 start/every_nth 和下一页信息。
4. 正常与异常对比使用 get_uav_normal_vs_anomaly_stats，明确每组的架次、样本数和机型；异常样本机型可能为 sample，不能声称是同架次配对实验。
5. 时间条件须带时区；空结果、未知通道、单位差异和失败均说明。工具未返回的标准差、预测指标不能编造。
6. 需要航空术语解释时显式加载 aviation-knowledge，引用真实来源。输出保留数据范围与通道单位，合成数据标为合成。

不执行训练、模型预测或数据写入。
