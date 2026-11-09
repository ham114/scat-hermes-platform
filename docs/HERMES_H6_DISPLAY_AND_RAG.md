# H6 展示开发与 RAG 候选

2026-10-27 当前状态：H6后端报告/业务质量收尾已完成，1289 passed/1 skipped；0.6B已正式接入，文件名缺陷和深度委托丢意图已修复，新版真实回归通过。见[H6后端质量验收](HERMES_H6_BACKEND_QUALITY.md)及[正式RAG与新40题](HERMES_RAG_PRODUCTION_AND_TEST40_20261026.md)。前端H6-B继续暂停，独立人工RAG准入单列；以下旧候选/未接入等结论保留为历史阶段记录。

2026-10-26审核后40题检索阶段完成：开发版本ragas-user-reviewed-dev40-v1固定116证据组，题文不改；原RRF→4B证据组R@5 0.925→1.000，完整证据@5 36/40→40/40，4改善/0退步。代价为合计p50 26.72ms→5316.05ms、p95 5944.31ms，时延门槛仍失败。59项相关测试与40题独立复算通过；问答/多Agent未开始、外部API0、不切8091，H6-C继续研究而非准入。新标签发现原跨来源类别012/015/016单块已足够，报告保留这一限制。详细配置、标签校正、样式检查豁免和收尾见[审核后检索验证](HERMES_REVIEWED_RAG_RETRIEVAL_VALIDATION.md)。

2026-10-25固定比较结果：149块/40题中文开发诊断中，Top100+4B使group R@5/R@10从0.95/0.95升至1.0/1.0（2题改善、0退步），4000对无截断；离线总耗时p50却从25.37ms增至4980.61ms，p95达5280.64ms，旧速度预算不通过。47项相关测试及清理复核通过，不替换8091；本轮不是独立泛化/准入。完整配置、数据版本纠偏、指标口径与后续见[4B固定对比报告](HERMES_RAG_QWEN3_COMPARISON.md)。

2026-10-25比较开发前纪律：仅已有Anaconda的scat-rag-train跑GPU、scat-agent做契约测试，本机scat-frontend/base检查传输；禁venv/uv/新增环境/依赖安装。比较离线，原六服务及H6预留18130–18133不变，无新监听。暂存仅两端项目.tmp/qwen3-compare，正式逐题记录留服务器var/validation，本机只同步不含题目/证据正文的汇总。固定输入SHA与参数后单次开发比较；不读或重跑已消耗验证/holdout，不训练、不外发、不切换8091。

2026-10-25候选下载补充：用户允许在已有 `/mnt/h3c/scat-rag` 下载并优先4B。现有scat-rag-train的CUDA检测V100 32GB约28GiB空闲，可先FP16短输入小批量验证；Qwen3-Reranker-4B与Qwen3-Embedding-4B分别加载，尚无延迟/质量准入结论。正式权重存RAG/models对应分类，暂存仅项目.tmp/qwen3-download并结束清理；不新增环境/依赖，不改驱动，不启动或切换任何服务。官方版本与逐文件SHA固定，具体清单见[下载清单](../services/rag/qwen3-4b-download.json)。NVML版本不匹配与临时DNS失败单独记录，不能把CUDA探测成功当成生产GPU运维验收。

开发前纪律与地址（2026-10-25）：用户明确恢复前端并优先H6。Python仅已有Anaconda：服务器scat-agent/scat-hermes/scat-rag-train，本机scat-frontend检查、base Paramiko传输；禁止venv/uv/新环境。服务器无Node，前端用本机已有Node24/npm11。预留回环18130平台、18131主Hermes、18132后台、18133候选RAG，启动前复查；本机4173浏览器测试、5173按需展示。临时目录两端项目.tmp/h6a，日志var/validation；原六服务、RAG8091、生产GPU不改。真实语料不外发，不训练或下载模型权重。

## 实施顺序

