# RAG题集与多智能体方案：GitHub复用调研

开发纪律与地址（2026-10-25）：远程项目 `/home/h3c/project/python/scat-hermes-platform`；Python只使用已有Anaconda环境，禁止项目venv、uv或新环境。后续依赖安装须先检查已有环境兼容性。临时文件只能创建在两端项目目录，完成及时删除。本轮仅公开GitHub/官方文档与本机项目文档调研，无安装、模型调用、服务启动或服务器变更。现有80/443/8080/8501/8091/27017与H6预留18130–18133均无变更。

## 结论与核验范围

优先评估Ragas作为离线编题组件，DeepEval作为备选编题/答案与轨迹评测组件；多智能体继续由现有Hermes原生delegate_task承载，参考Langroid的规划—检索—审查循环和MA-RAG的任务分解。此次完成README、依赖声明、关键源码和许可证入口核对，尚未安装或运行第三方项目。“可复用”表示组件候选，不表示兼容或业务效果已验证。实现时必须固定发行版本/commit和依赖快照，不能跟随main自动更新。

## 候选项目

| 项目 | 核对到的能力 | 复用判断与边界 |
| --- | --- | --- |
| [Ragas](https://github.com/vibrantlabsai/ragas) | Apache-2.0；资料构图、场景生成，支持配置单跳/多跳查询分布与LLM/Embedding | 优先复用TestsetGenerator/KnowledgeGraph作离线编题；图只服务题集构建，无需先替换业务检索为GraphRAG。需要保留原始chunk身份并增加中文业务类别、负例与独立证据审核。 |
| [DeepEval](https://github.com/confident-ai/deepeval) | Apache-2.0；Synthesizer可从文档或现成上下文生成goldens，并对问题做复杂度演化；包含答案与Agent轨迹评测 | 若希望先沿用当前分块，可优先试generate_goldens_from_contexts，避免自动重分块。作为Ragas替代或补充评分器，不在首阶段同时装两套。 |
| [AutoRAG legacy](https://github.com/Marker-Inc-Korea/AutoRAG/tree/main/legacy) | Apache-2.0；QA/corpus数据格式、证据映射、模块组合评测；原Python工具已进入维护模式 | 适合借鉴配置化实验矩阵和retrieval_gt映射；暂不引入完整AutoML栈。当前仓库根目录已是另一套2.0产品，不能照旧教程直接使用根目录。 |
| [Langroid](https://github.com/langroid/langroid) | MIT；LanceRAGTaskCreator确实创建规划、检索、审查三个Agent，而非单Agent多次调用 | 最值得参考的工程方案。其Task/Agent/存储不能直接当Hermes插件；建议用Hermes重建角色及结构化反馈协议，不引入第二套编排。 |
| [MA-RAG](https://github.com/thangylvp/MA-RAG) | 与[论文](https://arxiv.org/abs/2505.20096)对应的Planner、Step Definer、Extractor、QA分工，代码使用LangGraph | 参考复杂问题逐步分解、证据摘取及重新规划。检查的根目录未见明确许可证，暂不复制代码/提示词；旧固定依赖也不宜整套安装。论文成绩不代表当前中文业务效果。 |
| [LangGraph Adaptive RAG示例](https://github.com/langchain-ai/langgraph/blob/main/examples/rag/langgraph_adaptive_rag.ipynb) | 查询路由、文档相关性判断、改写重检、回答支持性检查 | 参考控制逻辑；该示例明确已归档，不按当前可直接运行模板推荐。多个图节点并不自动构成多个独立Agent。 |

R2R的Deep Research也进行了初筛，但其定位涉及完整检索后端与研究工具。本轮优先解决编题和Hermes编排，暂不推荐替换现有后端；“Deep Research”命名本身不能作为多Agent证据。

## 具体源码与兼容性依据

- Ragas：[生成指南](https://github.com/vibrantlabsai/ragas/blob/main/docs/getstarted/rag_testset_generation.md)、[生成器源码](https://github.com/vibrantlabsai/ragas/blob/main/src/ragas/testset/synthesizers/generate.py)、[依赖](https://github.com/vibrantlabsai/ragas/blob/main/pyproject.toml)。声明Python>=3.9，但还依赖datasets、LangChain、NetworkX等，声明范围不能证明Python3.14及本项目锁定依赖可用。
- DeepEval：[上下文编题](https://deepeval.com/docs/synthesizer-generate-from-contexts)、[文档编题](https://deepeval.com/docs/synthesizer-generate-from-docs)、[Synthesizer源码](https://github.com/confident-ai/deepeval/blob/main/deepeval/synthesizer/synthesizer.py)、[依赖](https://github.com/confident-ai/deepeval/blob/main/pyproject.toml)。文档路径会增加Chroma与解析/分块依赖；现成contexts入口可以跳过该步骤。源码包含本地JSON/CSV/JSONL导出，不能据早期issue断言必须开通云平台才能编题。
- Langroid：[三Agent构造源码](https://github.com/langroid/langroid/blob/main/langroid/agent/special/lance_rag/lance_rag_task.py)、[示例](https://github.com/langroid/langroid-examples/blob/main/examples/docqa/lance-rag-movies.py)、[依赖](https://github.com/langroid/langroid/blob/main/pyproject.toml)。当前声明Python>=3.10,<3.14，且pandas<3、redis<6、duckduckgo-search<7；与Hermes现用Python3.14及本项目部分依赖声明冲突，不能直接混装。
- MA-RAG：[依赖](https://github.com/thangylvp/MA-RAG/blob/main/requirements.txt)、[执行器](https://github.com/thangylvp/MA-RAG/blob/main/agents/plan_executor.py)。固定LangChain0.3/LangGraph0.6、torch2.5.1、transformers4.50.3等，和现有RAG环境版本不同。
- AutoRAG：[legacy说明](https://github.com/Marker-Inc-Korea/AutoRAG/blob/main/legacy/README.md)、[依赖](https://github.com/Marker-Inc-Korea/AutoRAG/blob/main/legacy/pyproject.toml)、[许可证](https://github.com/Marker-Inc-Korea/AutoRAG/blob/main/legacy/LICENSE)。Python>=3.10,<3.13，不能直接安装进scat-hermes3.14。

后续优先在服务器已有scat-rag-train3.11核查离线编题工具的依赖解析结果，通过文件契约向Hermes交付题集。若解析要求破坏现有torch/transformers等固定版本，就改用兼容发行版或仅参考方案，不新建环境，也不强行升级。生成/评分的LLM可用自定义适配器；不因这些工具就必然需要新OpenAI Key。Qwen3-Embedding/Reranker-4B不具备通用编题/答案生成能力，仍需获准的生成模型。

## 建议的题集构建流程

冻结当前语料/分块/SHA及类别配额 → 从资料关系选择证据包 → 独立上下文生成候选题 → 另一审核上下文回读原文核对答案、实体、证据完整性 → 去重/检查开发题泄漏 → 分配dev与封存holdout → 固定评分器和配置 → 运行单/多Agent配对比较。

第一阶段优先覆盖单文档事实、跨文档条件组合、数值边界、跨版本冲突、无答案/证据不足、业务与闲聊混合、实体标识一致性。复杂题必须有业务上确实需要合并的证据，不能只拼接两个无关问题。无答案样本需审核整个冻结语料的可回答性，不能因随机抽取的上下文没答案就标负例。

统一导出建议包含qid、question、category、difficulty、expected_answer、answerability、expected_claims、evidence_groups、source_id/version/chunk_id、corpus_sha、生成/审核模型与模板身份、split、审核状态。编题模型只能看到资料与配额，不能看到候选系统输出；审核者不接触候选结果。文档家族/问题近重复尽量按组划分。仅上下文隔离不能证明统计或人员独立，正式holdout仍需明确的数据保管和独立审核记录。

现有40题已经用于开发，仅保留诊断用途；既有被消耗确认集不恢复成盲测。Ragas/DeepEval自动评分是辅助，不能替代现有确定性证据组召回和硬性性能门槛。不同模型审核有助于减少同源偏差，但同一Key是否复用与评测独立性是两回事。

## 建议的Hermes多Agent流程

用户选择fast/deep/auto → 主Agent规划 → 复杂问题拆成有依赖的子任务 → 独立可并行的检索子Agent执行，依赖任务按顺序解锁 → 返回结构化证据包 → 核验Agent按原文逐项检查 → 主Agent生成带引用回答；证据不足时预算内补检或明确拒答。

保留现有原生delegate_task、profile、会话隔离、工具白名单和共享预算。审查对象应是claim/source/version/span/hash，不能仅是其他Agent的总结或投票。技能SKILL.md描述工作流程；脚本/tool负责证据绑定、预算、权限和结果校验。fast/deep/auto、并行数、轮次、超时和成本可做用户配置，但服务器硬上限不可由模型或用户配置突破。

下一轮应先冻结开发实验配置，以相同检索器/语料比较：单Agent、主Agent+检索子Agent、再增加独立核验Agent；并同时设置资源相近的单Agent对照，报告调用量差异。指标包括证据覆盖、答案正确/支持性、实体和数字一致性、无答案误答率、工具越权、完整p50/p95与token消耗。开发结果足够后再消耗新holdout。

当前四Agent合成试验已验证委托与证据链，但尚未接入真实8091、未完成单/多Agent配对质量对比、正式异步恢复与业务准入。4B Top100重排约5秒的瓶颈不会因增加Agent自动消失；deep需独立的时延/成本目标，fast原门槛保留。

## 本轮交付状态

仅保存本机调研文档与日志索引，没有同步服务器、安装第三方库、生成新题或消耗Key额度。上表属于后续实施建议，未修改已有准入结论或正式开发计划。
