后续更新（2026-11-08）：当前后端/门户已升级，最新版本、五条真实自然语言验收和页面指令见 [业务补齐轮记录](HERMES_BUSINESS_PARITY_ROUND_20261108.md)。本文保留上一轮证据与历史发布值，不作为当前活动指针。

# 平台入口、管理与记忆收口交接

2026-11-08，Asia/Shanghai。用户授权继续下一步，并选择新增独立HTTPS入口、复用现有登录、保留旧站。选定范围已实施并验收，完整平台H8仍有下述独立准入项；不把单链成功或52个工具映射等同全量产品对等。

## 当前入口与版本

- 正式工作台：https://172.26.67.148:8444/ 。无需本机Vite或SSH代理常驻。
- 新入口复用现有证书和 `/etc/scat/users.htpasswd`，当前限定已确认的 **HYN**。后台仍为部署单用户 `scat-researcher`，没有冒充多用户隔离。Nginx用已鉴权的remote_user覆盖内部身份头；其他用户名403，浏览器伪造该头不会改变身份。原443网站、`/assistant/`、原账号文件均不修改。
- 管理后端 `20261108-management-3f6ee1370128`，前端 `20261108-portal-a3e9aa99bdf5`；前端初版a66588b9d03d保留可回退。
- 网关：新Nginx站点8444 → 仅回环 `127.0.0.1:18158` 静态/受限代理 → 平台18120、独立作者API18124。业务Bearer仅在服务端进程内存，网站Basic登录不转发到后台；作者、发布凭据仍独立。
- 七核心健康，三个原生Agent容器和GBAR容器ID未变；原学习调度、记忆代理、新门户均active。Nginx只平滑reload，启动标识不变，服务器没有重启。

## 已完成与真实证据

| 功能 | 实际完成及边界 |
| --- | --- |
| 普通自然语言预测 | 一次普通问题，自动准备正确GBAR方案，页面确认一次，持久队列queued，独立原生后台自动完成六步骤；无维护补写或恢复 |
| 精确窗口/评估/展图 | 最后200原始点，模型重采样36步，从末点预测10/30/60秒；无真值、误差样本/指标0；摘要及轨迹两制品，图中40点，2D/3D与刷新读取成功 |
| 管理增强正式发布 | 新记忆corrections GET、删除状态/学习统计，技能catalog和settings路由已进入真实镜像；三角色只读挂载、八设置分区、提案持久目录均验证 |
| 管理权限与生效 | 无凭据401，缺独立发布凭据的提案403，真实preview成功且不应用配置；页面明确磁盘配置尚未核实运行生效。没有为验收更改用户配置或发布技能 |
| Honcho出口 | 宿主DNS仍失败，指定解析器与TLS验证成功；模型代理改为固定CPU镜像的项目容器，DNS仅影响它，不改宿主DNS或Docker daemon |
| 真实记忆效果 | 两个全新合成用户，公里/海里召回、隔离、更正为米、适配器重开读取、两次API自动删除completed均通过；实际Honcho，API驱动为进程内可信合成身份，不是主Agent采纳质量评测 |
| 对话展示调整 | 支持“请将当前结果改成双列”“把图表改成柱状图”“隐藏第2项”等有限展示语句；多结果先点击目标，切会话清目标。只改布局并按结果/摘要保存，不改数据或调用模型；数据查询不误拦为布局命令 |
| 旧业务核查 | 正式镜像17个SCAT/UAV底层只读操作均ok，新增LLM/GBAR推理0；不是17条自然语言或52项业务全量通过。完整矩阵见下文文件 |
| 正式HTTPS | HYN真实登录后桌面/手机检查轨迹40点、三维、对话布局及刷新、settings与12技能目录均通过；无写请求、无浏览器Bearer、无页面异常、凭据未入浏览器存储 |

自然语言任务 `7b0dbcfd9cf846898ed3183b44a13e18`，线程 `42297b89-54c3-469d-94c0-13bccae5b9e2`，规划 `run_faca41f09b064803bf7a0d5a25b9c9da`。批准快照、冻结输入、步骤与后端输出摘要全部核验；原8份GPU回执不变，新实际GPU POST恰好1次、calls=1，未重算旧成功结果。主/后台共享账本含父规划一次：14次LLM、16工具、782842保守单位，低于原100万后台上限；主角色原48次/200万配置未提高。

记忆另用持久campaign硬限制24次/750000保守单位及到期时间，失败和unknown照计，重启不重置，角色共享原子准入；全局每日/累计240次与800万封顶不变。本轮实际3次/17898单位，全部成功；结束时累计129次/6940664单位，campaign停止使用但记录保留，原封顶不重置。保守计量不是费用或供应商账单token。

记忆检查仅证明本次摘要关键词可见、指定隔离及清空流程，不证明任意事实已遗忘、旧偏好否定的语义正确性、真实主Agent采纳或长期准确率。真实用户记忆正文未读取或修改。

## 测试与失败保留

