# Qwen3-Reranker-4B 开发诊断对比（2026-10-25）

开发纪律与地址：服务器项目 `/home/h3c/project/python/scat-hermes-platform`；Python仅已有Anaconda scat-rag-train跑模型、scat-agent跑契约测试，本机scat-frontend/base检查与传输，禁venv/uv/新环境/安装依赖。正式权重使用 `/mnt/h3c/scat-rag/models/`，暂存仅两端项目 `.tmp/qwen3-compare` 并已清理；正式逐题材料只留服务器var/validation。比较离线，无新服务或监听；原80/443/8080/8501/8091/27017保持，H6预留18130–18133关闭。已有NVML版本不匹配不改驱动；本轮真实CUDA计算通过，不能因此声称生产GPU运维问题已解决。

## 结论

在当前语料对应的40题中文开发诊断集上，原召回+4B重排改善Top5/Top10证据覆盖率，但完整耗时中位数约4.98秒，超过旧检索75/200毫秒预算。因此只保留为离线研究候选，不替换8091；本次不是独立泛化或生产准入验证。

| 指标 | 原BM25+BGE/RRF | 相同候选+Qwen3-Reranker-4B |
| --- | ---: | ---: |
| Claim-group Recall@5 | 0.9500 | 1.0000 |
| Claim-group Recall@10 | 0.9500 | 1.0000 |
| 完整证据包覆盖@5 / @10 | 0.9500 / 0.9500 | 1.0000 / 1.0000 |
| Group MRR | 0.9050 | 0.9875 |
| Accepted-evidence NDCG@10 | 0.9140 | 0.9908 |
| 候选group Recall@100 | 1.0000 | 1.0000 |
| 本轮离线总耗时p50 | 25.37ms | 4980.61ms |
| 本轮离线总耗时p95 | 26.82ms | 5280.64ms |

Top5改善2题、退步0题、其余38题相同；改善来自scat_semantics与system_usage各1题。8类别最低Top5覆盖由2/3提升至1.0。该诊断集来自语料且已用于开发，不能据40/40覆盖推断真实用户准确率或独立泛化。

重排本身p50/p95为4955.44/5255.55ms；共享测量的一次原召回加上完整重排后，中位数约为原召回196倍。40对耗时样本包含分词、编码、融合与重排，未包括HTTP/排队/并发；没有将新数字包装成线上SLA。旧9项参考中6项质量条件在本诊断集满足，p50、p95及p50增量3项失败；独立数据条件本身也未满足，准入保持false。

## 输入身份与边界

当前8091健康身份及磁盘均为kg_20260926_research_v3、149块，语料SHA `61788775e03c392b493a7835bba2e5d38d7f8721035b73ccbb1dda441eb53b86`。历史m8-governed-dev报告实际使用580块，旧30题dev的53个引用ID在当前语料中全部缺失，未将其历史分数与本轮作数值对比。

本轮使用research-v3-diagnostic-v1的40题，均dev/rag/zh-CN，8类别；v1.1 claim groups的41个引用块全部有效。固定语料、原BGE向量缓存、问题、证据包及原召回源码SHA后，再冻结比较源码和参数。定位输入时只向当前任务返回版本/数量/字段等元数据；实际仅执行这40题dev，未重跑旧独立validation、confirmation或自然confirmation，未将消耗集的问题/证据/排名用于选参或训练。未生成答案或外发语料，4B Embedding未参与。

## 实现与实际资源

固定原BM25+BGE/RRF参数及Top100，全100候选由4B纯yes/no概率重排，同分保留原顺序；不加新候选、不修改证据标签。每题新辅助召回前10名与未修改的原runtime一致，40题候选集合均完全一致。4B使用固定ModelScope提交14f36843fc02cea94c1e0c59b6ae0ab1f9b9ebe2，运行前完整校验其17文件SHA。

V100 32GB、FP16/SDPA、batch4、总输入上限2048token，官方提示前后缀完整保留。共计4000对，截断0对，实际最大输入811token。仅计算最后token logits的优化与完整logits合成校验最大分差0；先完成最大batch/context的合成资源预热，再统计实际对比。

本进程同时驻留原BGE和4B重排，实际比较PyTorch峰值allocated约9.86GiB、reserved约11.06GiB；最大上下文合成预热reserved约15.46GiB。这与之前仅加载单个4B模型的小样本显存口径不同。运行结束后CUDA探测空闲恢复约28.10GiB，本轮模型进程0。

服务器相关47项测试通过（新增11项）：原召回前缀一致性、非法/不完整分数、稳定同分、截断提示完整性、多分块替代证据包必须完整覆盖。Ruff/格式通过。新比较模块没有接入生产工具或服务入口。

## 记录与后续

- [固定参数与输入SHA](../services/rag/qwen3-comparison-v1.json)
- [首次比较前预注册](validation/20261025-Qwen3-comparison-preregistered.json)
- [无题目正文的完整汇总](validation/20261025-Qwen3-comparison.json)
- [计分复核、源码/输入不变及清理验收](validation/20261025-Qwen3-comparison-acceptance.json)

服务器逐题排名/原始分数/计时保存在 `var/validation/20261025-Qwen3-compare-c1f60b6709/`，完整日志为 `var/validation/20261025-Qwen3-comparison.log`，不导出到本机。实际只完成一次固定配置比较，之后独立重算计分和候选集合，没有根据结果修改参数或标签。

后续先解决完整重排的延迟：可将更轻模型列为新的独立对照，保持Top100和同样指标；候选裁剪或分层路由须重新核查证据覆盖与端到端成本。达到开发目标后仍需另建符合clean-room流程的独立评测，旧消耗集不恢复使用额度。
