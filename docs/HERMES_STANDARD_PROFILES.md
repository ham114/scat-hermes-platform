# Hermes 标准 Profiles

开发纪律与地址（2026-10-24，开发前）：服务器项目 `/home/h3c/project/python/scat-hermes-platform`，仅已有Anaconda scat-agent/scat-hermes，本机scat-frontend检查，SSH运输已有base Paramiko。禁止venv/uv/新环境，临时目录两端项目`.tmp/profiles`，结束清理；回环18108主Agent、18109作者Agent、18110平台、18111后台、18112作者后端，启动前重查空闲。现网不变，前端暂停，外部模型只用合成输入。

目标：使用固定Hermes的原生命名profile创建/发现接口，在项目内集中管理主对话scat-main、后台scat-worker、技能作者scat-author；标准目录、SOUL.md、AGENTS.md、独立状态和凭据；profile_manage统一管理且不向普通业务Agent开放管理权限。后台按任务和代次创建标准布局的实例，防止任务共享会话状态。现有显式home与历史profile保留兼容。

现有Key可复用，无需新申请；若分角色隔离，提供商Key标签建议scat-hermes-main、scat-hermes-worker、scat-hermes-author，共3个（保留现有main时新增2个）。每个私有profile的.env变量都使用DEEPSEEK_API_KEY，不在对话里发送，不把数据库/业务AUTH_SECRET复制给Agent。profile_manage不返回密钥值。

## 布局与生效方式

正式根目录为服务器 `/home/h3c/project/python/scat-hermes-platform/var/hermes/managed`，通过固定Hermes原生create_profile/list_profiles创建与发现三个命名profile；不写`~/.hermes`、不创建用户目录别名，不启用上游安装器。原生创建的同目录原子暂存立即重命名，通用临时目录为项目`.tmp/profiles`。

```text
var/hermes/managed/
├── config.yaml                  # 原生命名profiles的项目根
└── profiles/
    ├── scat-main/               # 主对话
    ├── scat-worker/             # 后台角色母版
    └── scat-author/             # 技能作者
        ├── config.yaml          # 模型、温度、轮次、工具与记忆配置
        ├── .env                 # 仅模型凭据，0600，Git忽略
        ├── SOUL.md              # 角色身份，原生加载
        ├── AGENTS.md            # 操作指令；上游识别复数文件名
        ├── profile.yaml         # 原生命名profile描述
        ├── memories/
        │   ├── MEMORY.md
        │   └── USER.md
        ├── sessions/
        ├── state.db             # 由原生SessionDB创建真实schema
        ├── skills/
        ├── cron/
        ├── logs/
        ├── workspace/AGENTS.md
        └── skins/
```

三角色均有上述目录；后台每个task/generation另建完整标准布局的实例，从scat-worker复制角色指令与对应模型凭据，独立state.db、会话、workspace和预算，不复制历史或用户记忆。母版不直接执行效果，启动必须有可信任务/租约；没有长期共享的万能worker。

主对话仍是技能读取、只读业务与任务准备工具组；作者Agent实际只注册`skill_draft`与只读`profile_manage`，可以读已发布目录/正文、创建修订草稿、查询差异/历史、校验、申请隔离试跑及查状态；没有发布/回滚确认、profile修改或业务效果工具。发布服务仍使用另一私有凭据，作者Agent不读取它。

`config.yaml`中model及agent.temperature/max_turns、memory配置实际用于启动；temperature映射到原生request_overrides，轮次仍受平台预算上限限制。工具组按角色和可信任务强制生成，改YAML不能扩大权限；活跃profile拒绝管理器修改。SOUL原生读取，AGENTS在构造Agent前传入且workspace有相同指令。长期记忆、用户画像和cron默认不开启；完整目录不代表自动开放这些功能。

## 统一管理命令

以下均在服务器项目目录执行，Python固定已有`scat-hermes`，不使用venv/uv：

```bash
/home/h3c/anaconda3/envs/scat-hermes/bin/python -B scripts/profile_manage.py init --reuse-root-key
/home/h3c/anaconda3/envs/scat-hermes/bin/python -B scripts/profile_manage.py list
/home/h3c/anaconda3/envs/scat-hermes/bin/python -B scripts/profile_manage.py show --profile scat-main
/home/h3c/anaconda3/envs/scat-hermes/bin/python -B scripts/profile_manage.py doctor
```