最终：服务器全后端1498 passed/2 skipped（279.95秒），另新增门户3专项通过；前端166项、普通桌面/手机36项通过；真实自然语言GPU1项、管理页面只读2项、对话展示只读2项及最终正式HTTPS只读2项通过。格式、ESLint、类型、构建、Ruff通过，保留既有Starlette及大块构建告警。

12份服务器私有SQLite备份均0600、quick_check=ok。候选作者镜像真实挂载验证8设置分区、3角色、8源码指纹；上线后两角色8补丁指纹与候选一致，活动技能包仍为 `2b640dc9f6788b35748d313e147ad7852526206de87d9ae3bd953b23757fb0cc`，GBAR登记/权重/配置/model_load_count=1均保持。

失败不删除或覆盖：候选校验脚本误写9分区（实际8），修正检查后通过；展示单测漏等Vue更新，修正等待；三维浏览器脚本找错按钮，改为实际“打开轨迹交互→切换三维”，GET复验通过。首次管理切换因旧systemd用户进程无Docker组权限自动回滚，使用账号已有docker组的sg启动方式经systemd实际验证后再次成功，无模型请求。最终静态升级误判systemctl disable已删链接后的非零退出，上一版本先恢复；后按链接实际后置条件和已冻结文件摘要修正并完成升级。网页登录早先账号/密码不匹配401，均在网关更改前停止；用户补充正确HYN凭据后新旧网站200，未改密码或原登录文件。所有失败独立记录。

HTTP小样本仅20个私网目录GET，并发2：核心p50/p95约12.1/31.1ms，门户41.4/56.3ms，错误0；不是GPU吞吐、HTTPS用户端性能或SLA。Python环境版本与包许可分类、363个前端锁定包及7个直接依赖许可元数据已归档，最终分发许可人工审查未宣称完成。包锁SHA保持 `e0434b056f4173684e64742bbf73c3de48ffe5e1711827bf6a7f88b59c1b4e37`。

## 启停、复验与回滚

在服务器项目根目录：

```bash
systemctl --user status scat-hermes-portal.service scat-memory-model-proxy.service scat-auto-skill-learning.service
systemctl --user restart scat-hermes-portal.service
```

门户已enable；版本在 `var/deploy/portal/active-release`，配置与静态包在 `var/deploy/portal/releases/<release>`。业务AUTH_SECRET轮换后重启门户以重新读取凭据；无需复制到浏览器。新站点 `/etc/nginx/conf.d/scat-hermes-portal.conf`；旧 `/etc/nginx/conf.d/scat.conf` SHA未变。新站无访问正文日志。

本机项目根目录运行 `C:\ProgramData\anaconda3\python.exe -B scripts/probe_portal_browser.py`，按隐藏提示输入SSH与网页密码；用已有Node/Edge，只读复用原结果，禁止写请求，不做新推理。自然语言脚本 `frontend/e2e/closeout-natural-live.spec.ts` 已有提交哨兵，**不要删除哨兵重跑**。

移除新公网入口的受控回滚（不影响旧站）：先核对当前发布与新配置摘要，执行 `sudo /home/h3c/anaconda3/envs/scat-agent/bin/python -B scripts/deploy_portal_gateway.py nginx-rollback`，该命令仅在新配置与记录完全一致时移除本项目新站点并reload；随后可 `systemctl --user disable --now scat-hermes-portal.service`。不要回退活动/unknown业务任务或重发成功推理。管理镜像前版Compose及两次私有状态备份都在服务器保留；核心回退先走空闲、备份和摘要检查，旧接口无法提供新管理增强应明确说明。静态升级保留上一版，变更期间业务任务在独立后台继续。

开发纪律：仅既有Anaconda base/scat-frontend及服务器scat-agent/scat-hermes，已有Node24/npm11，无装包、新环境、venv/uv；本轮任务暂存限两端`.tmp/platform-closeout`并收尾删除。独立活动workbench生命周期目录保留，正式入口不依赖它。没有修改驱动、权重、宿主DNS或Docker运行时；原端口服务保留。

## 交付证据与未完成项

机器汇总 `docs/validation/20261108-platform-closeout-acceptance.json`；公开证据 `var/validation/20261108-platform-closeout`，重点 natural-audit、memory-acceptance、management-live、https-browser、portal-result、final-audit、http-latency。冻结原始输入、真实模型输出和私有SQLite仅服务器保留，不下载到本机。

本轮补齐当前入口收口，不宣称整个平台H8完成。剩余：旧业务全域自然语言与七类真实同数据交互对等、标准GPU运行时/CDI及Conda镜像迁移、RAG/记忆独立语义与模型科学质量、并发负载/冷暖启动/长期SLA、最终分发许可人工审查、多用户身份部署。当前Docker只有runc类运行时，CDI配置目录无spec；标准GPU迁移未执行，既有GBAR功能已可用。下一轮优先按业务矩阵补自然语言及真实制品证据，针对真实缺口补代码；独立人工RAG准入和运行时安装另列，不能用测试数量或映射覆盖率推算完成百分比。
