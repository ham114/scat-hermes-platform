# 第二批40题：Ragas原生生成与自动校验

人工审核状态仍为pending；自动抽检结果不冒称独立正式准入。

## LIVE-20261026-001

题目：空管系统航路快照是啥？

参考答案：空管系统航路快照指 TopSky `predicted_trajectory`，在报告中应称为“空管系统航路快照”或“CFPL 参考”，不能写成 GBAR 等机器学习模型输出。

核对点：空管系统航路快照指 TopSky `predicted_trajectory`。
- trajectory-evaluation-curated：TopSky `predicted_trajectory` 在报告中应称为“空管系统航路快照”或“CFPL 参考”
核对点：在报告中应称为“空管系统航路快照”或“CFPL 参考”。
- trajectory-evaluation-curated：TopSky `predicted_trajectory` 在报告中应称为“空管系统航路快照”或“CFPL 参考”
核对点：不能写成 GBAR 等机器学习模型输出。
- trajectory-evaluation-curated：不能写成 GBAR 等机器学习模型输出。

## LIVE-20261026-002

题目：根据 SCAT 科研解释指南，论文描述的 13 周公开数据在平台导入时可能涉及哪些处理？这些处理应如何披露？

参考答案：论文描述的是 13 周公开数据。平台可能只导入其中某些批次，或为特定模型范围生成研究子集。任何子集裁剪、质量修复和派生标签都必须作为新的研究条件披露，不能写成 SCAT 官方规则。

核对点：论文描述的是 13 周公开数据。
- scat-research-guide：论文描述的是 13 周公开数据。
核对点：平台可能只导入其中某些批次，或为特定模型范围生成研究子集。
- scat-research-guide：平台可能只导入其中某些批次，或为特定模型范围生成研究子集。
核对点：任何子集裁剪、质量修复和派生标签都必须作为新的研究条件披露，不能写成 SCAT 官方规则。
- scat-research-guide：任何子集裁剪、质量修复和派生标签都必须作为新的研究条件披露，不能写成 SCAT 官方规则。

## LIVE-20261026-003

题目：GBAR 里 MFHA 是干啥的？它那个注意力分数怎么算？还有分桶边界是啥？

参考答案：MFHA（Multi-Horizon Future Heading Alignment）只存在于行为嵌入预训练中。对未来锚点集合 A={h_1,...,h_K}，由最后观测点到每个未来锚点的局部 ENU 向量构造水平有符号航向偏差 Delta_psi_k 和垂直剖面偏差 Delta_theta_k。每个隐藏状态 H_t 与终端隐藏状态 H_T 计算注意力分数：e_t = W2_att tanh(W1_att [H_t; H_T] + b1_att) + b2_att，alpha_t = exp(e_t) / sum_tau exp(e_tau)，h_ctx = sum_t alpha_t H_t。终端状态和注意力上下文拼接后形成共享表征：z_agg = GELU(LN(W_agg [H_T; h_ctx] + b_agg))，z_MFHA = LN(W_sh z_agg + b_sh)。每个未来锚点分别有水平和垂直分类头。论文使用 5 个锚点 {1,9,18,27,36}；水平分桶边界为 {-15°,-1°,1°,15°}，垂直分桶边界为 {-3°,-0.5°,0.5°,3°}。共享宽度为 128，辅助 dropout 为 0.1。行为分类和两个 MFHA 交叉熵联合优化。完成预训练后删除 MFHA 分支，保留并冻结共享 Conv1D-Transformer 编码器和行为识别头。

核对点：MFHA（Multi-Horizon Future Heading Alignment）只存在于行为嵌入预训练中。
- gbar-method-curated：Multi-Horizon Future Heading Alignment（MFHA）只存在于行为嵌入预训练。
核对点：对未来锚点集合 A={h_1,...,h_K}，由最后观测点到每个未来锚点的局部 ENU 向量构造水平有符号航向偏差 Delta_psi_k 和垂直剖面偏差 Delta_theta_k。
- gbar-method-curated：对未来锚点集合 `A={h_1,...,h_K}`，由最后观测点到每个未来锚点的局部 ENU 向量构造：

- 水平有符号航向偏差 `Delta_psi_k`；
- 垂直剖面偏差 `Delta_theta_k`。
核对点：每个隐藏状态 H_t 与终端隐藏状态 H_T 计算注意力分数：e_t = W2_att tanh(W1_att [H_t; H_T] + b1_att) + b2_att，alpha_t = exp(e_t) / sum_tau exp(e_tau)，h_ctx = sum_t alpha_t H_t。
- gbar-method-curated：每个隐藏状态 `H_t` 与终端隐藏状态 `H_T` 计算注意力分数：

```text
e_t = W2_att tanh(W1_att [H_t; H_T] + b1_att) + b2_att
alpha_t = exp(e_t) / sum_tau exp(e_tau)
h_ctx = sum_t alpha_t H_t
```
核对点：终端状态和注意力上下文拼接后形成共享表征：z_agg = GELU(LN(W_agg [H_T; h_ctx] + b_agg))，z_MFHA = LN(W_sh z_agg + b_sh)。
- gbar-method-curated：终端状态和注意力上下文拼接后形成共享表征：

```text
z_agg = GELU(LN(W_agg [H_T; h_ctx] + b_agg))
z_MFHA = LN(W_sh z_agg + b_sh)
```
核对点：每个未来锚点分别有水平和垂直分类头。
- gbar-method-curated：每个未来锚点分别有水平和垂直分类头。
核对点：论文使用 5 个锚点 {1,9,18,27,36}；水平分桶边界为 {-15°,-1°,1°,15°}，垂直分桶边界为 {-3°,-0.5°,0.5°,3°}。
- gbar-method-curated：论文使用 5 个锚点 `{1,9,18,27,36}`；水平分桶边界为 `{-15°,-1°,1°,15°}`，垂直分桶边界为 `{-3°,-0.5°,0.5°,3°}`。
核对点：共享宽度为 128，辅助 dropout 为 0.1。
- gbar-method-curated：共享宽度为 128，辅助 dropout 为 0.1。
核对点：行为分类和两个 MFHA 交叉熵联合优化。
- gbar-method-curated：行为分类和两个 MFHA 交叉熵联合优化。
核对点：完成预训练后删除 MFHA 分支，保留并冻结共享 Conv1D-Transformer 编码器和行为识别头。
- gbar-method-curated：完成预训练后删除 MFHA 分支，保留并冻结共享 Conv1D-Transformer 编码器和行为识别头。

## LIVE-20261026-004

题目：SCAT 的原始对象和派生结果分别包括哪些内容，派生结果需要携带哪些版本信息？

