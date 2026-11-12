# 部署指南

## 本地开发

使用现有 Conda 环境安装项目依赖，准备 MongoDB 和本机 `.env`。前端使用 Node.js 24 与 npm 11。数据 API 和前端的启动方式见[项目首页](../README.md)。

## Agent 与后台任务

准备固定版本的原生 Hermes 上游源码和独立运行环境，再初始化 `scat-main`、`scat-worker`、`scat-author` 配置。以 `service.hermes_app:app` 提供平台 API，并为后台工作流配置任务服务和持久状态目录。[Hermes 服务说明](../services/hermes/README.md)

## 容器与网关

部署前准备固定镜像、本机部署参数和持久挂载目录。Compose 描述平台、主 Agent、任务服务和作者角色的连接。通过认证网关发布前端与平台 API，保存会话、结果、任务、活动技能包及发布状态。GPU 模型通过已登记的独立推理服务接入。[Compose 配置](../infra/compose/compose.yaml) · [配置说明](CONFIGURATION.md)
