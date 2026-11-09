# Hermes 服务

标准入口现支持原生scat-main/scat-worker/scat-author与统一profile_manage。先使用现有scat-hermes执行 `python -B scripts/profile_manage.py init --reuse-root-key`，再用 `run_hermes_api.py --profile scat-main`；旧显式home保留。作者模式是实际原生Agent，仅有草稿工具与只读profile_manage；发布凭据仍隔离。完整布局、模型配置、三个私有.env位置见 [Profiles说明](../../docs/HERMES_STANDARD_PROFILES.md)。

S7-A 作者后端使用独立 `scripts/run_skill_author_api.py`、独立作者/发布凭据及版本化 catalog；业务 native 仍只有技能读取工具。主对话、任务准备和后台执行读取同一 `SCAT_SKILL_RELEASE_STATE`，新运行选择活动包，在途运行保持旧包。新增包须先通过隔离试跑和具体预览的人工确认；不自动覆盖 skills.lock.json 或原 home。启动、API、边界和验收见 [S7-A](../../docs/HERMES_S7A_AUTHORING.md)。

已固定 Hermes v0.22.3 / `5b147ab036d6d19131bff8d20701b5001eab844a`，服务器使用独立 Anaconda `scat-hermes`（Python 3.14）。平台与 MCP 继续使用 `scat-agent`，不升级旧环境。

实际启动、工具白名单、HTTP/SSE 契约、单用户边界和实测命令见 [运行契约](../../docs/HERMES_RUNTIME_CONTRACT.md)。源码与依赖身份见 `upstream.lock.json`、`upstream.source.json`；安装日志位于服务器项目 `var/setup/`。

根 `.env` 只向 Hermes 注入 DeepSeek Key、模型和 URL，默认 `deepseek-flash`；不向其复制其他凭据。`scripts/run_hermes_api.py` 自动生成独立内部服务 Key，并仅监听回环接口。参考 YAML 保留作说明，实际配置由启动器生成，不能将旧示例当作已部署服务。

本机、服务器及后续 Python 容器均使用 Anaconda，不创建项目 venv。FastAPI 与门户 /assistant 已实现，真实业务链路已验证；Nginx 提供项目内配置示例，现网切换及 Compose 仍待 H7。


## S1 可选技能配置

S6 后端默认启用 MCP、Skills、任务和工作流；已有 home 的配置不自动覆盖，升级显式使用独立 `--home var/hermes/skills-s6`。`--with-skills` 保留为角色配置参数（普通主角色要求 MCP），历史入口必须显式 `--legacy-h5`。仅通过 scat_skills 注册受控 skills_list/skill_view，不整体启用含管理工具的 skills 组。skills.lock.json 是审核发布清单，不由运行时自动生成；候选留在 authoring/drafts。新旧 profile 实际启动/停止、归档维护、留出基线及回滚规范见 [S6 说明](../../docs/HERMES_S6_BACKEND_ACCEPTANCE.md)。两套运行环境继续使用已有 Conda，不新增依赖。