1. H6-A：复用AnalysisResult七类数据契约，通用渲染指标/表格/时序/二维轨迹/分类/预测/动作链接；接入现有任务artifact读取，拒绝跨会话过期响应和危险链接。覆盖空结果、单位、快照/轨迹分组及桌面/移动界面。
2. H6-B：持久展示描述与自然语言改布局、技能关系/版本/参数套用、历史及刷新恢复；只修改展示时复用原结果，分析范围改变仍重新准备/确认。旧地图底图、播放、三维跳转和专业交互逐项做同数据对照。
3. H6-C：RAG候选实现与隔离评测，明确知识来源/版本/定位/证据覆盖，文档检索与确定性事实查询分别路由；候选通过独立门槛后才考虑替换旧对照。

H6-A不代表整个H6完成。原生delegate_task工具仍未加入业务角色，不以通用子Agent绕过权限、run绑定和共享预算。

## H6-A 已交付与验证

新增七类基础渲染器与任务“查看分析结果”，入口仍为 `/assistant`。通过既有artifact GET读取，不提交/重复计算；核对artifact/run/摘要元数据，切线程后旧响应不显示。缺失值显示“未提供”，时序保留断点与孤立点，分类数量与置信度分开，预测保留上下界；轨迹快照/路径/分段分开，来源、UTC起始时间和单位可见。危险链接禁用，文本经Vue转义。

客户端显示预算2MB；轨迹最多5000点，数值表仅前200行并标明总数，完整JSON下载保留。当前不接收任意JS/HTML/ECharts配置。原地图底图/播放/三维和辅助颜色尺寸图例、消息工具收据关联、多轮布局编辑、刷新自动恢复已选展示及完整历史继续H6-B；当前刷新后可重新点击原任务读取制品。

最终前端 **118 passed**，格式/lint/typecheck/build通过，相对97项基线新增21项；桌面/手机 **4项** 通过，无横向溢出，过期制品明确失败，无自动重算，截图已人工核对。服务器 **14 passed/1 skipped**，验证同一合成文件符合AnalysisResult/1.6和真实canonical摘要，复验既有artifact归属/RAG接口。无外部LLM、训练、新权重或检索模型质量测量。

失败保留：默认可选字段类型、控制字符正则lint，以及夹具路由误拦截Vite源码。截图复核后修正孤立点标记和初始动画；原始失败/成功日志分别保留。Cesium大块提示沿用既有情况，未升级依赖。

证据：[服务器协议摘要](validation/20261025-H6A-backend.json)、[桌面合成展示](validation/20261025-H6A-desktop.png)、[手机合成展示](validation/20261025-H6A-mobile.png)。这些展示证据不代表新RAG候选通过准入。

[总验收元数据](validation/20261025-H6A-acceptance.json)及[本机前端检查摘要](validation/20261025-H6A-frontend.json)已同步。服务器扫描1268文本私有值0，55业务执行文件及默认s5.0.0未变，原六服务保持、预留18130–18133关闭；本机4173及已记录测试Node进程结束，两端.tmp/h6a清理。保留依赖与正式dist，原始前端日志只留本机、接口日志只留服务器。

## 旧 RAG 的结论

版本核查补充（2026-10-25）：下述50题confirmation/旧30题dev数字属于历史580块语料，不能直接对比当前8091的149块research-v3。当前服务语料SHA为61788775e03c392b493a7835bba2e5d38d7f8721035b73ccbb1dda441eb53b86；旧dev的53个引用ID在其中0匹配。本轮改用对应149块的research-v3-diagnostic-v1共40题（全中文、8类别）及固定v1.1 claim groups，41个引用块均有效。它明确是开发诊断集，不是独立泛化或生产准入证据；自然confirmation-v2也已有消费记录，本轮不重跑。