参考答案：原始 SCAT 对象包括飞行计划、许可、监视航迹、TopSky 航路、空域和天气。阶段标签、轨迹特征、预测误差、模型实验和可视化产物都是项目派生结果。派生结果必须携带数据来源版本、算法或指标版本、模型版本和样本量。

核对点：原始 SCAT 对象包括飞行计划、许可、监视航迹、TopSky 航路、空域和天气。
- scat-research-guide：原始 SCAT 对象包括飞行计划、许可、监视航迹、TopSky 航路、空域和天气。
核对点：阶段标签、轨迹特征、预测误差、模型实验和可视化产物都是项目派生结果。
- scat-research-guide：阶段标签、轨迹特征、预测误差、模型实验和可视化产物都是项目派生结果。
核对点：派生结果必须携带数据来源版本、算法或指标版本、模型版本和样本量。
- scat-research-guide：派生结果必须携带数据来源版本、算法或指标版本、模型版本和样本量。

## LIVE-20261026-005

题目：MFHA 中局部 ENU 向量如何构造？ENU 与 NED 参考系有何区别？

参考答案：在 GBAR 轨迹预测方法的 MFHA（Multi-Horizon Future Heading Alignment）中，局部 ENU 向量由最后观测点到每个未来锚点的向量构造，用于计算水平有符号航向偏差 Delta_psi_k 和垂直剖面偏差 Delta_theta_k。ENU 是东、北、天局部坐标，常用于地面运动和导航速度；NED 是北、东、地局部坐标，第三轴向下为正。同一个三维向量在不同参考系中的分量不能直接比较。

核对点：MFHA 中局部 ENU 向量由最后观测点到每个未来锚点的向量构造，用于计算水平有符号航向偏差 Delta_psi_k 和垂直剖面偏差 Delta_theta_k。
- gbar-method-curated：对未来锚点集合 `A={h_1,...,h_K}`，由最后观测点到每个未来锚点的局部 ENU 向量构造：

- 水平有符号航向偏差 `Delta_psi_k`；
- 垂直剖面偏差 `Delta_theta_k`。
核对点：ENU 是东、北、天局部坐标，常用于地面运动和导航速度。
- aviation-terms-curated：**ENU**：东、北、天局部坐标，常用于地面运动和导航速度；
核对点：NED 是北、东、地局部坐标，第三轴向下为正。
- aviation-terms-curated：**NED**：北、东、地局部坐标，第三轴向下为正；
核对点：同一个三维向量在不同参考系中的分量不能直接比较。
- aviation-terms-curated：同一个三维向量在不同参考系中的分量不能直接比较。

## LIVE-20261026-006

题目：我们航空科研人员核对论文方法时，如果同一问题出现冲突，到底该以哪个文件为准？是不是应该以 GBAR_METHOD.md 为准？还是说要把不同来源的说法平均化？

参考答案：同一问题出现冲突时按问题对象选择来源，而不是把说法平均化。论文方法以 `GBAR_METHOD.md` 为准。

核对点：同一问题出现冲突时按问题对象选择来源，而不是把说法平均化。
- agent-usage-curated：同一问题出现冲突时按问题对象选择来源，而不是把说法平均化：
核对点：论文方法以 `GBAR_METHOD.md` 为准。
- agent-usage-curated：2. 论文方法以 `GBAR_METHOD.md` 为准；

## LIVE-20261026-007

题目：在 SCAT 数据集论文信息与字段表的 Table 9 中，飞行计划更新 fpl_plan_update[] 的字段定义里，copn_pel_unit 和 copx_pel_unit 分别支持哪些取值及含义？同时，icao_route、rfl_string、tas_string 分别对应 ICAO 飞行计划第 15 项的什么内容？

参考答案：在 Table 9：飞行计划更新 fpl_plan_update[] 中，copn_pel_unit 的取值为 A=英尺高度，F=以 100 ft 为单位的 flight level；copx_pel_unit 的取值为 A=英尺高度，F=以 100 ft 为单位的 flight level。icao_route 定义为 ICAO 飞行计划第 15 项格式的当前航路；rfl_string 定义为 ICAO 飞行计划第 15 项格式的请求飞行高度；tas_string 定义为 ICAO 飞行计划第 15 项格式的请求速度。

核对点：copn_pel_unit 的取值为 A=英尺高度，F=以 100 ft 为单位的 flight level
- scat-dataset-curated：| `copn_pel_unit` | string | `A`=英尺高度，`F`=以 100 ft 为单位的 flight level |
核对点：copx_pel_unit 的取值为 A=英尺高度，F=以 100 ft 为单位的 flight level
- scat-dataset-curated：| `copx_pel_unit` | string | `A`=英尺高度，`F`=以 100 ft 为单位的 flight level |
核对点：icao_route 定义为 ICAO 飞行计划第 15 项格式的当前航路
- scat-dataset-curated：| `icao_route` | string | ICAO 飞行计划第 15 项格式的当前航路 |
核对点：rfl_string 定义为 ICAO 飞行计划第 15 项格式的请求飞行高度
- scat-dataset-curated：| `rfl_string` | string | ICAO 飞行计划第 15 项格式的请求飞行高度 |
核对点：tas_string 定义为 ICAO 飞行计划第 15 项格式的请求速度
- scat-dataset-curated：| `tas_string` | string | ICAO 飞行计划第 15 项格式的请求速度 |

## LIVE-20261026-008

题目：SCAT 数据集事实看哪个文件？

参考答案：SCAT 数据集事实以 `SCAT_DATASET.md` 为准。

核对点：SCAT 数据集事实以 `SCAT_DATASET.md` 为准。
- agent-usage-curated：3. SCAT 数据集事实以 `SCAT_DATASET.md` 为准；

## LIVE-20261026-009

题目：平台生成的研究子集在数据裁剪和质量修复后，是否应作为新的研究条件披露，而不能写成 SCAT 官方规则？

参考答案：是的。任何子集裁剪、质量修复和派生标签都必须作为新的研究条件披露，不能写成 SCAT 官方规则。

核对点：平台生成的研究子集在数据裁剪和质量修复后，应作为新的研究条件披露，不能写成 SCAT 官方规则。
- scat-research-guide：任何子集裁剪、质量修复和派生标签都必须作为新的研究条件披露，不能写成 SCAT 官方规则。

## LIVE-20261026-010

题目：在 Table 12 中，center_id 为 1 时对应哪个管制中心标识？

参考答案：center_id 为 1 时对应 ESMM。

