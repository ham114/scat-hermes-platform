# 独立项目初始化记录

日期：2026-10-22。新项目：`C:/python/scat-hermes-platform`；来源：同级 `agent-service-toolkit-main`。源目录只读，未移动或删除原文件。

## 选择性迁入

保留 SCAT/UAV、模型运行、任务与结果协议、RAG、FastAPI、客户端、Vue 门户与单元测试。LangGraph 航空业务入口、模型/实验内部图及 Streamlit（含其 voice 导入依赖）用于过渡兼容。

删除了新副本中的模板 Agent 注册项，只注册 scat-flight-agent；没有复制 research、chatbot、GitHub MCP、supervisor 等演示 Agent。保留现有依赖锁，尚未以猜测版本添加 Hermes 或实现新的 MCP Server。

仅迁入可移植业务测试；依赖历史模型服务 golden fixtures 的后端测试、生产验收、备份演练和原模板 Agent 测试不在本基线中。测试所需合成输入保留在测试代码内。

## 未迁入

`.git`、`.env`、`.env.gbar`、私有凭据、Conda/venv、node_modules、数据库、业务数据、知识库材料、模型权重、缓存、构建结果、临时目录、历史日志、旧生产 Compose/Docker 文件和部署脚本。

未迁入独立 GBAR 模型服务的打包工作区：应用侧 HTTP/子进程协议和加载适配保留，实际模型仍作为外部服务或独立模型环境接入。

## 初始化调整

- 项目标识与锁文件根包名称改为 scat-hermes-platform。
- 前端包标识改为 @scat-hermes/platform-web，保留其依赖锁。
- 新建 README、NOTICE、可跟踪源码/测试/文档/Compose 的 .gitignore。
- 保留 MIT LICENSE；完整来源及文件摘要见根目录 bootstrap-manifest.json。
- 未创建或复制完整依赖环境；没有启动本地或服务器服务，也没有修改服务器部署。

## 开发顺序

1. 从工具模块抽出任务应用服务、确认存储和可信执行上下文。
2. 发布代表性只读 MCP 工具并验证 Hermes 调用。
3. 通过 FastAPI 运行时适配器连接 Hermes HTTP/SSE。
4. 接入任务确认、运行状态、artifact 与图表恢复。
5. 在 infra/compose 中新增经过验收的服务定义，复用既有数据库并逐步切换。

本项目是复制和裁剪后的开发起点，不表示已完成 Hermes 迁移。未来开发使用新项目目录，不回写原项目。

## 初始化验证

- 371 个迁入或新建文件的内容摘要已核对；原项目对应源文件摘要未变化。
- Python 本地模块导入闭包检查通过，没有缺失的内部模块。
- 项目改名后的 `uv lock --check --offline` 通过，锁定 256 个包。
- 新副本的业务回归：**665 passed**，18.88 秒；有两个既有依赖弃用警告。
- 使用本机已有 `scat-frontend` Conda Python 3.12.14。该环境缺少 Redis Python 客户端，因此仅将锁定的 `redis==7.2.2` 放入新项目 `.tmp/test-userbase/Python312/site-packages`，通过本轮进程的 PYTHONUSERBASE 使用；未修改 Conda、系统配置或测试断言。
- 新项目没有安装 Node 依赖，因此未执行前端构建和测试；Vue 源码、单元测试与 lockfile 已迁入。
- 不复制旧 Git 历史，也未初始化或提交新的 Git 仓库。

正式开发统一使用 Anaconda 环境，禁止在项目目录创建 venv。服务器可先复用已有 `scat-agent`；本机暂用已有 `scat-frontend` 执行其依赖覆盖范围内的测试，完整后端环境需另行准备。Node 依赖使用 `npm --prefix frontend ci`。初始化的 `.tmp/test-userbase` 只作历史记录，后续不得再以此注入依赖；所需依赖安装到选定 Conda 环境后再测试。

`bootstrap-manifest.json` 记录初始化文件快照，不是后续开发的实时清单。后续更改由开发记录与版本管理跟踪。最新目标、环境纪律和验收标准见 [开发与测试计划](../SCAT_HERMES_PLATFORM_DEVELOPMENT_AND_TEST_PLAN.md)。
