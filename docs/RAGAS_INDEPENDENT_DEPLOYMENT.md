# Ragas独立部署记录

开发纪律与地址（2026-10-25）：Ragas运行项目位于服务器 /home/h3c/project/python/ragas-evaluation，独立Anaconda环境ragas-eval；Python3.11.14，禁止venv/uv。本文件仅记录部署位置和验收，运行代码、输入、配置及日志均在该独立项目。暂存仅两端项目目录，已清理；无新增监听端口，原80/443/8080/8501/8091/27017保留，18130–18133关闭。

## 已完成

- Ragas0.4.3，torch2.6.0+cu118，CUDA11.8实际可用，Tesla V100-PCIE-32GB。
- BGE-M3由独立环境直接加载已有固定权重，在cuda:0以FP16运行，输出FP32归一化向量。
- 当前8个来源、149个分块全部导入，语料SHA、chunk ID、版本及原文保持。
- Ragas原生EmbeddingExtractor完成149×1024向量提取，CosineSimilarityBuilder在0.75阈值建立198条相似关系；保存回读及来源身份检查通过。
- 全库模型加载后流程4.164秒，PyTorch峰值reserved约1.09GiB；这是接入测试，不是检索质量、服务SLA或独立准入结果。
- 部署时7项测试、pip check、缺Key拒绝检查通过；现已使用专用Key完成4题真实中文编题，详见[真实联调记录](RAGAS_LIVE_TEST_20261025.md)。
- 完整依赖版本锁及Conda显式清单已冻结；原三开发环境history摘要和原语料SHA未变。
- 本轮运行进程0，远程.tmp为空，本机暂存清理。安装包缓存清理，运行词表缓存保留。

## Key填写与首次编题

专用DeepSeek Key已填写到服务器文件 /home/h3c/project/python/ragas-evaluation/.env 的DEEPSEEK_API_KEY。该文件权限0600且被gitignore排除，无需在对话中发送Key；已有本地Embedding，无需其他模型API。

在服务器执行：

~~~bash
cd /home/h3c/project/python/ragas-evaluation
conda activate ragas-eval
python -I -B scripts/ragas_workbench.py doctor
python -I -B scripts/ragas_workbench.py generate --questions 4 --chunks 6
~~~

该命令将所选真实资料提交给DeepSeek，生成4道中文单跳开发候选题。实际接口、中文生成与原文映射已完成小样本联调，4题原文匹配，2题可直接作开发候选、2题建议改写表达。原始输出保留，不自动作为准入题。当前.env的模型名为deepseek-flash，API地址https://api.deepseek.com。

无Key复查入口为offline-smoke、embedding-smoke、graph-smoke，均通过同一CLI调用。完整使用说明在服务器独立项目README.md；日志在logs/DEVELOPMENT_LOG.md，结果在reports/，语料快照与图在data/61788775e03c/。

## 兼容修复与历史

前轮CPU阶段5通过2失败，原因是langchain-community0.4.2移除Ragas引用的VertexAI模块；固定0.4.1并补标准依赖后7通过。另保留两条LangchainLLMWrapper弃用警告，未修改上游安装包源码。DeepSeek通用客户端的max_tokens请求字段经不联网MockTransport测试核对，不能替代真实API联调。

DNS失败、依赖回溯、下载截断及SFTP中断的历史保留在独立项目日志。官方包SHA校验、有限续传及进程内有时效DNS回退用于完成安装；系统DNS、驱动与原8091均未修改。

验收：[结构化报告](validation/20261025-Ragas-independent-deployment.json)。