核对点：center_id 为 1 时对应 ESMM。
- scat-dataset-curated：| `center_id` | string | 管制中心标识：`1=ESMM`，`2=ESOS` |

## LIVE-20261026-011

题目：在 SCAT 科研解释指南中，预测误差被列为派生结果，并要求携带数据来源版本、算法或指标版本、模型版本和样本量；而航空与轨迹研究术语的数据解释原则又强调计划误差、许可偏差和预测误差是不同研究问题，且 measured、calculated、selected、assigned 和 predicted 表示不同产生过程。那么，当算法升级后，如果要把新旧版本的预测误差数值放在同一统计中，需要同时注意哪些版本与样本量要求，以及如何避免把预测误差与计划误差或许可偏差混为同一研究问题？

参考答案：根据 SCAT 科研解释指南，预测误差属于项目派生结果，派生结果必须携带数据来源版本、算法或指标版本、模型版本和样本量；算法升级后，不应把新旧版本数值放在同一统计中，也不能静默覆盖已用于报告的结果。根据航空与轨迹研究术语的数据解释原则，计划误差、许可偏差和预测误差是不同研究问题，回答时应保留 measured、calculated、selected、assigned 和 predicted 等限定词，缺字段不等于零，参考系、单位、版本或生成过程未知时应明确说明未知。因此，处理算法升级后的预测误差统计时，需要同时核对数据来源版本、算法或指标版本、模型版本和样本量，避免新旧版本数值混入同一统计，并明确区分预测误差与计划误差、许可偏差，保留产生过程的限定词。

核对点：预测误差属于项目派生结果
- scat-research-guide：阶段标签、轨迹特征、预测误差、模型实验和可视化产物都是项目派生结果。
核对点：派生结果必须携带数据来源版本、算法或指标版本、模型版本和样本量
- scat-research-guide：派生结果必须携带数据来源版本、算法或指标版本、模型版本和样本量。
核对点：算法升级后不应把新旧版本数值放在同一统计中，也不能静默覆盖已用于报告的结果
- scat-research-guide：算法升级后，不应把新旧版本数值放在同一统计中，也不能静默覆盖已用于报告的结果。
核对点：计划误差、许可偏差和预测误差是不同研究问题
- aviation-terms-curated：计划误差、许可偏差和预测误差是不同研究问题；
核对点：回答时应保留 measured、calculated、selected、assigned 和 predicted 等限定词
- aviation-terms-curated：`measured`、`calculated`、`selected`、`assigned` 和 `predicted` 表示不同产生过程，回答时保留限定词；
核对点：缺字段不等于零，参考系、单位、版本或生成过程未知时应明确说明未知
- aviation-terms-curated：- 缺字段不等于零；
- 计划误差、许可偏差和预测误差是不同研究问题；
- 参考系、单位、版本或生成过程未知时应明确说明未知。

## LIVE-20261026-012

题目：Adep 字段在 fpl_base[] 里到底指啥？

参考答案：在 SCAT 数据集论文信息与字段表的 Table 5：基础飞行计划 fpl_base[] 中，`Adep` 字段的类型为 string，定义为起飞机场 ICAO 代码。

核对点：在 SCAT 数据集论文信息与字段表的 Table 5：基础飞行计划 fpl_base[] 中，Adep 字段的类型为 string，定义为起飞机场 ICAO 代码。
- scat-dataset-curated：Table 5：基础飞行计划 fpl_base[]

| 字段 | 类型 | 定义 |
|---|---|---|
| `Adar` | string | 与 `Ades` 不同时的实际目的机场 ICAO 代码 |
| `Adep` | string | 起飞机场 ICAO 代码 |

## LIVE-20261026-013

题目：我们项目数据库里那个 source.external_id 到底对应论文里的哪个航班提取编号啊，还有跨系统身份的时候是不是不能只看航班数字编号，得保留数据批次和来源编号对吧？

参考答案：`source.external_id` 对应论文的航班提取编号。跨系统身份不能只依赖航班数字编号；研究时至少保留数据批次和来源编号。

核对点：`source.external_id` 对应论文的航班提取编号。
- scat-research-guide：`id` 或 `source.external_id` 对应论文的航班提取编号；
核对点：跨系统身份不能只依赖航班数字编号；研究时至少保留数据批次和来源编号。
- scat-research-guide：跨系统身份不能只依赖航班数字编号；研究时至少保留数据批次和来源编号。

## LIVE-20261026-014

题目：在轨迹预测评估中，当无法可靠估计局部航迹方向时，沿航向误差和横向误差应如何处理？

参考答案：无法可靠估计局部航迹方向时，沿航向误差和横向误差这两个值为空，不进入对应聚合。

核对点：无法可靠估计局部航迹方向时，沿航向误差和横向误差这两个值为空，不进入对应聚合。
- trajectory-evaluation-curated：无法可靠估计局部航迹方向时，这两个值为空，不进入对应聚合。

## LIVE-20261026-015

题目：在航空科研数据核对中，GBAR 输出与 TopSky 航路快照、监视航迹、飞行计划、管制许可分别表示什么、不表示什么？如何按 SCAT 科研解释指南保持四类信息分开而不清洗成一致？

参考答案：根据 SCAT 科研解释指南，四类信息必须分开：飞行计划表示申报并持续更新的预期飞行信息，不表示实际飞行轨迹；管制许可表示管制员给出的高度、速度或航向等授权，不表示航空器必然已经执行；监视航迹表示 ARTAS 融合后的系统航迹点，不表示单部雷达原始回波；TopSky 航路快照表示某一时刻的计划/预测航路与预计过点时间，不表示 GBAR 输出或未来真值。同一航班的四类信息可以不同步或互相矛盾，这是运行数据的一部分；除非研究问题明确要求，不应把它们清洗成一致。

核对点：飞行计划表示申报并持续更新的预期飞行信息，不表示实际飞行轨迹。
- scat-research-guide：| 飞行计划 | 申报并持续更新的预期飞行信息 | 实际飞行轨迹 |
核对点：管制许可表示管制员给出的高度、速度或航向等授权，不表示航空器必然已经执行。
- scat-research-guide：| 管制许可 | 管制员给出的高度、速度或航向等授权 | 航空器必然已经执行 |
核对点：监视航迹表示 ARTAS 融合后的系统航迹点，不表示单部雷达原始回波。
- scat-research-guide：| 监视航迹 | ARTAS 融合后的系统航迹点 | 单部雷达原始回波 |
核对点：TopSky 航路快照表示某一时刻的计划/预测航路与预计过点时间，不表示 GBAR 输出或未来真值。
- scat-research-guide：| TopSky 航路快照 | 某一时刻的计划/预测航路与预计过点时间 | GBAR 输出或未来真值 |
核对点：同一航班的四类信息可以不同步或互相矛盾，这是运行数据的一部分；除非研究问题明确要求，不应把它们清洗成一致。
- scat-research-guide：同一航班的四类信息可以不同步或互相矛盾，这是运行数据的一部分。除非研究问题明确要求，不应把它们清洗成一致。

