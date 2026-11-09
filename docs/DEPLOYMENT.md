# 部署指南

本指南说明源码的部署方式，不包含任何现有环境的地址、账号、凭据、数据或模型制品。首次安装先按根目录 README 在本机验证，随后准备自己的服务器配置。

## 1. 准备依赖和私有材料

准备 Anaconda 管理的 Python 3.12 环境、Node.js 24、npm 11、MongoDB 及反向代理。本机和服务器禁止在项目目录创建 `.venv` / `venv`；安装、测试与启动均使用明确的 Conda 解释器。完整纪律见 [开发与测试计划](../SCAT_HERMES_PLATFORM_DEVELOPMENT_AND_TEST_PLAN.md)。需要 GPU 模型时，另行准备兼容驱动和模型运行环境。服务器时间、磁盘容量和内网连通性由部署环境保障。

应用不附带数据库。导入自己的 SCAT/UAV 数据时，应满足 `src/scat/schemas/`、`src/uav/` 和各 repository 中的字段及单位契约；不要将任意格式文件直接导入后视为已完成数据适配。

实际 `.env`、数据库认证配置、TLS 私钥、账号文件、模型制品和备份密钥应保存在受限目录。模型、数据、知识库和运行记录不提交到源码仓库。

## 2. 分离发布与持久目录

建议结构如下，根目录由部署者自行选择：

```text
application/
  releases/<version>/      固定版本的源码和前端构建（Conda 环境在项目外）
  current/                指向当前发布
  shared/
    config/               受限环境配置和账号材料
    data/                 对话检查点和不可重建文件
    artifacts/            分析结果文件
    logs/                 运行日志
models/                   外部模型源码和权重
<anaconda-prefix>/envs/    项目外的应用及模型 Python 环境
backups/                  一致性备份，另保留异机副本
```

使用独立的非 root 应用用户，赋予必要目录读写权限。配置、数据及模型的访问权限按服务需要分别设置。SQLite 路径和结果目录指向 `shared` 下的持久目录。

## 3. 安装并构建

先准备与候选版本依赖兼容的 Conda 环境，再在新的发布目录验证并构建。服务器现有 `scat-agent` 可用于开发基线；它与旧平台共用，不应直接原地升级后当作新版本的隔离发布环境。下列命令只检查环境并构建前端，不安装 Python 依赖：

```sh
/home/h3c/anaconda3/envs/scat-agent/bin/python -m pip check
npm --prefix frontend ci
VITE_STREAMLIT_URL=/assistant/ npm --prefix frontend run build
```

将实际配置通过服务环境注入；配置不写进前端构建。现有服务的Conda制品地址/哈希、pip确切版本和库存已在[H7-A](HERMES_H7A_BACKEND_OPERATIONS.md)导出核验，实际环境重建及pip逐文件制品哈希仍留H7-B。Python依赖只在明确的已有Conda环境内管理，不自动创建环境，不把Torch等模型依赖合并进API环境。依赖变更前记录环境快照与恢复方案，不能只切换源码current链接便认为完成依赖回滚。

## 4. 托管应用服务

为完整 API 和 Streamlit 分别配置 systemd 服务：指定非 root 用户、固定 `WorkingDirectory`、对应版本的 Conda 环境内可执行文件绝对路径（不能依赖交互式 shell 自动激活）、受限 `EnvironmentFile`、失败重启策略、日志与资源限制。

下列 python、uvicorn、streamlit 命令均指目标 Conda 环境中的解释器或模块。API 的启动入口为 `python src/run_service.py`，设置 `MODE=prod`、`HOST=127.0.0.1`，并选择应用端口。仅部署数据接口时，可使用 `python -m uvicorn service.scat_only:app --app-dir src --host 127.0.0.1 --port 8080`。

Streamlit 的入口示例：

```sh
python -m streamlit run src/streamlit_app.py \
  --server.address 127.0.0.1 \
  --server.port 8501 \
  --server.baseUrlPath assistant
```

工作台配置 `AGENT_URL` 指向 API，服务端使用同一 `AUTH_SECRET`。部署后启用服务开机启动，并在维护窗口实际验证重启。不要直接复制其他环境的服务文件、路径或账号。

## 5. 配置 HTTPS 与身份入口

由 Nginx 等网关提供统一 HTTPS 入口：

- `/` 提供 `frontend/dist`，前端路由使用 SPA fallback。
- `/api/` 原样转发到 API，不删除 `/api` 前缀。
- `/assistant/` 转发到工作台，保留该前缀并启用 WebSocket 转发。
- Agent 流式接口按需透传给后端，关闭对应响应缓冲并设置合理超时；不要让 SPA fallback 吞掉 API 请求。

团队账号在网关验证。后端设置 `ANALYSIS_IDENTITY_MODE=trusted_proxy`，网关必须覆盖客户端传入的 `X-Authenticated-User`，只传递已认证的稳定用户标识；工作台也必须收到可信身份。后端直接访问应受限制，所有转发到受保护 API 的路径正确注入服务端 Bearer 凭据。

证书使用团队信任的 CA；自建内网 CA 时需要在客户端导入信任。检查不同用户之间的对话、任务、结果和下载均无法越权访问后，再开放团队使用。

## 6. 接入可选模型服务

先验证模型的独立运行环境、制品身份和健康检查，再在应用私有配置中启用对应后端。保持模型并发与队列限制，应用多开 worker 不等于增加 GPU 推理容量。

外部模型和知识检索的配置键见 [配置说明](CONFIGURATION.md)。未准备好相关材料时，保持功能开关关闭。仓库不附带一键复刻现有生产环境的配置或镜像。

## 7. 验证、备份与回滚

部署后检查存活、就绪、数据库、真实查询、对话、任务终态、结果跳转、用户隔离和模型身份。空数据库上的进程启动成功不能代替业务验收。

备份应覆盖业务 MongoDB、SQLite 检查点、不可重建结果、私有配置和必要模型制品。使用一致性导出或受支持的在线备份方式，备份加密保存并保留异机副本；解密身份另外离线保管。定期在隔离环境恢复并检查真实业务结果。

监控入口、应用、数据库、模型、任务、磁盘和备份时效；告警须限制重复发送并验证送达。发布前备份、排空任务、验证新版本，再切换 `current` 并重启服务。保留上一版和兼容的数据恢复方案，不自动重放状态未知的模型任务。

具体维护窗口、可接受数据丢失时间和恢复时间由团队确定。依赖安全审查与发布验收通过后，才将候选源码部署为新的生产版本。