依据旧项目docs/RAG_FINETUNE_DECISION_GATE.md及RAG_INDEPENDENT_VALIDATION.md：旧BM25+BGE-M3以及两次Embedding微调试点均未获得准入。后续50题confirmation候选Recall@100=1.0000，但group R@5从冻结RRF的0.7600降到0.7200，排序、类别和延迟门禁失败。即使候选集包含证据，也可能因排序、同节拥挤或证据引用契约而失败。

这支持优先分清候选召回、排序和引用问题；不能将候选覆盖高误称为答案可靠。新方案必须保留旧RRF对照，使用未消耗的独立评测；当前任务已见旧方法结果，不承担clean-room编题，不打开旧冻结题正文、不将旧holdout错误回流训练。

## 替代候选及优先级

| 候选 | 目标 | 本项目处理 |
| --- | --- | --- |
| 上下文分块/父子块 + 有界重排 | 改善脱离章节的碎片、证据拥挤和Top5排序 | 先作为结构/排序消融；保留章节、版本和精确定位。候选集不能为重排而无证据地从100截到15 |
| Qwen3-Embedding + Qwen3-Reranker | 独立替换BGE向量与排序模型 | 资源核查后按用户指示先准备4B，0.6B保留为资源/延迟对照；逐一替换而非同时全换，避免无法归因；下载和合成冒烟不代表质量/延迟准入 |
| GraphRAG | 文档间实体关系、多跳和跨文档归纳 | 适合复杂关联问题的独立分支，保留图边到原文的引用；不是实时航班数或轨迹数值的来源 |
| 多Agent自适应检索 | 子问题分解、多路证据与核验 | 编排层可组合上述检索器；不能修复本身缺失/错误的证据，须与单Agent同预算对照 |

Qwen与GraphRAG仅是候选，没有本项目通过准入的结论。BM25可作为精确术语/编号通路继续参与对照。上下文补充若由LLM生成，必须记录生成来源与原文边界，不能把生成文字当成独立权威证据。

### 4B权重准备与复用

官方固定清单为 `services/rag/qwen3-4b-download.json`：重排17文件/8,059,546,924字节，向量14文件/8,059,520,478字节，合计16,119,067,402字节。分别使用ModelScope提交 `14f36843fc02cea94c1e0c59b6ae0ab1f9b9ebe2` 与 `959bed3521cc1e8714392c77e15ae213b00aa921`；LFS权重及tokenizer与清单内对应HF完整版本交叉核对SHA256，所有文件逐项验证。

下载脚本只在已有 `/home/h3c/anaconda3/envs/scat-rag-train/bin/python` 下运行 `scripts/download_qwen3_rag_candidates.py`，无Key、无安装、无服务启动。暂存只放本项目 `.tmp/qwen3-download`；下载完成校验后复制正式路径、fsync及复读验证。已有目的文件不匹配时拒绝覆盖。`--verify-only` 可离线重验完整文件。临时DNS回退为显式项目内JSON输入，有效期两小时，仅影响该进程两个官方域名，HTTPS域名与证书验证保留；正常网络不需要此选项，不应长期固定CDN IP。

`scripts/validate_qwen3_rag_candidates.py` 用两个独立进程依次加载模型，FP16/SDPA、短输入不超过512 token，三条公开常识合成问题分别比较正负文档；所有加载均local_files_only且关闭HF联网/远程代码。记录实际分数、加载时间、少量forward耗时与显存峰值，不测量真实语料召回、Top5、服务p50/p95或并发能力，不能作为旧门槛的替代。

实际离线冒烟两模型均通过：重排PyTorch峰值保留显存约7.75GiB，向量约7.64GiB，向量输出2560维。详见[合成冒烟元数据](validation/20261025-Qwen3-4B-smoke.json)与[最终完整性及清理验收](validation/20261025-Qwen3-4B-acceptance.json)。首轮一个重排分片出现复制后复读摘要不一致，已拒绝就绪并保留失败，受保护删除本轮损坏分片后重下；不能据此确认底层根因，复核最终文件才可使用。下一步仍先固定原语料/候选召回，只换4B重排作隔离消融，再考虑4B向量及独立索引；本轮没有建立新索引或替换原服务。