## LIVE-20261026-016

题目：在GBAR轨迹预测方法的特征级Geohash-Binary表示中，区间距离差分如何计算时间变化？特别是对覆盖完整周期的角变量，最短有符号环向位移的公式是什么？多变量历史差分与未来差分的维度如何定义？

参考答案：在GBAR轨迹预测方法的特征级Geohash-Binary表示中，时间变化不通过相邻二进制向量相减，而通过量化区间索引相减：d_t(i) = q_(t+1)(i) - q_t(i)。对覆盖完整周期的角变量，使用最短有符号环向位移：d_t(i) = mod(q_(t+1)(i) - q_t(i) + 2^(b_i-1), 2^b_i) - 2^(b_i-1)。多变量历史差分与未来差分分别为：D[1:T-1] ∈ R^((T-1)×C) 和 D[T:T+H-1] ∈ R^(H×C)。它们使用训练集统计量归一化，并直接作为连续回归目标，不再编码成 bit。

核对点：时间变化不通过相邻二进制向量相减，而通过量化区间索引相减：d_t(i) = q_(t+1)(i) - q_t(i)。
- gbar-method-curated：时间变化不通过相邻二进制向量相减，而通过量化区间索引相减：

```text
d_t(i) = q_(t+1)(i) - q_t(i)
```
核对点：对覆盖完整周期的角变量，使用最短有符号环向位移：d_t(i) = mod(q_(t+1)(i) - q_t(i) + 2^(b_i-1), 2^b_i) - 2^(b_i-1)。
- gbar-method-curated：对覆盖完整周期的角变量，使用最短有符号环向位移：

```text
d_t(i) = mod(q_(t+1)(i) - q_t(i) + 2^(b_i-1), 2^b_i) - 2^(b_i-1)
```
核对点：多变量历史差分与未来差分分别为：D[1:T-1] ∈ R^((T-1)×C) 和 D[T:T+H-1] ∈ R^(H×C)。
- gbar-method-curated：多变量历史差分与未来差分分别为：

```text
D[1:T-1] ∈ R^((T-1)×C)
D[T:T+H-1] ∈ R^(H×C)
```
核对点：它们使用训练集统计量归一化，并直接作为连续回归目标，不再编码成 bit。
- gbar-method-curated：它们使用训练集统计量归一化，并直接作为连续回归目标，不再编码成 bit。

## LIVE-20261026-017

题目：在航空与轨迹研究中，字段名中的 forward/lateral/normal 通常与机体或气动轴相关，但具体设备必须以其说明书为准；请问这些分量能否与 ENU 或 NED 参考系中的分量直接比较？另外，角度差应如何处理，以及角度的普通算术平均在跨越 0°/360° 时可能错误时，应采用什么方法？

参考答案：同一个三维向量在不同参考系中的分量不能直接比较。字段名中的 forward/lateral/normal 通常与机体或气动轴相关，但仍需数据来源确认。角度差应折回约定区间，例如 [-180°,180°)；角度的普通算术平均在跨越 0°/360° 时可能错误，应使用圆统计或先展开连续角度。

核对点：同一个三维向量在不同参考系中的分量不能直接比较。
- aviation-terms-curated：同一个三维向量在不同参考系中的分量不能直接比较。
核对点：字段名中的 forward/lateral/normal 通常与机体或气动轴相关，但仍需数据来源确认。
- aviation-terms-curated：字段名中的 `ve/vn/vu` 通常表示东、北、天分量；`forward/lateral/normal` 通常与机体或气动轴相关，但仍需数据来源确认。
核对点：角度差应折回约定区间，例如 [-180°,180°)。
- aviation-terms-curated：角度差应折回约定区间，例如 `[-180°,180°)`；
核对点：角度的普通算术平均在跨越 0°/360° 时可能错误，应使用圆统计或先展开连续角度。
- aviation-terms-curated：角度的普通算术平均在跨越 0°/360° 时可能错误，应使用圆统计或先展开连续角度。

## LIVE-20261026-018

题目：在航空与轨迹研究中，当涉及 UAV 的通用术语与具体数据集事实出现冲突时，根据科研 Agent 知识使用规则，应如何选择来源？

参考答案：根据科研 Agent 知识使用规则，同一问题出现冲突时按问题对象选择来源，而不是把说法平均化。具体到 UAV 相关冲突：通用术语以 `AVIATION_TERMS.md` 为辅助，不覆盖数据集事实；SCAT 数据集事实以 `SCAT_DATASET.md` 为准；平台中的 SCAT/UAV 语义以对应研究指南为准。因此，当 UAV 通用术语与数据集事实冲突时，应优先采用数据集事实（如 SCAT 数据集事实以 `SCAT_DATASET.md` 为准），通用术语仅作辅助，不覆盖数据集事实。

核对点：同一问题出现冲突时按问题对象选择来源，而不是把说法平均化。
- agent-usage-curated：同一问题出现冲突时按问题对象选择来源，而不是把说法平均化：
核对点：SCAT 数据集事实以 `SCAT_DATASET.md` 为准。
- agent-usage-curated：3. SCAT 数据集事实以 `SCAT_DATASET.md` 为准；
核对点：平台中的 SCAT/UAV 语义以对应研究指南为准。
- agent-usage-curated：4. 平台中的 SCAT/UAV 语义以对应研究指南为准；
核对点：通用术语以 `AVIATION_TERMS.md` 为辅助，不覆盖数据集事实。
- agent-usage-curated：5. 通用术语以 `AVIATION_TERMS.md` 为辅助，不覆盖数据集事实；
- aviation-terms-curated：本文只定义跨 SCAT 与 UAV 文档复用的通用术语。具体数据字段和单位以对应数据集文档为准。
核对点：因此 UAV 通用术语与数据集事实冲突时，应优先采用数据集事实，通用术语仅作辅助、不覆盖数据集事实。
- agent-usage-curated：同一问题出现冲突时按问题对象选择来源，而不是把说法平均化：
- agent-usage-curated：3. SCAT 数据集事实以 `SCAT_DATASET.md` 为准；
- agent-usage-curated：5. 通用术语以 `AVIATION_TERMS.md` 为辅助，不覆盖数据集事实；

## LIVE-20261026-019