`init`原生创建缺少的三角色，已有受管理profile不覆盖人格/配置/历史；未知同名目录拒绝接管。`--reuse-root-key`只将根.env已有DeepSeek Key写入缺少Key的私有profile.env，不复制数据库或业务认证；已有非空Key不替换。默认不复制凭据。列表、检查和模型工具只返回配置/摘要及key_configured布尔值，不返回.env、会话或记忆。

修改模型参数先show取得`config_sha256`，然后：

```bash
/home/h3c/anaconda3/envs/scat-hermes/bin/python -B scripts/profile_manage.py set --profile scat-main --expected-sha256 <上次配置摘要> --temperature 0.3 --max-turns 12
```

支持`--model`选择当前DeepSeek端点可用模型；目前不开放任意新供应商/外部URL、任意工具/脚本、profile删除或跨用户管理。配置冲突或profile已运行时拒绝修改。`SCAT_HERMES_PROFILE_ROOT`可指定另一个项目相对根目录，维护与运行须使用同一值。

启动主对话使用`run_hermes_api.py --profile scat-main`，默认端口18081；作者使用`--profile scat-author --port 18109`，并先由已有scat-agent启动`run_skill_author_api.py --port 18112`。显式旧`--home`保持兼容，未指定home的新默认选择scat-main。测试可用`--profile ... --home .tmp/profiles/...`隔离运行；关闭后不会清理正式profile。服务本轮按需验收，不自动常驻、不改Nginx。

## API Key 放置

本轮三个profile已复用现有DeepSeek Key，因此**额外申请数量为0**。可选独立Key标签及路径如下；每个文件中都填同一个标准变量名：

| 提供商Key标签 | 服务器私有文件 |
| --- | --- |
| `scat-hermes-main` | `/home/h3c/project/python/scat-hermes-platform/var/hermes/managed/profiles/scat-main/.env` |
| `scat-hermes-worker` | `/home/h3c/project/python/scat-hermes-platform/var/hermes/managed/profiles/scat-worker/.env` |
| `scat-hermes-author` | `/home/h3c/project/python/scat-hermes-platform/var/hermes/managed/profiles/scat-author/.env` |

```dotenv
DEEPSEEK_API_KEY=在服务器文件中粘贴对应Key
```

Key直接在服务器文件内填写，保持0600，不发到对话、不填入SOUL/config.yaml或模板`.env.example`。不需要每个后台子任务另申请Key：它使用worker角色Key，任务状态仍独立。若保留现有Key用于main，想完全分角色时只新增worker和author两个Key；三个Key依然可能共享提供商账号额度。

内部`var/secrets/hermes-api.key`、`skill-author.key`、`skill-release.key`是本项目生成的随机服务凭据，不是模型提供商Key，不需要额外申请。作者只持作者服务凭据，发布凭据只给独立用户确认入口。`.gitignore`已覆盖var/、**/.env及私有.key。

## 验收结果

- 服务器专项60 passed，包含新增27条profile设置、路径、运行锁、草稿HTTP权限、只读管理及预算测试；原生创建/发现/真实SQLite schema/凭据隔离/重复运行拒绝/配置修改探针通过。
- 完整后端 **1120 passed/1 skipped**（239.94秒）；Ruff/格式/native轻量导入及两Conda pip check通过。没有新增环境或依赖。
- 真实主Agent调查3个实际只读操作；真实后台CPU任务批准后执行，backend calls=1，结果与引用复核通过。两场景共20次LLM/19次工具，保守1071449，provider input280970/output2980/cache248576（cache为input子集）。使用标准main及worker角色，后台实例目录/状态隔离。
- 真实作者Agent实际调用profile_manage、已发布目录、新建草稿、validate；4次LLM/4次工具，保守96744；实际工具集合仅profile_manage/skill_draft、temperature=0.4、发布事件0。先前S7-A只有作者API，本轮已补真正独立作者Agent。这里是每场景一次接入验证，不作为重复稳定性SLA。
- 正式三profile完整结构、0600私有.env及原生state.db已保留，当前实际供应商Key数量1。1318文本扫描实际私有值0；前端8文件未变，原六服务保持，18108–18112关闭、本轮进程0、.tmp/profiles清理。长期记忆与cron仍关闭，未常驻、不改现网、不发布新业务技能包。

仅元数据同步本机：[总验收](validation/20261024-profiles-acceptance.json)、[原生profile验收](validation/20261024-profiles-native.json)、[真实角色接入](validation/20261024-profiles-runtime.json)。原始后端/模型日志保留服务器var/validation与docs/validation，完整回归驱动3d8a3ed40b、合成主后台报告20261024-profiles-flow-30f6678d07.json。自动审批拦截整段字段导出后已改用限定布尔/数字，未扩大真实数据外发。
