# H7-A 后端运行与部署准备

开发纪律与服务地址（2026-10-24，开发前）：服务器 /home/h3c/project/python/scat-hermes-platform；Python 仅已有 Anaconda scat-agent/scat-hermes，本机检查用 scat-frontend，SSH 传输用已有 base Paramiko。禁止 venv/uv/新环境和自动安装。临时文件只在两端项目 .tmp/h7a；正式状态/私有日志在 var/backend。回环18120平台、18121主Hermes、18122按任务启动的后台Hermes；启动前核查端口，验收后停止。现网六服务、Nginx、数据库配置和前端不改，生产GPU关闭。

本轮范围：服务级 Conda/pip 重建输入、复用主/后台标准 profile、进程与就绪状态分别检查、项目内启停、日志轮转/磁盘阈值，以及进程异常与状态恢复验证。作者入口继续独立管理。H7 的容器镜像、容器重建、正式网关切换和 H8 总体验收继续独立开展；S7-B 任意脚本执行不在本轮开放。

服务器系统盘目前95%，约43GiB可用。本轮不创建镜像层、不迁移 Docker 数据、不删除其他服务文件。依赖重建输入不等于已经在新环境完成重建；仅在已有环境核验，不新建环境。

## 依赖输入与核验

`environments/backend-h7a/` 下按 scat-agent、scat-hermes 分别保存：

- `conda-explicit.txt`：已安装 Conda 制品的公共 HTTPS 地址及 SHA256/MD5，包含 Python 的确切构建。
- `pip-requirements.txt`：由 pip 管理的分发包确切版本，排除 Conda 已拥有的包，避免混合安装覆盖。
- `inventory.json`：版本、来源和固定上游提交；根 manifest 保存各环境计数与库存摘要。

只接受已列明的公共 Conda 源；包含认证、查询参数、私有源或未知本地分发的环境拒绝导出。Conda noarch 的 site-packages 路径按当前 Python 版本映射。平台环境中遗留的 agent-service-toolkit 可编辑安装被明确列为排除项，新服务通过当前项目的 src 启动；不引用旧项目路径。Hermes 可编辑来源仅允许项目 vendor/hermes-agent，提交以既有 upstream.lock.json 固定。

```bash
/home/h3c/anaconda3/envs/scat-agent/bin/python -B scripts/manage_backend_environment.py snapshot
/home/h3c/anaconda3/envs/scat-agent/bin/python -B scripts/manage_backend_environment.py verify
```

snapshot 用于明确刷新依赖输入；verify 逐项比较实际环境和三份输入内容，可发现 pins 被修改。命令均不安装依赖、不创建环境、不读取凭据输出。当前 scat-agent 为26个Conda包/240个pip包；scat-hermes为23个Conda包/85个pip包及1份固定Hermes源码。

这是 Linux 服务重建输入，尚未在另一个环境实际重建。pip 下载制品的逐文件哈希及离线镜像仓库未完成；固定版本也不能保证未来下载源永久可用。H7-B 构建时需补齐制品锁定、来源可用性与实际重建证据；不能直接在旧平台共用环境里升级依赖充当部署验收。

## 候选服务管理

```bash
/home/h3c/anaconda3/envs/scat-agent/bin/python -B scripts/manage_backend.py preflight
/home/h3c/anaconda3/envs/scat-agent/bin/python -B scripts/manage_backend.py start
/home/h3c/anaconda3/envs/scat-agent/bin/python -B scripts/manage_backend.py status
/home/h3c/anaconda3/envs/scat-agent/bin/python -B scripts/manage_backend.py stop
```

默认读取 `config/backend-h7a.json`。可调整启动/停止宽限期、健康检查周期、最长运行时长、磁盘阈值和日志容量。端口限定18120–18139、彼此不同，实例名限定h7a及其后缀；配置不能注入命令、任意工具或模型供应商。默认最长8小时，上限24小时；这是候选运行管理，不安装开机自启。

| 组件 | 地址 | 解释器与职责 |
| --- | --- | --- |
| 平台 API | 127.0.0.1:18120 | scat-agent；可信身份、任务确认、结果读取 |
| 主 Hermes | 127.0.0.1:18121 | scat-hermes；scat-main 母版的独立实例 |
| worker | 无常驻 TCP | scat-agent；单并发领取已批准任务 |
| 后台 Hermes | 按任务临时占用127.0.0.1:18122 | scat-hermes；scat-worker 的任务/代次实例 |

候选始终使用合成 MCP 和 CPU 夹具，生产GPU开关强制关闭；仍须人工确认任务，启动不触发模型请求或计算。已存在的请求/技能预算和后台单并发规则继续适用；本轮未新增供应商级全局限流、RAG显存治理或生产GPU并发能力。

