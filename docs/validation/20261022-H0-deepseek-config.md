# H0：DeepSeek 默认配置与部署规划验证

日期：2026-10-22。工作目录：`C:/python/scat-hermes-platform`。仅本机配置、代码和文档变更，未部署服务或切换服务器入口。

## 变更

- 根 `.env.example` 和未提交的 `.env` 预留空 `DEEPSEEK_API_KEY`，默认 `deepseek-flash`、`https://api.deepseek.com`，开发后端回环端口 18080。
- 现有模型配置支持 Flash / V4 Pro；缺少 DeepSeek Key 时明确报错，不回退使用其他厂商 Key；新模型默认关闭思考模式以先验证工具循环。
- 新增 Hermes 模型参考配置与容器部署说明；Hermes 尚未安装或完成启动适配，Compose 尚未实现。
- 开发计划更新到 v1.1：DeepSeek 优先、本地 LLM 后置、Nginx 统一入口。

## 实际验证

解释器：`C:/ProgramData/anaconda3/envs/scat-frontend/python.exe`，Anaconda Python 3.12.14。未创建 venv、未安装依赖、未注入初始化临时依赖。

```text
python -m ruff check src/core/llm.py src/core/settings.py src/schema/models.py tests/core/test_deepseek_config.py
All checks passed!

python -m pytest tests/core/test_deepseek_config.py tests/core/test_settings.py tests/core/test_llm.py -q
33 passed in 8.44s
```

HTTP 契约测试使用 httpx.MockTransport，验证实际 SDK 请求的模型、地址、Authorization、工具 schema 和关闭思考模式参数。密钥为合成值，没有外网请求或真实 API 费用。

## 未验收

后续基础文本 API 连通性已验证，见 [真实 API 检查](20261022-H0-deepseek-api-smoke.md)；流式、工具循环与限流处理仍待验证。Hermes 端到端、MCP、Nginx 代理、Docker 构建、Compose 启停、真实数据库和前端完整回归均未在本轮执行。33 项为受影响范围测试，不能与初始化的 665 项重复累计为新增覆盖。