题目：批次级冻结评估和旧式单航班最近点误差有啥区别？

参考答案：旧式单航班最近点误差是对单航班做较简单的“预测航路点与时间上最近的实飞点”比较，适合快速排查；它与批次级冻结评估的时间对齐、分组和指标集合不同。若用户没有明确要求旧式口径，优先使用批次级评估。

核对点：旧式单航班最近点误差是对单航班做较简单的“预测航路点与时间上最近的实飞点”比较，适合快速排查。
- system-user-guide-curated：系统也能对单航班做较简单的“预测航路点与时间上最近的实飞点”比较。它适合快速排查
核对点：旧式单航班最近点误差与批次级冻结评估的时间对齐、分组和指标集合不同。
- system-user-guide-curated：但与批次级冻结评估的时间对齐、分组和指标集合不同。
核对点：若用户没有明确要求旧式口径，优先使用批次级评估。
- system-user-guide-curated：若用户没有明确要求旧式口径，优先使用批次级评估。

## LIVE-20261026-020

题目：关键结论要引文档名和章节吗？

参考答案：是。每个关键结论尽量紧邻引用，至少给出文档名和章节。

核对点：是。每个关键结论尽量紧邻引用，至少给出文档名和章节。
- agent-usage-curated：每个关键结论尽量紧邻引用，至少给出文档名和章节；

## LIVE-20261026-021

题目：我在核对航空科研系统结果制品时，想确认制品标识到底能用来做什么，以及结果制品通常包含哪些上下文信息，能否按系统对话使用指南说明一下？

参考答案：在航空科研系统对话使用指南中，模型任务和统一比较可能生成持久化结果制品，用户可以通过运行标识或制品标识继续访问，例如打开 artifact_id 为……的结果摘要。结果制品通常包含任务身份、数据范围、模型与评估版本、指标摘要、轨迹数据和可视化入口；解释结果时应优先保留这些上下文，而不是只摘取一个误差数字。

核对点：模型任务和统一比较可能生成持久化结果制品。
- system-user-guide-curated：模型任务和统一比较可能生成持久化结果制品。
核对点：用户可以通过运行标识或制品标识继续访问结果制品。
- system-user-guide-curated：用户可以通过运行标识或制品标识继续访问：
核对点：可以打开 artifact_id 为……的结果摘要。
- system-user-guide-curated：打开 artifact_id 为……的结果摘要。
核对点：结果制品通常包含任务身份、数据范围、模型与评估版本、指标摘要、轨迹数据和可视化入口。
- system-user-guide-curated：结果制品通常包含任务身份、数据范围、模型与评估版本、指标摘要、轨迹数据和可视化入口。
核对点：解释结果时应优先保留这些上下文，而不是只摘取一个误差数字。
- system-user-guide-curated：解释结果时应优先保留这些上下文，而不是只摘取一个误差数字。

## LIVE-20261026-022

题目：作为航空科研人员，我在核对固定翼无人机导航数据时，看到以 cmp_comb_* 为前缀的组合导航解字段，想确认这些字段（如 lon、lat、alt、ve、vn、vu）的单位和限制分别是什么，以及现有资料能否说明外部辅助源如何参与、采用何种滤波、状态维数、权重或失锁切换逻辑？

参考答案：根据固定翼无人机数据科研指南，三套导航结果分别以 cmp_bds_*、cmp_gps_* 和 cmp_comb_* 为前缀。其中 cmp_comb_* 可称为组合导航解。字段后缀含义与单位限制如下：lon、lat 表示经度、纬度，单位为 degree，具体坐标基准实现未确认；alt 表示高度，现有约定为 m，椭球高、海拔高或其他基准未知；ve、vn、vu 表示东、北、天向速度分量，单位为 m/s，正方向按字段名解释。现有资料不能确定外部辅助源如何参与、采用何种滤波、状态维数、权重或失锁切换逻辑。

核对点：三套导航结果分别以 cmp_bds_*、cmp_gps_* 和 cmp_comb_* 为前缀，其中 cmp_comb_* 可称为组合导航解。
- uav-research-guide：三套导航结果分别以 `cmp_bds_*`、`cmp_gps_*` 和 `cmp_comb_*` 为前缀
核对点：lon、lat 表示经度、纬度，单位为 degree，具体坐标基准实现未确认。
- uav-research-guide：| `lon`, `lat` | 经度、纬度 | degree；具体坐标基准实现未确认 |
核对点：alt 表示高度，现有约定为 m，椭球高、海拔高或其他基准未知。
- uav-research-guide：| `alt` | 高度 | 现有约定为 m；椭球高、海拔高或其他基准未知 |
核对点：ve、vn、vu 表示东、北、天向速度分量，单位为 m/s，正方向按字段名解释。
- uav-research-guide：| `ve`, `vn`, `vu` | 东、北、天向速度分量 | m/s；正方向按字段名解释 |
核对点：现有资料不能确定外部辅助源如何参与、采用何种滤波、状态维数、权重或失锁切换逻辑。
- uav-research-guide：`cmp_comb_*` 可称为组合导航解，但现有资料不能确定外部辅助源如何参与、采用何种滤波、状态维数、权重或失锁切换逻辑。

## LIVE-20261026-023

题目：我在核对轨迹预测评估指标时，看到单变量 MAE 与 RMSE 的公式，但 RMSE 的拼写和计算口径容易混淆。请问对于任一标量变量，RMSE 的具体计算公式是什么？它的单位与原变量是否一致？当经度、纬度、高度或速度存在不同缺失模式时，应如何分别处理有效集合和分母？

参考答案：对任一标量变量，误差 `e_i=y_hat_i-y_i`，RMSE 的计算公式为：`RMSE = sqrt(sum(e_i², i in V) / N)`。RMSE 的单位与原变量相同。每个目标变量独立形成有效集合和分母；经度、纬度、高度或速度有不同缺失模式时，必须分别报告 `sample_count`。

核对点：对任一标量变量，误差 e_i=y_hat_i-y_i，RMSE 的计算公式为 RMSE = sqrt(sum(e_i², i in V) / N)。
- trajectory-evaluation-curated：对任一标量变量，误差 `e_i=y_hat_i-y_i`：

```text
MAE  = sum(|e_i|, i in V) / N
RMSE = sqrt(sum(e_i², i in V) / N)
```
核对点：RMSE 的单位与原变量相同。
- trajectory-evaluation-curated：二者单位都与原变量相同。
核对点：每个目标变量独立形成有效集合和分母；经度、纬度、高度或速度有不同缺失模式时，必须分别报告 sample_count。
- trajectory-evaluation-curated：每个目标变量独立形成有效集合和分母；经度、纬度、高度或速度有不同缺失模式时，必须分别报告 `sample_count`。

