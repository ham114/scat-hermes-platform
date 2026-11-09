# H0：DeepSeek 真实 API 连通性检查

日期：2026-10-22。工作目录：`C:/python/scat-hermes-platform`。

## 已验证

使用 Anaconda `scat-frontend` Python 3.12.14 读取本机 `.env` 中的 DeepSeek 配置，向 `https://api.deepseek.com/chat/completions` 发送一条纯合成短请求，不含业务数据。模型为 `deepseek-flash`，关闭思考模式，非流式，输出上限 16 tokens。

实际 HTTP 状态为 200，模型回复 OK；耗时约 0.8 秒。服务报告输入 9 tokens、输出 1 token，共 10 tokens。密钥未记录到文档或工具输出。

这仅证明 API Key、模型名称和本机网络能完成基础文本调用。Hermes、流式输出、工具选择、多轮工具结果回填、真实业务查询和限流恢复尚未验收。

## 下一阶段准备

- 本机 `.env` 未显式配置业务 MongoDB；真实数据联调优先复用服务器私有配置，不将数据库暴露到外网。
- 本轮 SSH 端口探测可达，但实际登录连接超时，远端最新代码与配置未核实、未同步。
- 后续按 H1 固定 Hermes 版本与 Conda 安装方式，再以外部 DeepSeek 接入首批只读 MCP 工具。
- 首版使用人数影响登录、会话、状态隔离的实施范围；若无新增要求，建议先按单用户研究演示推进。
