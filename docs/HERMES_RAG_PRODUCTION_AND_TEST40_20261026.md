# 0.6B正式接入与第二批40题测试

开发纪律与地址（2026-10-26，开发前）：服务器项目`/home/h3c/project/python/scat-hermes-platform`；生成器为同级`ragas-evaluation`，仅其既有Anaconda ragas-eval，GPU服务仅scat-rag-train，Hermes仅scat-hermes，接口测试仅scat-agent。本机仅已有scat-frontend/base。禁止venv/uv、新环境或安装依赖。任务暂存仅两个项目内`.tmp/rag-live`及原子传输暂存，结束清理；正式配置、版本和日志留项目var。既有80/443/8080/8501/8091/27017保留；本项目0.6B正式服务为回环18133，真实主Hermes API为回环18081，部署前验证用18134且结束关闭。使用已有DeepSeek出口与两项目原有独立Key，不输出或复制Key到本机。

授权：用户明确选择0.6B正式接入，并要求用已部署开源项目新生成40题后测试。本轮允许正式本项目RAG配置与常驻服务接入，无需再次确认；旧项目共用8091保留用于兼容与回滚，不更换模型或现场调参。前端仍暂停。

实施顺序：

1. 为已核实的Table 1B七包94,992/全部十三包167,547范围问题创建新的项目内语料修订版，旧149块语料和已审核40题保留。记录变更、来源版本、chunk映射及向量缓存身份；使用既有BGE权重，不训练模型。
2. 将固定BGE/RRF Top100+Qwen3-Reranker-0.6B实现为正式服务，兼容既有/search的1–10条结果、引用与分数，提供健康/就绪、有限并发、日志轮转、优雅停止、用户级systemd重启与明确回滚。服务器已启用user linger，无需sudo。正式配置从开发比较题配置中分离，不依赖题目/标签。修改仅本项目RAG指向；真实技能调用验证后保留常驻。
3. 在ragas-eval中调用Ragas原生图变换与TestsetGenerator生成第二批40题，优先20单跳/20多跳；多跳是否实质跨来源另验，不以生成器名称自动认定困难。保存原生输出，做新请求证据复核、逐字引用/字符偏移检查和对旧题/批内去重，不拿旧题改写充数。自动检查不冒称人工批准。
4. 固定新40题、语料、运行参数和源码后，使用正式0.6B端点测试检索及单Agent问答；多Agent模式维持可选，增加必要的深度题对照。题目与标答仅在运行外评分，不进入回答Agent。先报检索完整证据@5/@10、问答/引用正确性、拒答/冲突及单次/并发时延；语义模型初审与人工判断分列。结果不达标也保留全部失败，不按成绩修改标签。

本轮资源上限在启动模型调用前固定：生成与证据复核最多600请求、累计已返回usage 900,000 token后停止新请求、SDK重试0；单请求输出4096，上限3小时。问答与外部评分最多500请求、保守总量20,000,000，单运行沿用28请求/100万/60工具/360秒、输出4096；分片避免原生基础上下文估算不足。只允许在记录剩余额度后调整内部阶段分配，不重置账本，不超过上述全轮请求/计量。生成器仍读专用Key，问答读本项目profile Key；无新Key。

新批次为自动验证的上线后开发抽检；未人工审核，不称独立正式准入。固定检索参考目标为证据组R@5≥0.90、R@10≥0.95，精确引用校验全部通过；问答模型初审参考≥0.90但不能代替人工质量判断。用户接受的1.38秒是单次检索重排观测值，单Agent完整答案与并发排队另外测量，不把它当端到端承诺。

生成与评测各有不可覆写的批次/阶段标记与每请求持久账本；连接中断先检查运行状态与日志，不重复启动已完成付费阶段。正式上线状态须以systemd active、健康配置身份、实际工具调用、停止/重启与恢复检查为依据。不能以文件创建或进程存在代替可用。

## 已完成的正式接入

本项目的只读知识后端正式使用 BGE-M3 + RRF Top100 + Qwen3-Reranker-0.6B。模型固定提交 `e61197ed45024b0ed8a2d74b80b4d909f1255473`，保持全部 Top100 重排，未改 Embedding、未训练或安装依赖。入口为服务器回环 `http://127.0.0.1:18133/search`，健康地址 `/health`、`/health/ready`；主 Hermes API 为 `http://127.0.0.1:18081`，使用真实 MCP、`scat-main` profile 和 `aviation-knowledge` 正文加载守卫。fast/deep/auto 仍由参数选择。本轮未发布前端、未启动全部业务工作流，也没有替换旧项目的 8091。

