# GBAR 正式接入与有限预算验收

2026-11-08，Asia/Shanghai。服务器优先；仅已有 Anaconda scat-agent/scat-hermes，本机 base 传输；禁止 venv/uv、新环境和装包。两端暂存限项目 `.tmp/gbar-live`，证据 `var/validation/20261108-gbar-live`。保留服务器18120平台、18121主Agent、18126后台Agent、18128worker、18133RAG及其他既有服务。沿用户授权启用正式GPU配置并真实验收，不重启服务器、不改驱动、权重或既有Bento容器。凭据仅内存，真实轨迹和私有备份留服务器。

## 范围与预算

核对固定GBAR镜像/release/provenance/CUDA身份，五核心共享配置与持久请求账本，主后台活动技能一致，Mongo多页制品索引准入，实际批准窗口与后台回执一致。未来预测与历史回测分别验证；自然语言从正式主Agent发起，可信测试驱动检查方案后通过确认接口批准，不能将接口确认说成页面点击。

本轮至多两个真实任务、两次GPU POST（每任务一窗口），一次普通自然语言请求。每个任务家庭最多24次LLM、100万保守预留单位、80次工具、1800秒，包含已导入的主规划；总48次LLM、200万单位。失败计入、不提额、不清零；未知请求停止，不重放或另起成功替代。一次历史回测由REST准备，仍由正式独立原生后台Agent执行。成功后仅GET读取与重开恢复。保守单位不是费用或供应商token数。

## 实施与结果

接入前活动版本 `20261107-forecast-contract-2efaab19633e`，七容器健康，GPU目录关闭。只读预检 `/metadata` 和既有登记验证通过：固定GBAR v23、CUDA、model_load_count=1；历史四条GPU回执均succeeded/calls=1。

正式运行使用 `SCAT_GPU_CONFIG_FILE=var/gpu/production.json`；配置文件只读覆盖挂载，`var/gpu`目录共享读写以持久保存SQLite及其journal。沿用旧账本，不为新角色创建独立计数。旧 `SCAT_GBAR_V23_ENABLED=false` 保留以关闭旧兼容入口，正式workflow能力由registration和目录决定。

本轮正式接口接通与有限预算验收已完成；首轮失败与恢复见下文。标准GPU镜像迁移和科学精度准入独立于本轮正式接口接通。

## 正式接口

