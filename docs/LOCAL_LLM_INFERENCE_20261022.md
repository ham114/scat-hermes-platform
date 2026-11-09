# 本地 LLM 下载与推理验证

日期：2026-10-22。服务器为 h3c@172.26.67.148，单张 Tesla V100-PCIE-32GB。本次只运行隔离测试，没有接入 Hermes、切换平台默认模型、修改驱动或重启生产服务。

## 权重完整性

上次下载因远端连接重置，在 12,552,074,467 字节中断。本轮保留已下载前缀完成续传，最后使用分段传输补齐，并完成整文件检查。

- 模型：Qwen3.6-35B-A3B，GGUF Q4_K_M。
- 文件：`/mnt/h3c/scat-rag/models/llm/Qwen3.6-35B-A3B-GGUF-7878dc9a905c/Qwen3.6-35B-A3B-Q4_K_M.gguf`
- 大小：20,419,565,568 字节，约 20.42 GB。
- 仓库版本：`7878dc9a905c7c9379e5e8d6a1aa9affdd6839c5`。
- 实测 SHA-256：`671e47e0ec53c665d048b98c3ecbfd5236b5ca9c3e02ed19fc8f81f7b85140c7`，与官方记录一致。
- GGUF 文件头通过；校验完成后由 .part 重命名为正式文件。
- 官方校验记录：https://huggingface.co/ggml-org/Qwen3.6-35B-A3B-GGUF/blob/main/Qwen3.6-35B-A3B-Q4_K_M.gguf

## 实际运行方式

- 引擎：llama.cpp b11796，提交 `0a1ef309cdbb9f2024f58a38fb4c42352c5db569`，官方 CUDA 12.8 运行包。
- 引擎包和 CUDA 用户态包均与 GitHub 发布资产的 SHA-256 匹配后才解压使用。
- 运行目录：`/mnt/h3c/scat-rag/runtimes/llama.cpp-b11796-cu128/`。
- 官方预编译包要求 GLIBC_2.38；宿主 Ubuntu 22.04 不满足，因此使用临时 Ubuntu 24.04 容器，没有升级宿主 glibc。
- 基础镜像通过服务器可达的 docker.1ms.run 来源拉取，固定摘要 `sha256:68ee22004a9109b44973313b02ff7b1de250f1de0f5ecb628d73f579aaff33ac`。
- 使用只读模型与运行包挂载、显式 GPU 设备与现有驱动库挂载；不授予 privileged，不挂载 Docker socket。
- 必需驱动库包括 libcuda 和 PTX JIT/NVVM/GPU compiler 库。最初只识别设备而缺少 JIT 库时加载失败，补充只读挂载后完成实际推理。
- 测试监听 `127.0.0.1:8093`，仅本机访问；测试完成自动停止并移除容器。它不是常驻推理服务。

参数：8,192 上下文，单并发，batch 256，ubatch 64，CPU 线程 12，GPU layers 99，Flash Attention 关闭，启用 Jinja 模板，温度 0，响应上限 192 tokens，关闭 thinking。

## 测试结果

| 检查 | 结果 | 请求耗时 | 输出 tokens | 引擎报告生成速度 |
| --- | --- | --- | --- | --- |
| 算术 | 17×23 输出 391 | 15.418 秒 | 4 | 64.89 tokens/s |
| 中文生成 | 正常输出 ADE/FDE 中文说明 | 0.952 秒 | 68 | 97.17 tokens/s |
| 自动工具选择 | 工具名、batch、callsign、limit 与预期一致 | 2.081 秒 | 75 | 96.73 tokens/s |
| tool_choice=required | 返回指定工具集合中的有效调用，参数正确 | 0.869 秒 | 75 | 96.75 tokens/s |
| 模拟工具结果回填 | 说明 2 条记录，列出 TEST-001/TEST-002，并识别为合成测试数据 | 2.023 秒 | 90 | 51.46 tokens/s |

最终 5 项基础检查通过。原始结果为同运行目录的 `inference-results.json`，最终状态为 `completed`、`checks_passed=true`、`test_container_stopped=true`。

模型加载及测试准备约 44.35 秒。首次请求约 15.4 秒；热请求约 0.87–2.08 秒。首轮请求存在明显冷启动开销；后续重复提示存在前缀缓存。短输出的 tokens/s 不能替代真实业务吞吐量或长上下文基准。

这是推理与协议小样本检查，不是航空领域质量验收：中文检查只验证有中文响应和所要求术语。人工阅读发现 ADE 说明额外提到“平滑度”，该表述不严谨；实际业务解释应继续使用项目指标定义与检索证据约束。

## 显存采样

- 测试前空闲：30,173,298,688 字节，约 28.10 GiB。
- 模型加载后空闲：9,407,299,584 字节，约 8.76 GiB。
- 差值约 19.34 GiB；这是当前配置下的采样差值，不是峰值显存测量。
- 总可见显存：34,072,559,616 字节，约 31.73 GiB。

不能据此认定 LLM、RAG、GBAR 可以任意并行。上下文长度、batch、其他进程、模型执行都会改变显存占用，需要另做共存与并发测试。

## 接口兼容边界

首轮还尝试了 OpenAI 风格的对象形式 `tool_choice={type:function,function:{name:...}}`。引擎日志报告类型不符并回退默认值；即使恰好返回正确工具，也不能证明强制指定语义生效。

最终补测使用字符串 `tool_choice=required`，没有相关协议警告。后续 Hermes 对接需验证实际发送的 tool_choice 形式；若依赖强制指定某个函数，需要适配或选择修复该行为的固定引擎版本。

首轮结果和日志单独保留为 `.initial` 文件；首轮结果回填仅回答数量，未列出未被明确要求的 ID，故未满足最初较严格的 ID 检查。补测明确要求列出 ID 并识别测试数据后通过。没有把该现象当作已修复的模型代码缺陷。

## 清理与平台状态

- 两轮临时推理容器均已退出，最终测试容器自动停止。
- 模型文件、固定引擎包、测试脚本与结果保留在 /mnt，供后续集成复用。
- 原有 `http://127.0.0.1:8080/health/ready` 在测试后返回正常，MongoDB connected、memory initialized、默认 Agent loaded。
- 本次没有执行真实 SCAT 工具，没有访问业务数据库，也没有完成 Hermes 端到端集成。

可复用请求脚本：`scripts/probe_local_llm.py`。它只请求给定模型 API，工具响应使用明确标记的合成数据。

Compose 管理建议见 [部署说明](HERMES_COMPOSE_DEPLOYMENT_NOTES_20261022.md)。
