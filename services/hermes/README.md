# Hermes 运行时

应用适配原生 Hermes，使用 `scat-main` 处理对话和方案，使用 `scat-worker` 执行已批准任务，使用 `scat-author` 处理技能草稿。主 Agent 与后台 Agent 按运行读取固定的技能发布包。

上游代码的仓库、提交及环境版本记录在 [upstream.lock.json](upstream.lock.json) 和 [upstream.source.json](upstream.source.json)。按锁定提交准备 `vendor/hermes-agent/` 和独立 Conda 环境后，在仓库根目录初始化角色配置：

```bash
python -B scripts/profile_manage.py init --reuse-root-key
python -B scripts/run_hermes_api.py --profile scat-main
```

为平台配置原生服务连接和持久状态目录，使用 `service.hermes_app:app` 启动 FastAPI。任务执行服务和作者接口按 [Compose 配置](../../infra/compose/compose.yaml) 中的角色运行。正式部署所需连接与凭据由部署环境提供。[配置说明](../../docs/CONFIGURATION.md)

[技能发布清单](skills.lock.json) · [工作流操作契约](workflow-operations.v1.json) · [模型步骤契约](model-operations.v1.json)