沿用带归属认证的工作流接口，GBAR不增加绕过确认的推理入口：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/v1/hermes/workflow-catalog` | 查看实际登记模型、GPU来源和每模型输入限制 |
| POST | `/api/v1/hermes/threads/{thread}/workflow-tasks` | 准备并冻结方案；body含UUID request_id、proposal，可降低budget |
| GET | `/api/v1/hermes/threads/{thread}/model-tasks/{task}` | 查看批准摘要、精确窗口、后台步骤、累计预算和回执 |
| POST | 上述任务路径加 `/confirm` | body含 `confirmed:true` 和原 `snapshot_sha256`；受信任客户端确认 |
| GET | 上述任务路径加 `/artifacts/{artifact}` | 读取原制品；重读不重新计算，跨线程返回404 |

未来预测的proposal示例：

```json
{
  "kind": "forecast",
  "batch": "scat20161015_20161021",
  "external_ids": ["100002"],
  "max_flights": 1,
  "models": [{"model": "scat-gbar-v23", "version": "23"}],
  "observation_last_n": 200,
  "forecast_origin": "last_observation",
  "min_observed_s": 350,
  "max_cutoffs_per_flight": 1,
  "horizons_s": [10, 30, 60]
}
```

历史回测改用`kind=comparison`、`forecast_origin=historical_cutoffs`，本轮单起点回测不指定observation_last_n。GBAR不接受window_s/噪声参数；输入不足或不满足预处理则拒绝，不补读扩大已批准精确窗口。响应未知时保留占用并核实原请求，不自动重发POST。

## 真实失败与修复

首轮自然语言正确选择了forecast/GBAR v23/最后200点，但准备返回RuntimeError，任务与GPU增量均0。原因是`AnalysisArtifactRepository.require_multipage`先取未初始化的Mongo集合，独立工具进程没有API热连接。补为非注入仓库先await connect，再检查索引；新增两个冷启动用例分别覆盖合法索引与旧唯一索引拒绝。14项相邻专项通过，候选/正式验证改为真实新进程冷检查，不再预先connect掩盖问题。

恢复驱动只接受原run的唯一已审计prepare参数摘要、scope、技能版本及原proposal_key，核对原scope尚无任务、原规划预算已终态后，使用相同工具handler恢复准备；没有新增LLM或GPU请求。初轮失败与修复后的恢复分开记录，不将其说成初轮自然语言全链成功。确认由可信REST测试驱动完成，不是本轮浏览器点击验收。

真实CUDA推理已成功后，原适配器以36行全None表示无真值，旧forecast后处理错误地将非空数组当作存在真值，后端因此unknown。永久修复仅接受全空占位并规范为空expected_values；任何非空真值或actual_future_trajectory仍拒绝。新增空数组/全空行/意外非空真值三类契约；首轮新增异常断言误用安全错误文字，改为核对BackendError.code，12专项通过。

只针对原任务8121b9d0764e4ff584d4146fe2b57a39恢复：核对原GPU成功回执的请求/输出摘要、原冻结200点、同一执行号、后台进程已退出和原批准源码；备份私有任务状态后，从原响应规范空真值并产生后处理结果。GPU原始output和SHA、calls=1、批准snapshot SHA均未改。原任务独占租约内复用计算结果并完成六个可信报告步骤，新增LLM/GPU均0；六步骤不经过原生模型工具计数，审计单独列出，不冒称预算账本已计入。原生后台实际执行过一次，报告由维护恢复完成，不将其说成原生后台初轮全链成功。

自动审批曾拒绝额外暂停核心worker，操作未执行；随后改用仅锁定原任务的恢复租约，所有服务保持运行。首次维护脚本同步健康函数误置async循环，未改任务状态、未调用GPU；改为既有线程桥后恢复成功，原失败日志保留。初轮完整回归另发现旧目录测试夹具没有新增max_windows_per_job字段，修正夹具及新增限制断言后重跑，原1489通过/1失败/2跳过保留。

最终发布版本`20261108-gbar-production-e87fd9c4237b`，活动技能包`2b640dc9f6788b35748d313e147ad7852526206de87d9ae3bd953b23757fb0cc`。比较/评估/报告沿用已发布0.3.0/0.2.0/0.2.0，仅补实验技能0.2.0；固定旧GBAR兼容开关不启用。最终镜像的纯后处理还会复用同一CUDA响应验证摘要相等，禁止网络预测方法，以零新增POST核对真实全空占位兼容，不重跑已经成功的GPU计算。


## 最终验收与交接

| 案例 | 真实输入和结果 | 执行证据 |
| --- | --- | --- |
| 末点未来预测 | 最后200点→固定36时刻；10/30/60秒三个目标；无真值、误差样本0、摘要及轨迹两制品 | task 8121b9d0764e4ff584d4146fe2b57a39；原准备和原生后处理失败分别恢复，无GPU重算 |
| 历史回测 | 同一真实航班、单历史起点，71原始点→36时刻；3个M6样本、真实误差及两制品 | task a1f321d6e9bd41699dbfe5f71fa34af0；REST准备确认、正式原生后台全链成功 |

两案例均succeeded，实际GPU POST共2次、各calls=1。最终镜像禁止predict方法并复用原CUDA响应的检查通过，与恢复结果后处理摘要完全相等，新增POST0。原批准快照、输入、步骤和GPU响应摘要均核对；主后台81项实现指纹一致。正式制品GET重复读取摘要一致，跨线程404；本轮没有浏览器点击或视觉验收。

最终完整后端1492 passed/2 skipped（274.29秒），保留1项既有Starlette提示；143相关专项、14冷启动相邻专项、最终12空真值/目录专项通过。源码及维护脚本Ruff/格式通过，没有安装依赖或建立环境；前端未改动、未重测。

预算含原规划只导入一次：23次LLM、27次原生工具、1293388保守单位；另6次可信恢复步骤，合计33次工具/恢复操作。供应商input=339557、output=3890 tokens；保守单位不是费用。48次/200万上限未提高、未耗尽，没有清零、重复规划或重发成功GPU请求。

七容器healthy，服务器启动标识未变，原GBAR和两作者容器未重启，学习调度active。33份私有备份均0600/quick_check通过；真实输入、结果和备份保留服务器。本轮暂存清理，其他预览与状态保留；标准GPU镜像迁移和模型科学精度准入另列。

机器入口：docs/validation/20261108-gbar-production-acceptance.json；证据var/validation/20261108-gbar-live中的acceptance-audit.json、cached-forecast-final-image.json、closeout-server.json。失败日志、初轮发布及回滚配置保留。双端源码/文档/脱敏证据同步见source-sync.json；不得下载同目录私有SQLite备份。