## Hermes 多智能体 RAG

2026-10-25已完成[原生四Agent合成技术试验](HERMES_MULTI_AGENT_RAG_DEMO.md)：主会话原生并行委托规则/样本检索，再委托独立核验，28.39秒、14次模型/14次工具准入，29项相关测试通过。仅隔离合成资料与同步结果交付适配；正式角色及8091未接入该能力，质量/时延/单多Agent对照和独立评测仍未准入。原始回答一处型号拼写错误已记录。

当前固定Hermes提交5b147ab036d6d19131bff8d20701b5001eab844a的tools/delegate_tool.py及辅助模块提供真实子Agent上下文和委托执行；官方委托文档也说明该能力。项目当前只开放已审核业务工具，S4/S5的可信后台子Agent主要执行批准CPU工作流；业务多Agent RAG尚未接入真实语料或正式验收，合成技术试验与生产接入分别记录。

建议受控链路：主Agent按复杂度路由 → 文档检索子Agent/确定性数据查询子Agent → 证据核验子Agent → 主Agent按可核查出处回答；证据不足返回不足。检索子Agent按需要读取词面、向量或图检索服务，所有结果带source_id/source_version/locator。核验侧必须实际读取原引用，不接受另一子Agent的总结作为证据。

首版最多两个并行分支、最多两轮补查，超限停止；这些是计划中的有界配置，尚未上线。继承用户/线程/run，工具权限只能缩小，主子累计token/工具/时间预算，跨scope引用拒绝。原生通用delegate_task不直接加入业务白名单；须先验证其子Agent工具注册、预算及会话绑定均通过项目guard。不能默认依赖新版本插件或升级固定核心。

## 评测与准入

本轮比较预注册见[固定参数](../services/rag/qwen3-comparison-v1.json)：原BM25+BGE/RRF完整顺序的前100个候选，对照原排序与4B纯yes/no分数重排；同候选、同语料、同标签，原召回源码哈希及逐题前10名一致性必须通过。FP16/SDPA、batch4、总输入2048token、稳定同分顺序，保留官方提示前后缀，记录实际截断；只保留末token logits的优化先与完整logits做合成数值一致性校验。每题共享一次实际测量的原召回，报告原召回耗时、完整重排耗时及两者之和，私有逐题材料留服务器。40对时延样本不含HTTP/排队且不构成线上SLA；诊断门槛仅参考旧预算，不改低门槛，不用已消耗题选参。

保留旧标准作为对照：group R@5/R@10≥0.90/0.95、候选Recall@100≥0.98、相对基线收益、回退题数和类别下限，以及原p50/p95延迟要求。不得为了新模型放宽后宣称通过。多Agent总耗时与单检索调用耗时分别报告；旧75/200ms门槛不能套到含多次LLM的端到端时间，也不能静默取消检索服务门槛。

固定语料/解析/切块版本后，分别比较：原RRF；仅换重排；仅换Embedding；上下文/父子块；最后在相同预算下对比单Agent与受控多Agent。按路由分开计分，确定性事实、无答案与注入拒答不混为Embedding失败。记录候选集覆盖、group Recall、MRR/NDCG、引用支持率、拒答、延迟/显存与调用预算。新holdout只能按既有clean-room和隔离审核流程建设，当前不复用已消耗数据。

## 主要来源

- [Hermes子Agent委托](https://hermes-agent.nousresearch.com/docs/user-guide/features/delegation)
- [Qwen3-Embedding与Reranker官方仓库](https://github.com/QwenLM/Qwen3-Embedding)
- [Anthropic Contextual Retrieval](https://www.anthropic.com/research/contextual-retrieval)
- [Microsoft GraphRAG](https://microsoft.github.io/graphrag/)
