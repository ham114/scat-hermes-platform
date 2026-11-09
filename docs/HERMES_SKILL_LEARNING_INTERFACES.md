# 从记忆/任务经验到技能草稿：接口准备

开发纪律与地址（2026-10-27）：服务器 `/home/h3c/project/python/scat-hermes-platform` 优先，Python仅已有Anaconda scat-agent；本机base传输/scat-frontend检查，禁venv/uv、新环境、装包。临时文件仅两端项目 `.tmp/skill-learning`，结束清理；证据留服务器 `var/validation/20261027-skill-learning`。复用私有作者应用结构，当前候选作者API地址 `http://127.0.0.1:18124`；本轮进程内隔离测试，不新增监听、不重建现有容器。正式18081/18133、七CPU容器、旧GBAR保持；Honcho18140/Embedding18141/代理18142保持关闭。无外部模型调用，不读取真实记忆或私有Key。

## 范围

本轮提供经验候选提交、读取与幂等保存生成草稿的后端接口，以及作者Agent的对应工具操作。作者仍负责理解摘要并编写技能正文，HTTP接口只保存调用方给出的结构化content，不在请求中启动模型。自动从Honcho或任务账本采集、判断学习时机、筛选成功经验、跨用户身份映射和定时调度尚未接入。运行中的容器使用固定旧镜像，新路由须后续候选镜像构建/部署后才可通过18124访问；源码同步不代表该运行服务已更新。

无需新增API Key，沿用作者服务凭据 `X-SCAT-Author-Key`。发布接口继续要求独立 `X-SCAT-Release-Key`，该凭据不交给作者Agent。

## 数据与权限

- 候选不可修改，包含purpose和1–8条evidence，每条为kind、reference、revision、summary。kind仅支持honcho_memory/task_review；reference与revision是调用方提交的来源标识，本接口不访问URL、文件、任务库或记忆库，也不核验业务成功。摘要必须由后续可信采集适配器先做权限检查、筛选和脱敏。
- purpose最多2048字节；reference256字节、revision128字节、summary4096字节；禁止空白、NUL、同一kind/reference/revision重复。每个服务端actor最多100个候选，列表最多100条且不含摘要正文。候选原文保存在现有作者SQLite状态库，沿用项目私有存储权限；后续接入真实记忆前需完成留存/删除治理。
- `sha`为规范化purpose+完整evidence快照的SHA256。证据顺序归一化，同actor的相同快照去重；新来源版本或新摘要形成新候选。不同actor隔离。actor由服务配置/鉴权依赖提供，不能由请求正文指定；现有服务仍是固定actor模式，不把此接口宣称为多租户身份适配。
- 创建候选时固定当前技能包base。后续生成草稿沿用该base，不因活动包变化静默迁移。已有草稿需要换基线时使用既有修订接口并重新校验/试跑。
- 来源摘要作为不可信参考数据。作者工具不能提交候选、读取真实记忆、增加可用工具或发布。操作权限与技能契约仍由原有校验器约束；保存草稿不等于校验通过。

## HTTP接口

| 方法/路径 | 请求与结果 |
| --- | --- |
| `POST /v1/skills/learning/candidates` | purpose、evidence；返回id/sha/base/来源快照/state=pending/replayed。重复提交返回原候选，HTTP201，replayed=true。 |
| `GET /v1/skills/learning/candidates` | 当前actor候选列表，包含id/sha/base/created/draft/revision，不返回证据正文。 |
| `GET /v1/skills/learning/candidates/{id}` | 读取不可变来源快照与原始草稿关联，evidence_status恒为supplied_snapshot，不代表来源真实性已核验。 |
| `POST /v1/skills/learning/candidates/{id}/draft` | expected_sha256、content；在同一SQLite事务内创建既有格式的草稿并关联来源，返回candidate/draft/replayed。 |

候选生成草稿后state=drafted，记录原始draft及revision=1。相同candidate、sha和content重复提交返回原始修订，不生成第二份；不同content冲突，改稿应使用原有PUT草稿接口。即使草稿已被修订，重试仍返回最初关联的修订，完整历史由草稿接口读取。publication_triggered=false只表示这条接口未触发发布，不代表以后该草稿不可能经独立流程发布。

鉴权缺失401、身份不可见/不存在404、摘要或幂等内容冲突及容量/业务约束409、请求类型/额外字段等契约问题422。无传入路径、任意服务URL、发布确认或“客户端自报试跑成功”的扩展参数。

### 合成候选示例

```json
{
  "purpose": "将重复成功的航班查询步骤整理成可复用技能",
  "evidence": [
    {
      "kind": "task_review",
      "reference": "synthetic-task-001",
      "revision": "review-v1",
      "summary": "先检查航班是否存在，再查询；不存在时停止并说明。"
    },
    {
      "kind": "honcho_memory",
      "reference": "synthetic-observation-001",
      "revision": "observation-v2",
      "summary": "优先用简体中文解释结果。"
    }
  ]
}
```

生成草稿请求为 `{"expected_sha256":"候选sha","content":{"changes":[...]}}`。changes中的name/version/allowed_operations/requires_skills/contract/files沿用[S7-A](HERMES_S7A_AUTHORING.md)格式，必须由作者编写完整SKILL.md；此请求不接受纯自然语言作为可执行技能。

## 作者调用链

`skill_draft(operation=learning_list)` → `learning_read(identity)` → 阅读候选与已发布技能 → 作者生成content → `learning_draft(identity, expected_sha256, content)` → 既有`validate` → 既有`trial/status` → 独立入口发布预览/确认。

已有draft时先读取、继续既有草稿，不重复生成。新三个操作沿用作者绑定会话及共享工具预算；主Agent/worker工具集不扩展。learning_draft不自动校验、试跑或发布，调用者需依据真实回执推进后续步骤。

## 验证与后续

本轮无模型调用的验证覆盖HTTP鉴权/严格字段、来源版本与去重、并发生成、来源与草稿事务回滚、原始修订重启恢复、基线固定、容量以及作者工具实际路由到隔离API。注入摘要不能让未注册terminal操作通过校验。该证据不代表已验收模型自主筛选或自动写作质量。

服务器首轮专项：新接口21项，加原作者/标准profile相关回归，共79 passed（16.999秒，1项既有Starlette提示）；完整后端1329 passed/1 skipped（252.57秒，同一既有提示）。所有pytest临时目录显式设在项目内，结束清理，原始日志/XML留上述验证目录；公共验收见[机器报告](validation/20261027-skill-learning-acceptance.json)。本机conda.exe查询因系统DLL重定位错误失败，改用已有base Python执行`-m conda env list`成功，未修系统、未安装依赖；六个改动Python文件Ruff与格式检查通过。八个原有容器与三套Conda history保持，运行18124新路由仍404，待后续镜像部署生效。

后续接入顺序：可信用户/任务/记忆来源适配与脱敏 → 学习启用参数及事件/次数阈值 → 成功任务筛选与去重策略 → 有限预算作者会话调度 → 真实模型生成质量和隔离试跑验收 → 候选镜像部署。生成草稿可自动化，发布沿用现有独立确认。
