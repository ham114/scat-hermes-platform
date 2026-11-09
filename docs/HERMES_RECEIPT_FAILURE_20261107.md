# 新指令无只读回执：供应商500与页面提示

日期：2026-11-07，Asia/Shanghai。

用户反馈所有指令显示“暂无可读取的只读回执”。本轮确认有两项问题：新指令在模型调用阶段失败；页面把失败且无数据的情况显示成通用空列表。当前仅完成页面提示修正，供应商500仍未恢复，不将前端测试通过当作新指令可用。

## 已确认事实

- 旧成功run `run_ff948b7e76124769a2e91470ce2a5857`仍有一份`get_scat_track_points`回执，桌面与手机真实GET回读均显示100个点，街道地图正常。原数据没有丢失，回执列表/读取接口正常。
- 用户新绘图run `run_023fd1196bed452d8d86bfea68c6c341`为failed：原生API记录HTTP500，request_id `f7fade0b-b3c0-4efb-a022-73c78199e07a`；2次LLM尝试、0工具，无业务数据回执。
- 用户查询模型目录run `run_fc98a67fd339498b85c0e0bf40c379fd`为failed：目录工具执行一次成功，随后模型调用HTTP500，request_id `1796e9db-55c4-4649-8d36-1f4d0239acee`。工作流目录工具本身不属于业务只读数据回执的发布白名单，不能把目录调用成功等同于生成轨迹数据。
- 上述运行exhausted均为空，日志最近300行无`readonly_receipt_unavailable`，原生服务healthy、RestartCount=0，启动时间保持2026-11-06T23:55:51Z。没有证据表明是本轮地图代码、回执数据库或运行额度耗尽引起。
- 只发送一次新的普通100004绘图请求复核独立会话，thread `d450ce8d-b3b2-4cd7-aad6-f787f26954c4`，run `run_06bd7285abad4e36a3c2e6647fc92c52`，仍failed/HTTP500，request_id `7df68f99-9f99-48d2-80a0-49041797f96d`。2次模型尝试、0工具、136803保守预留单位、供应商usage未返回；不能把usage为0当成零费用。没有自动重试该用户请求、预测任务或扩大预算。

[DeepSeek官方错误说明](https://api-docs.deepseek.com/zh-cn/quick_start/error_codes/)将500定义为服务器内部故障。本轮已确认实际接口返回500，未取得供应商内部根因，不能进一步推断其具体内部故障。

## 已实施修正与验证

`RunEvidencePanel`接收该轮执行状态：失败、停止、中断分别说明没有生成可读取数据；已完成但没有独立数据回执时，引导查看回复或任务面板。已有回执仍照常显示，不伪造回执或改写失败终态。

- 前端格式、lint、143单元、类型与构建通过；普通桌面/手机回归40项通过。
- 真实桌面/手机各一次只读回读通过：新失败来源有明确提示、原成功100点可绘图、写请求0。
- 新请求恢复检查失败，按失败保留记录，不继续发请求求通过。此轮新对话链路仍不可用；后续需要供应商接口恢复后再做一次受限验收，或经用户选择切换已有可用模型服务。
- 当前本机工作台5175已加载提示修正；服务器只同步源码、构建归档和记录，没有重启服务器/容器，没有改API Key、模型、DNS、GPU、技能包或预算。继续保留活动预览暂存，测试专用暂存收尾删除。

证据：`var/validation/20261107-receipt-diagnosis/{backend-audit.json,frontend-check.log,browser-regression.log,readback-desktop.json,readback-mobile.json,recovery-run.json}`；汇总`docs/validation/20261107-receipt-diagnosis-acceptance.json`。`scripts/audit_receipt_failures.py`只读审计状态、计数及错误码，不导出轨迹、回复或凭据。
