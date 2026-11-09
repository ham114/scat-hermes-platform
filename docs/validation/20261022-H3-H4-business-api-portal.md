# H3–H4：真实业务、FastAPI 与门户验证

日期：2026-10-22。新项目：scat-hermes-platform。证据摘要见 [JSON](20261022-H3-H4-evidence.json)。

## 实现与结果

新增独立 `service.hermes_app` profile、线程/运行/停止/SSE 接口、SQLite 原子准入与结果未知恢复；门户 `/assistant` 提供流式回答、工具状态、停止、刷新续接和原请求恢复。原确定性 API、旧 LangGraph/Streamlit 入口继续保留。

真实 MCP 对指定 `scat20161015_20161021` 批次完成五类业务调用：

| 能力 | 证据 |
| --- | --- |
| 航班检索 | 返回 1 条，总计 10,637；与独立 MongoDB count_documents 一致 |
| 轨迹分页 | 返回 3 点，pagination.returned_points 与 points 数量一致 |
| 批次概览 | 真实统计和分布，约 3.39 秒 |
| 六特征质量 | 真实指定批次聚合，约 30.14 秒，未进行全库扫描 |
| RAG | 2 段结果，source_id/source_version/chunk_id/locator 均存在；来源 trajectory-evaluation-curated |

初次 MongoDB 查询因新项目未配置认证失败；在服务器本机复用旧项目必要数据库配置后通过。只复用 URI、库名和非秘密 RAG 默认值，不向本机传输数据库凭据。新项目 `.env` 为 0600。轨迹验证初次误读顶层 count，已按真实分页字段修正并重测，未修改业务算法或用默认值判通过。

## 真实 API 链路

使用服务器 scat-agent 执行 `python -B scripts/probe_platform_api.py`，自动启动独立 Hermes/平台进程，调用真实 deepseek-flash。结果全部通过：

- FastAPI -> Hermes -> MCP 检索的模型答案包含与数据库相符的 10,637；工具成功事件 1 次。
- 同一 UUID / 相同内容重放返回相同 run，replayed=true。
- SSE 第一次连接断开后，以 Last-Event-ID 续接；后续序号递增，无重复业务提交。
- 下一轮在同一 thread 回忆上一轮随机校验代号，证明会话历史实际续接。
- RAG 工具被实际模型调用，最终回答引用实际 source_id。
- 停止请求得到实际 cancelled，completed=false。
- 关闭 Hermes 后，平台就绪检查 503；MongoDB 仍可用，确定性航班 API 返回 200。
- 两个自建服务进程均退出。测试 profile 与临时绑定库在收尾时清理。

模型实测覆盖航班检索与 RAG；其余三类已通过真实 MCP/业务验证。尚未运行 20 题工具选择评估，不据此报告总体准确率或 SLA。

## 自动化与前端

| 检查 | 结果 |
| --- | --- |
| 本机 Conda scat-frontend，相关 Python 测试 | 67 passed，9.57 秒 |
| 服务器 Conda scat-agent，完整 tests | 730 passed、1 warning，24.57 秒 |
| 前端 lint / TypeScript | 通过 |
| 前端完整 Vitest | 22 文件、92 passed |
| 最终助手相关前端回归 | 15 passed |
| 前端生产构建 | 通过；仍有既有大块资源提示，含 Cesium 资源 |
| 桌面/手机 Edge 交互 | 6 passed；发送、原运行刷新恢复、停止 |

Python 用例覆盖归属、鉴权、未知收据恢复、原子并发准入、明确拒绝与 5xx 区分、SSE 终态、断流、跨会话拒绝、敏感字段过滤。前端覆盖 UTF-8/CRLF 分块、游标去重、EOF 非成功、HTTP 错误脱敏、UUID 回退、缓存损坏、取消与安全文本显示。

浏览器测试使用接口模拟；服务器集成测试使用真实数据和 DeepSeek。页面截图展示“验证样例”及 HTML 文本安全用例：[桌面](20261022-H4-assistant-desktop.png)、[手机](20261022-H4-assistant-mobile.png)。没有把模拟页面结果当作真实模型输出。

## Nginx 与遗留项

提供统一入口配置示例。项目内独立 Nginx 语法检查通过；定时合成 SSE 的首事件 0.007 秒到达、末事件 1.507 秒到达，证明透传未等完整响应结束，并验证 Last-Event-ID。Nginx 子进程与合成上游均退出；这不是 LLM 延迟测量。现网 Nginx 未切换，Compose 定义尚未实施。

仍需后续完成：20 题评估、模型任务确认/执行、结构化图表与 artifact、完整会话历史、长期状态与容量策略、生产认证及多用户 profile/MCP 隔离、Compose 与正式统一入口发布。尚未进行公网浏览器访问正式站点的验收。

本轮源码、日志、缓存及状态均位于新项目；Python 使用已有 Anaconda 环境，没有新增或升级旧环境依赖。Node.js 24.21.0 / npm 11.19.0 通过既有 package-lock 安装前端依赖。本轮临时文件、测试进程与私有值日志扫描的收尾结果见开发日志及 cleanup 记录。
