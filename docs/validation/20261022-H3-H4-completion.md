# H3/H4 当前范围验收（2026-10-22）

当前固定单用户、DeepSeek 和五个只读 MCP 工具范围的 H3/H4 剩余项验收完成。保持原计划，H5 模型任务、H6 动态展示、完整历史/生产多用户、H7 Compose 与正式入口尚待后续阶段。

| 检查 | 最终结果 |
| --- | --- |
| 本机相关 Python | 114 passed，10.62 秒 |
| 服务器完整 Python | 788 passed，1 条既有弃用提示，25.65 秒 |
| 两套服务器 Conda pip check | scat-agent / scat-hermes 均通过，无依赖安装或升级 |
| 前端完整 Vitest | 22 文件、92 passed，7.08 秒 |
| 前端格式/lint/TypeScript/生产构建 | 通过，保留既有大块资源提示 |
| Edge 桌面/手机交互 | 6 passed，6.3 秒；发送、转义、刷新续接、停止 |
| 同数据三入口 | 真实共享 Service，REST/旧 Tool/实际 MCP；非空/空检索和轨迹、分页/抽样、UTC 等价、经纬度和高度数值一致 |
| 真 DeepSeek/MCP/结果读取 | 10637 与 REST 对照、3 点轨迹、两条 SSE 引用与 SHA256、同 UUID 重放、跨会话拒绝 |
| 真停止与降级 | 原生 cancelled；停止范围 conversation_only；Hermes 离线时已保存结果及确定性 API 可用，就绪检查正确 503 |

## 实现与边界

轨迹大页明确标为 fetched_page 摘要，不把部分页冒充完整航迹；超过摘要预算仍拒绝。错误和未授权在边界处理，MCP isError 与业务失败一致。结果收据仅由固定版真实工具完成回调产生，按 scope/run/call 隔离，单条 64 KiB、24 小时、最多 1000 条；它不是 H5 模型任务 artifact。接口及页面只说明停止对话；没有提交 GPU 作业，业务任务独立性由受控夹具及仅调用 native stop 的断言验证。

浏览器使用模拟接口，真实服务器链路独立验证。当前仍是固定单用户 profile，scope 测试不代表生产多用户文件/记忆隔离。通用图表与 LLM 动态编排继续放 H6；本轮页面仅调整停止文字与说明位置。现网 Nginx 未切换，无新增常驻服务。

## 失败与修复

首轮回归 780 passed、3 failed：测试把 REST pagination 当平铺字段，并比较 UTC 文本的字面形式。按已有契约映射字段与时刻后，第二轮 783 passed；再补 MCP 错误、分页及 stopping 边界后最终 788 passed。业务算法未因断言而改变，原失败报告保留。前端说明替换未命中由新断言发现并修正；手机截图发现长说明挤占按钮后，将说明移到独立一行并重跑完整测试/浏览器检查。验证命令首次从错误 cwd 读取 Playwright 配置，改为 frontend 目录后执行，未将配置错误算作测试通过。

## 证据

- [最终结构化记录](20261022-H3-H4-completion.json)
- [最终真实集成](20261022-h34-completion-attempt2.json)、[首轮真实集成](20261022-h34-completion-attempt1.json)
- [最终完整回归](20261022-h34-regression-attempt3.json)、[最终日志](20261022-h34-regression-attempt3.json.log)
- [首轮失败](20261022-h34-regression-attempt1.json.log)、[第二轮回归](20261022-h34-regression-attempt2.json.log)
- [最终桌面截图](20261022-H34-assistant-attempt2-desktop.png)、[最终手机截图](20261022-H34-assistant-attempt2-mobile.png)
- [阶段实现与范围](../HERMES_H3_H4_ACCEPTANCE.md)、[API 指南](../HERMES_API_AND_PORTAL.md)

最终同步与清理以本轮 completion-manifest 和 cleanup-final 记录为准；原始成功/失败报告及实现摘要保留。本轮没有重跑固定 20 题；H1/H2 记录保留为此前验收快照，不改写其统计。

- [最终清理记录](20261022-H3-H4-cleanup-final.json)
- [源码、文档与证据清单](20261022-H3-H4-completion-manifest.json)