## LIVE-20261026-025

题目：在评估轨迹预测时，如何区分模型预测与CFPL参考？并且，旧式单航班最近点误差检查与批次级CFPL指标为什么不能直接混用？

参考答案：评估前必须区分模型预测与真实后续轨迹、TopSky/CFPL航路快照与真实后续轨迹，以及不同模型在同一输入、同一截止时间和同一未来时间轴上的预测。TopSky predicted_trajectory 在报告中应称为“空管系统航路快照”或“CFPL 参考”，不能写成 GBAR 等机器学习模型输出。旧式单航班最近点误差检查适合快速排查，但与批次级冻结评估的时间对齐、分组和指标集合不同，因此不能直接混用；若用户没有明确要求旧式口径，优先使用批次级评估。

核对点：评估前必须区分模型预测与真实后续轨迹、TopSky/CFPL航路快照与真实后续轨迹，以及不同模型在同一输入、同一截止时间和同一未来时间轴上的预测。
- trajectory-evaluation-curated：评估前必须区分：

- 模型预测与真实后续轨迹；
- TopSky/CFPL 航路快照与真实后续轨迹；
- 不同模型在同一输入、同一截止时间和同一未来时间轴上的预测。
核对点：TopSky predicted_trajectory 在报告中应称为“空管系统航路快照”或“CFPL 参考”，不能写成 GBAR 等机器学习模型输出。
- trajectory-evaluation-curated：TopSky `predicted_trajectory` 在报告中应称为“空管系统航路快照”或“CFPL 参考”，不能写成 GBAR 等机器学习模型输出。
核对点：旧式单航班最近点误差检查适合快速排查，但与批次级冻结评估的时间对齐、分组和指标集合不同，因此不能直接混用。
- system-user-guide-curated：它适合快速排查，但与批次级冻结评估的时间对齐、分组和指标集合不同。
核对点：若用户没有明确要求旧式口径，优先使用批次级评估。
- system-user-guide-curated：若用户没有明确要求旧式口径，优先使用批次级评估。

## LIVE-20261026-026

题目：在固定翼无人机数据与SCAT监视数据中，水平速度的m/s口径分别如何计算？若固定翼数据中的cmp_ground_speed需换算为m/s，其依据是什么？两种来源在速度计算与统计时对缺失值和样本条件有何不同要求？

参考答案：固定翼无人机数据中，水平速度按 horizontal_speed_mps = sqrt(cmp_comb_ve² + cmp_comb_vn²) 计算，单位 m/s；ground_speed_mps = cmp_ground_speed / 3.6，该换算建立在“原始 cmp_ground_speed 高置信为 km/h”的 B 级证据上，回答时必须保留该限定。原始值、换算值和由位置差分得到的地速是三种不同来源，不能混用。SCAT 科研解释指南中，水平地速是 sqrt(vx²+vy²)，单位 m/s；一段时间的平均地速是有效地速样本的算术平均，不能把缺失值当零。SCAT 计算只使用时间可解析且经纬度有效的监视点，并按时间排序；时长、航程、平均地速和覆盖率必须同时说明筛选条件、批次、时间范围和有效样本数。

核对点：固定翼无人机数据中，水平速度按 horizontal_speed_mps = sqrt(cmp_comb_ve² + cmp_comb_vn²) 计算，单位 m/s
- uav-research-guide：horizontal_speed_mps = sqrt(cmp_comb_ve² + cmp_comb_vn²)
核对点：ground_speed_mps = cmp_ground_speed / 3.6，该换算建立在“原始 cmp_ground_speed 高置信为 km/h”的 B 级证据上，回答时必须保留该限定
- uav-research-guide：ground_speed_mps     = cmp_ground_speed / 3.6
```

最后一个换算建立在“原始 `cmp_ground_speed` 高置信为 km/h”的 B 级证据上，回答时必须保留该限定。
核对点：原始值、换算值和由位置差分得到的地速是三种不同来源，不能混用
- uav-research-guide：原始值、换算值和由位置差分得到的地速是三种不同来源，不能混用。
核对点：SCAT 科研解释指南中，水平地速是 sqrt(vx²+vy²)，单位 m/s
- scat-research-guide：水平地速是 `sqrt(vx²+vy²)`，单位 m/s；
核对点：一段时间的平均地速是有效地速样本的算术平均，不能把缺失值当零
- scat-research-guide：一段时间的平均地速是有效地速样本的算术平均，不能把缺失值当零；
核对点：SCAT 计算只使用时间可解析且经纬度有效的监视点，并按时间排序
- scat-research-guide：以下计算只使用时间可解析且经纬度有效的监视点，并按时间排序：
核对点：时长、航程、平均地速和覆盖率必须同时说明筛选条件、批次、时间范围和有效样本数
- scat-research-guide：时长、航程、平均地速和覆盖率必须同时说明筛选条件、批次、时间范围和有效样本数。

## LIVE-20261026-027

题目：GBAR 轨迹预测方法的主论文标题是什么？

参考答案：主论文标题是 A Pretrained Behavior-Embedded Framework for Flight Trajectory Prediction via Geohash-Binary Encoding。

核对点：GBAR 轨迹预测方法的主论文标题是 A Pretrained Behavior-Embedded Framework for Flight Trajectory Prediction via Geohash-Binary Encoding。
- gbar-method-curated：来源：用户提供的主论文 *A Pretrained Behavior-Embedded Framework for Flight Trajectory Prediction via Geohash-Binary Encoding*

## LIVE-20261026-028

题目：我在核对航空科研系统对话流程时，看到示例里提到“从干净航班中随机选 8 个，准备 GBAR 预测”，这个“干净航班”到底指什么？是不是指质量检查通过的航班？能不能结合示例说明一下从质量检查到 GBAR 的完整步骤？

参考答案：根据提供的上下文，“干净航班”出现在航空科研系统对话使用指南的常见多轮对话示例中。示例流程为：用户先要求检查这个批次的 GBAR 六特征质量；然后用户说“从干净航班中随机选 8 个，准备 GBAR 预测”；系统展示选中航班、模型和任务规模，并请求确认；用户确认提交；用户稍后查询刚才的 run_id，并展示预测轨迹和评估摘要。上下文没有直接定义“干净航班”的含义，但结合示例顺序，它是在质量检查之后用于 GBAR 预测准备的航班集合。

核对点：“干净航班”出现在航空科研系统对话使用指南的常见多轮对话示例中。
- system-user-guide-curated：航空科研系统对话使用指南 > 16. 常见多轮对话示例 > 16.2 从质量检查到 GBAR
核对点：示例流程为：用户先要求检查这个批次的 GBAR 六特征质量。
- system-user-guide-curated：用户：检查这个批次的 GBAR 六特征质量。
核对点：然后用户说“从干净航班中随机选 8 个，准备 GBAR 预测”。
- system-user-guide-curated：用户：从干净航班中随机选 8 个，准备 GBAR 预测。
核对点：系统展示选中航班、模型和任务规模，并请求确认。
- system-user-guide-curated：系统：展示选中航班、模型和任务规模，并请求确认。
核对点：用户确认提交。
- system-user-guide-curated：用户：确认提交。
核对点：用户稍后查询刚才的 run_id，并展示预测轨迹和评估摘要。
- system-user-guide-curated：用户稍后：查询刚才的 run_id，并展示预测轨迹和评估摘要。
核对点：上下文没有直接定义“干净航班”的含义，但结合示例顺序，它是在质量检查之后用于 GBAR 预测准备的航班集合。
- system-user-guide-curated：用户：检查这个批次的 GBAR 六特征质量。
>
> 用户：从干净航班中随机选 8 个，准备 GBAR 预测。

## LIVE-20261026-029

题目：在SCAT数据集论文信息与字段表的Table 11中，TopSky轨迹预测对象的predicted_trajectory元素包含哪些字段？

参考答案：每个predicted_trajectory元素含快照时间time_stamp和航路点数组route[]。

核对点：每个predicted_trajectory元素含快照时间time_stamp和航路点数组route[]。
- scat-dataset-curated：每个 `predicted_trajectory` 元素含快照时间 `time_stamp` 和航路点数组 `route[]`。

## LIVE-20261026-030

题目：GBAR 轨迹预测中，输入张量 X[1:T] 的维度含义是什么？

参考答案：X[1:T] ∈ R^(T×C)，其中 T 是观测步数，C 是状态变量数。

核对点：X[1:T] ∈ R^(T×C)，其中 T 是观测步数，C 是状态变量数。
- gbar-method-curated：X[1:T] ∈ R^(T×C)
```

