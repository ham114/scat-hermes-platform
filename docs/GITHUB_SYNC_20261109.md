# GitHub 首次源码同步记录

日期：2026-11-09。

目标仓库：`https://github.com/ham114/scat-hermes-platform.git`，目标分支：`main`。同步前通过 GitHub API 核对仓库为私有，当前登录账号具备推送权限；远端尚无提交。本地原目录也没有 Git 历史，因此本次建立当前进展的首次源码快照，不虚构此前的开发提交历史。

## 本次内容

- 重写根目录 README，说明已实现功能、Agent 与技能、架构、目录、环境、运行入口、既有验证记录和未完成准入事项。
- 原 README 的阶段记录保留在 [历史归档](README_HISTORY_20261109.md)，当前进展以 [上线审查](RELEASE_READINESS_REVIEW_20261109.md) 和 [最近实施记录](HERMES_RELEASE_READINESS_20261108.md) 为准。
- 同步应用源码、前端源码与依赖锁、测试、技能、配置模板、部署脚本、环境锁及文档。未修改业务实现，也未操作生产服务。
- 新增 `.gitattributes` 统一仓库文本行尾，Windows 批处理保留 CRLF，减少跨平台检出差异。
- 使用现有 Git Credential Manager 登录，不将 GitHub 凭据写入仓库、远端 URL 或文档。

## 排除与检查

- 沿用 `.gitignore` 排除 `.env`、私有密钥、`var/` 运行状态与账本、临时目录、依赖目录、数据库、数据集、权重和镜像归档。
- 外置 `vendor/hermes-agent/` 不上传；上游提交与源码摘要保留在 `services/hermes/`，新环境须单独还原。
- 对待提交文本执行凭据模式扫描，并检查是否包含本机实际私有配置值。初次命中已逐项核对为测试用例或代码模板，未发现实际配置值被复制到待提交文件。
- 检查新 README 的本地文档链接与代码围栏。本轮为文档整理和源码归档，不重新执行收费模型、GPU 或生产验收；README 中的测试数来自已有记录。

远端同步采用普通首次提交与推送，不强制覆盖历史。同步后以远端 `main` 的提交 SHA 与本地 `HEAD` 一致作为完成依据，机器结果保留在本机不入 Git 的 `var/validation/20261109-github-sync/`。
