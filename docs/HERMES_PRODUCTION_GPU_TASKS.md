2026-11-08更新：正式入口已显式启用GBAR workflow，最后200点预测和历史回测均有实际GPU结果；未来案例的两项故障已修复并沿原回执恢复。当前部署/预算/接口见 HERMES_GBAR_PRODUCTION_20261108.md，下文为10月27日独立后端验收记录。

# 生产GPU模型任务开发与验收

开发纪律与地址（开发前，2026-10-27）：服务器 `/home/h3c/project/python/scat-hermes-platform` 优先开发测试；Python仅已有Anaconda scat-agent（业务/测试）、scat-hermes（原生Agent），本机base传输/scat-frontend检查，禁止venv/uv、新环境与装包。临时文件仅两端项目 `.tmp/production-gpu` 及受控原子同步暂存，结束清理；正式配置/回执/证据分别保留 `var/gpu`、`var/validation/20261027-production-gpu`，真实业务制品只留服务器。

服务地址：复用现有GBAR BentoML私有网络3000（开发前解析为172.30.67.2:3000，地址不得作为永久服务身份；同时核对固定镜像、release和模型provenance），不重启/改写该容器。正式API18081、RAG18133、七CPU容器18120–18126/后台18128及旧80/443/8080/8501/8091/27017保持。隔离开发入口预留回环18135–18138，启动前再次检查；18127/18134标准GPU候选未使用，Honcho18140–18142保持关闭。不改宿主驱动、Docker GPU运行时或模型权重。

开发前已确认：GBAR健康且metadata报告device=cuda、model_load_count=1；release为scat-gbar-v23:20261010-485de7947678-deps7d416e4aac0f-sec3。宿主nvidia-smi因NVML驱动/库版本不匹配失败，保留限制，不以此关闭正在工作GPU服务或改用CPU冒充GPU。系统盘约27.57GiB可用。本轮功能复用已运行服务，标准GPU镜像迁移仍独立验收。

## 本轮实施范围

1. 服务端显式注册GBAR v23、固定release/provenance/CUDA要求；主Agent只准备方案，实际推理由既有可信确认、租约与独立后台数值步骤触发。
2. 扩展工作流的单/多航班预测与CPU/GBAR模型对比，补受限随机候选选择。确认前冻结抽样、观测窗口、真实未来、模型参数和服务身份，不在执行/恢复时重新查询或抽样。
3. 复用现有预处理、Bento传输、单位/UTC对齐与评估内核；GBAR观测/时域限制显式校验，失败/排除和共同样本分母不隐藏。
4. 独立持久GPU请求回执和全局串行准入；每个稳定请求只发送一次POST，响应未知不自动重放，不因客户端取消就宣称远端计算已终止。补状态核查与可信人工处置入口。
5. 契约/来源/数值/并发/恢复测试、已有Anaconda完整回归、服务器实际CUDA推理及真实业务样本验证。模型测试不调用外部LLM；若需要真实Agent委托验证，先登记有限预算且只用合成内容，不外发真实轨迹。

结果与未覆盖边界在实测后追加，不以本文件或配置创建宣称完成。前端、Honcho自动学习、标准GPU容器迁移、正式网关切换不属于本轮功能实现。

## 配置与调用

新增 `services/hermes/gpu-models.v1.json` 固定既有GBAR镜像、release及provenance；已有Anaconda执行 `scripts/prepare_gpu_registration.py --enable`，只验证/登记，不改旧容器，配置写入服务器 `var/gpu/production.json`（0600）。为需要GPU的服务设置 `SCAT_GPU_CONFIG_FILE=var/gpu/production.json`。默认公共配置仍关闭，运行中的固定CPU镜像不因源码同步而更新。

GPU运行配置须使用支持多制品的新集合，例如 `ANALYSIS_RUNS_COLLECTION=analysis_runs_hermes_gpu`、`ANALYSIS_ARTIFACTS_COLLECTION=analysis_artifacts_hermes_gpu`。新集合仅artifact_id唯一，run_id/kind为普通查询索引，允许概览与多份分项。已有旧集合若仍有唯一run_id/kind索引，提案会在GPU请求前拒绝；不自动删除历史集合索引。旧入口与历史数据仍保留，跨版本集合迁移/正式切换需单独准备。

完整非敏感环境样例见 `config/gpu-runtime.env.example`：GPU采用独立catalog/任务/结果状态及集合。源代码基线包为s5-gpu.1.0，比较/实验技能0.2.0；已有活动catalog与运行镜像不自动切换。样例用于包含本轮源码且已开启工作流的运行profile，现有私有Key由原角色配置提供。

沿用HTTP工作流接口：GET `/api/v1/hermes/workflow-catalog`，POST `/api/v1/hermes/threads/{thread}/workflow-tasks` 准备，POST原model-tasks确认入口固定snapshot_sha256，GET任务及其artifact读取概览和GPU图表。主Agent仍只有准备/查询权限。