其中 `T` 是观测步数，`C` 是状态变量数。

## LIVE-20261026-031

题目：那个 run_id 是啥？我咋用它查 GBAR 任务状态和结果啊？

参考答案：run_id 是 GBAR 轨迹预测任务的运行标识。提交任务后，可以说“查询 run_id 为……的 GBAR 任务状态”来查询该任务状态；也可以要求“显示这个任务的预测轨迹、评估摘要和可用图表”。成功任务通常会产生结果制品 artifact_id，可继续要求读取摘要、二维图或三维轨迹。若任务尚未开始，也可以请求取消；已经完成的任务不能按“未运行任务”处理。

核对点：run_id 是 GBAR 轨迹预测任务的运行标识。
- system-user-guide-curated：查询 run_id 为……的 GBAR 任务状态。
核对点：提交任务后，可以说“查询 run_id 为……的 GBAR 任务状态”来查询该任务状态。
- system-user-guide-curated：稍后可以说：

> 查询 run_id 为……的 GBAR 任务状态。
核对点：也可以要求“显示这个任务的预测轨迹、评估摘要和可用图表”。
- system-user-guide-curated：> 显示这个任务的预测轨迹、评估摘要和可用图表。
核对点：成功任务通常会产生结果制品 artifact_id，可继续要求读取摘要、二维图或三维轨迹。
- system-user-guide-curated：成功任务通常会产生结果制品 `artifact_id`，可继续要求读取摘要、二维图或三维轨迹。
核对点：若任务尚未开始，也可以请求取消；已经完成的任务不能按“未运行任务”处理。
- system-user-guide-curated：若任务尚未开始，也可以请求取消；已经完成的任务不能按“未运行任务”处理。

## LIVE-20261026-032

题目：航空科研中，仅凭轨迹或传感器数据能否确认异常根因？设备手册在解释异常时起什么作用？

参考答案：仅凭轨迹或传感器数据通常不能确认根因。因果结论需要设备手册、运行记录、维护记录或实验设计等额外证据。

核对点：仅凭轨迹或传感器数据通常不能确认异常根因。
- system-user-guide-curated：系统可以定位异常样本、描述字段变化和比较群体统计，但仅凭轨迹或传感器数据通常不能确认根因。
核对点：因果结论需要设备手册、运行记录、维护记录或实验设计等额外证据。
- system-user-guide-curated：因果结论需要设备手册、运行记录、维护记录或实验设计等额外证据。

## LIVE-20261026-033

题目：在GBAR轨迹预测方法与模型结构中，Behavior-Aware Embedding（行为嵌入模块）具体包含哪些组件？

参考答案：行为嵌入模块包含共享行为编码器、行为识别头和训练期MFHA辅助头。

核对点：行为嵌入模块包含共享行为编码器、行为识别头和训练期MFHA辅助头。
- gbar-method-curated：行为嵌入模块包含共享行为编码器、行为识别头和训练期 MFHA 辅助头。

## LIVE-20261026-034

题目：approach_clearance 是啥意思？

参考答案：approach_clearance 是布尔类型字段，表示是否已经获准进近。

核对点：approach_clearance 是布尔类型字段
- scat-dataset-curated：| `approach_clearance` | bool | 是否已经获准进近 |
核对点：approach_clearance 表示是否已经获准进近
- scat-dataset-curated：| `approach_clearance` | bool | 是否已经获准进近 |

## LIVE-20261026-035

题目：在 Context-Guided Differential Forecaster 中，历史运动 token 如何与 horizon context 拼接？

参考答案：历史归一化差分经过时间卷积和位置编码得到 P_d[1:T-1] = PE(Conv1D_d(D[1:T-1])) ∈ R^((T-1)×D)，然后将历史运动 token 与 H 个 horizon context 拼接：S^(0) = [P_d[1:T-1]; C[1:H]] ∈ R^((T-1+H)×D)。

核对点：历史归一化差分经过时间卷积和位置编码得到 P_d[1:T-1] = PE(Conv1D_d(D[1:T-1])) ∈ R^((T-1)×D)。
- gbar-method-curated：历史归一化差分经过时间卷积和位置编码：

```text
P_d[1:T-1] = PE(Conv1D_d(D[1:T-1])) ∈ R^((T-1)×D)
```
核对点：将历史运动 token 与 H 个 horizon context 拼接：S^(0) = [P_d[1:T-1]; C[1:H]] ∈ R^((T-1+H)×D)。
- gbar-method-curated：将历史运动 token 与 `H` 个 horizon context 拼接：