状态/数据库/各服务输出日志位于 `var/backend/h7a/`，后台实例位于其中 jobs 目录；复用模板时不覆盖正式的三个 profile。平台认证独立生成 `platform-auth.key`，权限0600，重启保持稳定；命令和状态不输出值。私有文件仅留服务器，不新增供应商API Key。

## 健康、停止与恢复语义

`platform_live`、`platform_ready`、`hermes_live`、`worker_alive`分别报告。平台 ready 检查 Mongo、Hermes 与配置；worker_alive只证明进程存活，任务完成仍须核查真实run/artifact。启动阶段显示starting；首次就绪后依赖失联显示degraded，恢复后返回ready，不能把无响应当作进程已退出。

子进程实际退出或日志写入失败时，管理器停止该候选组并保存failed原因；不无限自动重启。启动使用文件锁，重复启动拒绝。PID与内核启动标识、boot ID、项目脚本和控制令牌一起核对；不凭旧PID终止进程。管理器遭SIGKILL后显示stale，须stop核实并清理其仍存活的子进程，不能直接启动第二份。start超时也会停止本次候选。

stop先关闭新请求入口，再停止worker和Hermes，保留数据库与状态。它不删除/重排任务、不等于取消业务效果；已有独立CPU执行可能继续，仍按S4/S5持久执行号与unknown核实规则恢复。因此管理器不自动删除worker临时目录。清理前须核实进程、执行号与租约；正常使用时也不能手工删库来消除unknown。

三个服务的 stdout/stderr 各默认4MiB、保留3个备份，私有0600，最大约48MiB；原生内部文件、历史验收日志和业务制品不属于这组三输出的轮转范围。磁盘默认少于2GiB可用或使用率达到98%时拒绝启动/停止候选，90%起告警；按非root可用空间计算，与df口径一致。参数在有界配置范围内调整，不自动清理其他目录。

## 验证与已知边界

首轮完整后端1146 passed/1 skipped。首轮合成进程探针已启动服务并拒绝重复启动，但清理误引用不存在的Motor，导致原失败细节未落盘；保留其私有日志及固定失败元数据。已改用现有PyMongo，并修复已就绪服务失联仍被初次启动宽限期掩盖的问题；补充日志写入失败上报及输入文件篡改检测。第二轮在快速重启时启动失败，端口准入增加SO_REUSEADDR后第三轮15项全部通过，限定错误元数据和失败日志保留，不据旧失败推断成功。

最终完整后端 **1147 passed/1 skipped**（pytest237.69秒），新增27项运维边界用例；Ruff、格式、输入核验、两Conda pip check通过。真实生命周期15项覆盖启动/重复拒绝、认证/归属、依赖降级恢复、持久线程/合成回执恢复、子进程退出、控制器中断与核实孤儿清理。原六服务与55份业务执行源码、8份前端基线不变；本轮进程0、18120–18122关闭，13个临时链接仅解除自身，.tmp/h7a已清理；1463文本私有值扫描0。

证据：[总验收](validation/20261024-H7A-acceptance.json)、[最终回归](validation/20261024-H7A-e2664d1dad-checks.json)、[15项真实进程检查](validation/20261024-H7A-24383a1d-lifecycle.json)、[首轮失败记录](validation/20261024-H7A-first-probe-failure.json)、[第二轮恢复失败](validation/20261024-H7A-a225dc6a-lifecycle.json)。这些是固定元数据，原始日志留服务器var/validation；没有导出私有配置或业务结果。

本轮重启验证使用通过API创建的线程，以及由维护侧存储接口注入的明确合成结果回执；回读走真实认证API，覆盖跨线程和错误Key拒绝。没有将该夹具宣称为新的模型计算或付费LLM业务稳定性评测。真实长任务恢复沿用既有S4/S5验收，生产GPU、多用户登录、长期记忆、cron、脚本沙箱和前端H6继续独立规划。

## 后续后端工作

1. H7-B：补齐pip制品哈希和实际重建，生成固定CPU/DeepSeek服务镜像与Compose，在独立入口验容器重建、状态卷、健康及故障恢复；宿主回环Mongo访问须单独设计。系统盘和Docker存储条件确认后再构建，不直接套旧项目模板。
2. H6后端相关项：完整结构化报告/旧多图协议转换、RAG出处与数值口径的最终质量复核。界面展示仍暂停，不能因S6题集通过就勾选整个H6。
3. H8：依赖/许可审查、真实业务冒烟、备份恢复、完整交付与正式切换/回滚，未验收前保持隔离入口。
4. 可选扩展：S7-B薄脚本及沙箱、长期记忆/cron、多用户登录与profile隔离、生产GPU；这些项目不因标准目录已创建而自动开放。