新增用户级 `scat-hermes-rag.service`、`scat-hermes-api.service`，启用 linger、开机常驻和失败重启；API 对 RAG 使用 Wants/After，RAG 重启期间 API 可保持运行。GPU 串行计算、最多两个在途请求，超额返回 429/Retry-After；输入和 limit 严格校验、异常进入降级状态并由 systemd 恢复，避免静默返回旧模型结果。日志保留在 journal 与项目 `var/rag/runtime`，服务日志 4 MiB×4 轮转。

正式运行 home 为 `var/hermes/rag-production`，凭据和 API key 保留服务器、权限 0600；不要删除运行中的 home、`.tmp/mcp` 或数据库。项目 `.env` 只切换三个 RAG 参数，旧值独立保存在 `var/rag/deployments/live-v1/previous-rag-options.json`，不复制其他密钥。

语料建立独立修订版 `var/rag/corpora/kg_20261026_research_v4`，仍为 149 块、8 个来源，SHA256 `208c2322f951ac53028dcb999b75222d4ed4cac945d4122c76dd9544f9d38c96`。修正 Table 1B 七包小计为 94,992，全部十三包为 167,547；27 个同来源块统一产生新身份，保存 old→new 映射和 revision。148 行向量复用，仅重新编码改文的一行。旧原始语料、向量和已审核 40 题均保持原 SHA。新 source_version 表示规范化块集合摘要；继承的标题/行号仅为父来源定位线索，逐字引用以新块正文及字符偏移为准。

服务器操作命令，均从本项目根目录执行：

```bash
/home/h3c/anaconda3/envs/scat-agent/bin/python -B scripts/manage_rag_production.py status
/home/h3c/anaconda3/envs/scat-agent/bin/python -B scripts/manage_rag_production.py start
/home/h3c/anaconda3/envs/scat-agent/bin/python -B scripts/manage_rag_production.py rollback
```

`rollback` 只控制验证归属的两项新服务并恢复三个参数，不控制旧 8091。恢复使用 `start`。真实故障恢复、持久幂等、跨会话拒绝和回滚恢复结果见下方验收附件。

服务器对外链 user unit 的标准 disable 返回 Access denied，回滚实现因此改为校验并移除这两项自己的 `default.target.wants` 链接，再 daemon-reload；单元定义继续保留，systemd 显示 `linked`，表示可手动加载但没有自启动链接。初次脚本失败由 finally 自动恢复，日志未删除。另修正验收程序不应将旧健康响应的动态 uptime_seconds 纳入身份比较；最终只比较稳定模型/语料身份并重新演练。以上均无新增模型调用。

## Ragas 原生第二批 40 题

使用已下载部署的独立 `/home/h3c/project/python/ragas-evaluation`，既有 Anaconda `ragas-eval` / Ragas 0.4.3。正式新增 `scripts/generate_live40.py` 与 `config/corpus-live-v4.json`，复用原 `ragas_workbench.py`，调用原生图变换、实体抽取和 `TestsetGenerator` 的 SingleHopSpecific / MultiHopSpecific；没有用旧题改写或自制题干补数。原始问题、参考答案、生成器名称和每条 native batch/index 均保留。原 `config/corpus.json` 保持旧版。

最终组成 **35 单跳 + 5 实质多上下文题**，共 40 道新题。起初计划 20/20，但许多标为多跳的候选只需一个上下文便可回答，被必要性检查拒绝。第一阶段得到 25 题；测试前进一步去掉 024 与 005 的近重复题，并修复未生效的 Ragas prompt 属性名，继续累计原账本。第二阶段只有 25 道有效候选，故在任何检索/问答测试之前，将组成固定为 35/5，使用新块的原生 NER 与单跳生成补足，保持严格的跨上下文要求。未为凑多跳数量降低筛选要求，也未根据答题结果换题或改标答。

生成最终累计 **597 请求、581 响应、16 错误**，返回 usage 为 input 613,838 / output 86,807 / total 700,645，低于 600 请求与 900,000 返回 token 上限。失败批次、v1/v2 源码、阶段 marker、拒绝原因和原生输出全部保留在 `reports/batch-20261026-live40`；多阶段未重置计量。40 题均为自动证据检查通过、human_review=pending，不能称为人工审核后的独立准入集。

题目与参考见 [完整题单](ragas-review/20261026-live40/QUESTIONS.md)，机器数据见同目录 candidates.jsonl / summary.json，冻结测试输入见 `evaluations/rag_live40_ragas_20261026_v1`。131 处标准引用逐字及偏移核对通过，原生输出身份、旧 40 题和更早 4 题的精确去重均通过；预测试近重复筛选另有拒绝记录。