```text
S^(0) = [P_d[1:T-1]; C[1:H]] ∈ R^((T-1+H)×D)
```

## LIVE-20261026-036

题目：那个，我想问一下，就是关于 SCAT 数据集论文信息与字段表里 Table 2 的 Flight 顶层对象，里面有个叫 Id 的字段，它到底是啥意思啊？是不是就是提取的时候给航班分配的那个唯一整数编号，然后还跟文件名对应？能不能帮我详细说说这个 Id 字段的定义和用途？

参考答案：在 SCAT 数据集论文信息与字段表 > 3. 航班对象与飞行计划 > Table 2：Flight 顶层对象中，`Id` 字段的类型为 number，定义是“提取时分配的航班唯一整数，与文件名对应”。项目数据库可能把字段规范化为小写；论文原字段的大小写和数据库键不能混用。

核对点：在 SCAT 数据集论文信息与字段表 > 3. 航班对象与飞行计划 > Table 2：Flight 顶层对象中，`Id` 字段的类型为 number。
- scat-dataset-curated：| `Id` | number | 提取时分配的航班唯一整数，与文件名对应 |
核对点：`Id` 字段的定义是“提取时分配的航班唯一整数，与文件名对应”。
- scat-dataset-curated：| `Id` | number | 提取时分配的航班唯一整数，与文件名对应 |
核对点：项目数据库可能把字段规范化为小写；论文原字段的大小写和数据库键不能混用。
- scat-dataset-curated：项目数据库可能把字段规范化为小写；论文原字段的大小写和数据库键不能混用。

## LIVE-20261026-037

题目：在SCAT数据集中，TopSky空中交通管理系统具体涉及哪些数据来源和处理过程？请详细说明监视航迹、飞行计划、空域资料和天气预报分别来自哪里，以及TopSky在记录前可能进行哪些处理。

参考答案：SCAT数据主要来自LFV的TopSky空中交通管理系统。监视航迹来自ARTAS多传感器跟踪系统，输入包括雷达和广域多点定位；飞行计划由飞行员或航空公司提交，并可通过AFTN/AMHS和相邻飞行情报区的OLDI更新；空域资料主要来自航行情报资料，可能为适配TopSky而调整；天气预报源于世界气象组织数据；TopSky可能在记录前处理来自其他系统的数据。因此，Plots是系统融合航迹，不等同于单部雷达原始回波；飞行计划、许可、系统预测和监视真值也不能互相替代。

核对点：SCAT数据主要来自LFV的TopSky空中交通管理系统。
- scat-dataset-curated：数据主要来自 LFV 的 TopSky 空中交通管理系统：
核对点：监视航迹来自ARTAS多传感器跟踪系统，输入包括雷达和广域多点定位。
- scat-dataset-curated：监视航迹来自 ARTAS 多传感器跟踪系统，输入包括雷达和广域多点定位；
核对点：飞行计划由飞行员或航空公司提交，并可通过AFTN/AMHS和相邻飞行情报区的OLDI更新。
- scat-dataset-curated：飞行计划由飞行员或航空公司提交，并可通过 AFTN/AMHS 和相邻飞行情报区的 OLDI 更新；
核对点：空域资料主要来自航行情报资料，可能为适配TopSky而调整。
- scat-dataset-curated：空域资料主要来自航行情报资料，可能为适配 TopSky 而调整；
核对点：天气预报源于世界气象组织数据。
- scat-dataset-curated：天气预报源于世界气象组织数据；
核对点：TopSky可能在记录前处理来自其他系统的数据。
- scat-dataset-curated：TopSky 可能在记录前处理来自其他系统的数据。
核对点：Plots是系统融合航迹，不等同于单部雷达原始回波；飞行计划、许可、系统预测和监视真值也不能互相替代。
- scat-dataset-curated：因此，`Plots` 是系统融合航迹，不等同于单部雷达原始回波；飞行计划、许可、系统预测和监视真值也不能互相替代。

## LIVE-20261026-038

题目：SCAT数据集是瑞典的吗？

参考答案：是的，SCAT 是瑞典飞行情报区区域管制数据集，由 13 个连续一周的数据包组成，各周分布在一年内，共 167,547 个航班文件。

核对点：SCAT 是瑞典飞行情报区区域管制数据集
- scat-dataset-curated：SCAT 是瑞典飞行情报区区域管制数据集
核对点：由 13 个连续一周的数据包组成，各周分布在一年内
- scat-dataset-curated：由 13 个连续一周的数据包组成，各周分布在一年内
核对点：共 167,547 个航班文件
- scat-dataset-curated：共 167,547 个航班文件

## LIVE-20261026-039

题目：那个，我想问一下，就是关于航空科研系统对话使用指南里面说的，如果需要写入任务队列或者重算数据的时候，到底应该按照什么步骤来操作啊？是不是要先准备再确认后提交？能不能详细说说这个流程？

参考答案：根据航空科研系统对话使用指南的使用原则，需要写入任务队列或重算数据时，应先准备、再确认、后提交。

核对点：需要写入任务队列或重算数据时，应先准备、再确认、后提交。
- system-user-guide-curated：需要写入任务队列或重算数据时，先准备、再确认、后提交；

## LIVE-20261026-040

题目：在航空科研系统对话使用指南中，使用原则对数值报告、模型比较和任务提交分别有哪些要求？

参考答案：所有数值尽量附带单位、样本量、时间范围和版本；比较模型时固定样本、截止点、预测时域和对齐规则；需要写入任务队列或重算数据时，先准备、再确认、后提交。

核对点：所有数值尽量附带单位、样本量、时间范围和版本
- system-user-guide-curated：所有数值尽量附带单位、样本量、时间范围和版本；
核对点：比较模型时固定样本、截止点、预测时域和对齐规则
- system-user-guide-curated：比较模型时固定样本、截止点、预测时域和对齐规则；
核对点：需要写入任务队列或重算数据时，先准备、再确认、后提交
- system-user-guide-curated：需要写入任务队列或重算数据时，先准备、再确认、后提交；

## LIVE-20261026-041

题目：在航空科研系统对话使用指南的“从质量检查到 GBAR”示例中，用户完成质量检查并准备 GBAR 预测后，系统返回的 run_id 应如何用于后续查询？

参考答案：用户稍后查询刚才的 run_id，并展示预测轨迹和评估摘要。

核对点：用户稍后查询刚才的 run_id，并展示预测轨迹和评估摘要。
- system-user-guide-curated：用户稍后：查询刚才的 run_id，并展示预测轨迹和评估摘要。