单航班/单模型使用kind=comparison、models仅scat-gbar-v23:23；对比时增加constant-velocity-heading:1或kalman-cv:1。GBAR要求min_observed_s>=350、horizons_s<=360、max_cutoffs_per_flight<=4，服务默认总窗口（含重复）<=16。支持explicit外部ID或selection=random；random仅在批次前candidate_limit（最多100）个受限服务器候选池内按seed随机，绝不宣称全库均匀抽样。模型窗口质量与未来/共同截止点在确认前检查、冻结；执行不再查询原轨迹。

GPU请求单独写入 `var/gpu/requests.sqlite`；实际POST最多4个窗口一批，不超过旧服务8条/2000源点限制，不做POST重试。running/unknown全局阻止新GPU请求；调用进程丢失时标unknown，不因TTL自动释放。可信维护者用 `scripts/manage_gpu_tasks.py status` 查看，只有独立确认远端已终止后，才用resolve-unknown/原请求ID/expected-sha256/remote-termination-confirmed收敛为failed；同请求仍不可重放。

## 实施与首轮证据

本机生成清单首轮因Windows默认GBK读中文失败，改已有Anaconda Python的UTF8模式；未产生部分清单。首轮87项测试为86通过/1失败：GPU来源嵌套对象不满足AnalysisResult context标量约束，改为各独立摘要字段并加入返回前验证，原失败保留。后续健康检查发现同步Bento health在异步提案中调用asyncio.run冲突，改为线程中检查并新增用例；修复后专项20通过。

synthetic-v1验证脚本类名导入错误、synthetic-v2异步健康检查失败均发生在GPU发送前，原日志保留。synthetic-v3真实CUDA已成功1次POST、12个GPU/12个CPU评估样本；旧Mongo唯一run/kind索引使第二份图表发布被拒绝，首失败报告保留。只对该案例独立测试集合核验并移除旧索引后复用原结果，完成概览+图表两制品发布，修复中新增GPU请求0。正式旧集合/容器未改变；新增提案前多制品索引检查及发布冲突unknown状态，避免任务假停留running。

已有scat-rag-train实测torch CUDA可用、Tesla V100-PCIE-32GB，空闲/总显存约26.04/34.07GB；NVML警告保留。不把GPU健康或单次推理当作标准GPU容器迁移。

下一项验证：真实业务样本保持服务器内，确定性驱动不调用LLM；原生Hermes子Agent仅使用合成样本。原生验收全活动最多48次LLM/200万保守单位，每任务24次/100万/48工具/600秒，含失败，不清零；最多两个原生案例，不重复成功请求，不发送真实轨迹。无新Key。

## 最终功能验收

- 最终专项27 passed，完整后端1349 passed/1 skipped（255.29秒，1项既有Starlette提示）；新增20项测试覆盖GPU契约、CUDA身份、存储预检、固定抽样/窗口、持久回执、全局串行、进程丢失、unknown及发布冲突。19个Python文件Ruff/格式通过。此前1345/1及首轮86/1均保留，不替换旧结果。
- real-v1：服务器真实SCAT两架航班、每架两个共同截止点，GBAR/CPU各12个M6样本；实际GPU POST 1次、回执calls=1，概览+GPU图表两份制品实际Mongo保存/读取，跨线程拒绝、重开状态读取不重算通过。真实轨迹未发外部LLM或下载本机。
- single-v1：真实单航班、单GBAR模型、1个截止点/3个M6样本，GPU POST 1次，完整两制品与恢复读取通过。单模型使用同一workflow机制，无旧Agent图总调度。
- native-v1：固定Hermes/DeepSeek原生后台子Agent，合成两航班，真实技能加载及sample→GPU/CPU trials→评估→诊断→报告构建/读取通过；10次LLM/11次工具/386926保守单位，GPU POST 1次，各计算单元calls=1。generation=2是终态围栏递增，不表示第二次运行。
- 加上synthetic-v3已保存且修复发布的原始推理，整轮实际GPU POST共4次，全部成功且每请求calls=1，无POST重试；原生仅一个案例，共10次外部模型请求，低于48次/200万总预留。确定性真实数据验证外部模型请求0。冻结样本、后台进程/步骤和GPU回执均可核对。
- 新集合第一次索引探测的NamespaceNotFound由驱动归为空索引后继续建表，原监控日志保留，未导致任务失败；没有因此删除任何正式旧集合索引。

本轮完成后端功能开发与服务器实际CUDA验证，复用既有GBAR/BentoML GPU容器，不修改驱动或模型权重。源码/配置与验收元数据同步；当前七CPU容器仍为原固定镜像，正式入口未启用新GPU工作流，标准GPU镜像迁移和网关/H8发布另行实施。前端仍暂停。

公共结果：`docs/validation/20261027-production-gpu-{real-v1,single-v1,native-v1,synthetic-recovery,acceptance}.json`；完整原始日志、真实轨迹图及回执仅服务器 `var/validation/20261027-production-gpu`。本轮项目测试暂存结束清理，服务器私有登记/请求账本保留 `var/gpu`。