## 正式接口测试结果

冻结题集、语料、参数和九项关键源码后，经正式 18081 API 发起 40 次 fast、预选的 5 次 deep；答案 Agent 不接收参考答案和 gold 标签。deep 是主对话 Agent 委托受控研究流程，四会话研究由协调者、两名并行研究者、核验者组成，主 Agent 为额外上层会话；五题累计发生七次完整研究操作，不能把全链路一概称为只有四个 Agent。

| 项目 | 结果 |
| --- | --- |
| 证据组 R@5 / R@10 | 0.975 / 1.000 |
| 完整证据 @5 / @10 | 39/40 / 40/40 |
| 检索重排 p50 / p95 | 1.353 / 1.519 秒 |
| fast 原始执行及工具出处 | 39/40 |
| fast 原始有效模型初审 | 38/40 |
| fast 单次连接失败补测后 | 执行 40/40，模型初审 39/40 |
| deep 执行、出处及模型初审 | 5/5，仅五题对照 |
| 原始 fast / deep 端到端中位 | 11.904 / 93.631 秒 |
| 完整后端回归 | 1263 passed / 1 skipped；一条既有 Starlette 提示 |

初次评分有 33 题因评分请求未明确包含 JSON 一词而被供应商以 BadRequest 拒绝；涉及的 deep 评分一起缺失，并非答案质量失败。按[官方 JSON 模式协议](https://api-docs.deepseek.com/guides/json_mode/)修复传输提示后，只重新评分这 33 题的原始答案，原先 7 题有效判断保持。所有原始错误、修复源码、剩余额度和修复后评分分别存档。

LIVE-20261026-004 原始 fast 在调用知识工具前发生模型供应商连接失败，保留原 failed run；确认终态后仅重试此题一次并通过。实际内容缺口为 **LIVE-20261026-008**：问题要求指出事实来源文件 `SCAT_DATASET.md`，答案只给了文档标题、source_id 和大量内容概述，没有直接给文件名，模型初审未通过；未重写题目/标签或用多次答案择优覆盖。后续应改善答案直接性并另建版本回归，本轮未为追求满分继续调参。

问答、冒烟、无效评分与修复合计 **341 请求、14,953,887 保守计量单位**，在 500/20,000,000 上限内；API 268 请求、评分 73 请求。生成另为 597 请求，整轮合计 938 请求。生成的返回 usage 与问答的保守预算单位口径不同，不直接相加为实际 token 或账单。均使用现有独立 Key，无新 Key、无装包。

独立程序复核确认 45 个原始运行、452 条工具证据、七次研究操作和其中 70 处深度引用；9 项冻结源码未变。原始检索阶段只保存覆盖指标、未保存完整排名，故另行只读重放 40 次 Top10，逐题复算指标并保存排名 ID/分数；重放 p50 为 1.345 秒，明确与首次检索区分，没有新增模型请求。评分属于同供应商新上下文初审，不代表独立人工判断；五题 deep 不足以证明多 Agent 总体优于 fast。

## 验收与留档

- [正式技能、幂等、隔离及崩溃恢复](validation/20261026-rag-live-smoke.json)
- [原始 40 题记录](validation/20261026-rag-live-test40.json)
- [评分修复和单次连接重试后的结果](validation/20261026-rag-live-test40-final.json)
- [原生题目、引用和排名独立复核](validation/20261026-rag-live-test40-verification.json)
- [真实回滚与恢复](validation/20261026-rag-live-rollback.json)
- [最终环境、旧数据、凭据检查及部署状态](validation/20261026-rag-live-final-audit.json)

完整模型响应、运行数据库、账本和回归日志留服务器项目 `var/validation/20261026-rag-live`；Ragas 原生图与批次保留其独立项目 reports。任务暂存清理后不影响正式服务。后续质量工作保留：人工抽查新题/参考与初审争议、文件名直接回答缺口回归、更大且均衡的多跳独立测试。当前完成的是本项目知识后端正式接入及新 40 题开发抽检，不宣称整个平台或前端发布完成。

最终核对：回滚恢复 10 项全部通过，正式两服务 active/enabled，user linger=yes，旧六端口及 8091 健康，测试 18134 关闭。既有三项 Conda history、原始语料/向量/冻结标签、Hermes 与预算源码锁定全部保持；八个模型文件重新逐 SHA 核验。2,571 个文本文件实际私有值扫描无暴露；两服务器项目本轮 `.tmp/rag-live` 已清理，正式日志、原生输出和运行状态保留。最终总验收为 passed，本机同步文件校验另附 [同步清单](validation/20261026-rag-live-synchronized-files.json)。
