开发纪律与地址（2026-11-04）：Python仅既有Anaconda（服务器scat-agent/scat-hermes，本机base传输、scat-frontend检查）；前端仅现有Node24/npm11与现有依赖，禁venv/uv/新环境/装包。临时目录仅两端项目.tmp/conversation-history，日志var/validation/20261104-conversation-history。保留服务器18120平台、18121原生及既有服务；后端使用进程内隔离API测试，不新增监听；本机4173浏览器测试，启动前查端口。历史正文仅服务器私有状态库，测试只用合成文本，不外发真实历史、不调用付费模型或提交计算。

# 完整会话管理与接口

本轮补齐原始提问、最终/部分回复、多轮历史、标题和分页。新增记录不包含推理、system instructions、工具凭据或原生session数据；明确用户发送的文本将作为聊天记录持久保存。旧绑定没有提问原文，标记input_available=false，不重建或猜测。旧回复仅通过已绑定运行的只读GET补取，不重放POST。

沿用原SQLite状态库，新增conversation_turns/conversation_titles表，旧表及运行绑定不变。问题写入与准入保留同一事务，UUID重试不重复写入、换内容冲突；拒绝、结果不明保留状态。流式片段按运行/序号去重；终态不被迟到running覆盖。读取与改名保持owner/thread/run归属校验。

## 接口约定

机器可读定义由当前FastAPI路由离线导出：[OpenAPI JSON](api/hermes-conversations.openapi.json)。请求参数、鉴权和校验边界与以下约定共同核对；回复分页的字段语义和缺失规则见本文。

基础路径 `/api/v1/hermes`，沿用平台Bearer鉴权及服务端单用户身份解析。请求不能传owner、scope或原生session；其他用户的thread返回404。以下示例均为合成值。原有创建会话、提交运行、运行状态、SSE与结果接口保留，`GET /threads/{thread_id}/runs`保持原返回契约。

### 会话列表与分页

`GET /threads?limit=20&cursor=<next_cursor>`

limit为1–100，默认20。按创建时间倒序、同时间以thread_id倒序；新会话不会挤占下一页导致重复。首次省略cursor，继续使用上一页返回的next_cursor；结束为null。cursor是不透明的定位值，仅适用于对应用户和列表，不是访问令牌，不应自行构造或跨用户复用。

```json
{"items":[{"thread_id":"a9322722-e12b-4ed1-a2da-180d91056a55","created_at":1793779200,"title":"轨迹指标解释","title_revision":1}],"next_cursor":null}
```

title可能为null，表示尚无标题；title_revision为0表示未设置。首次持久保存提问时，将空白合并后取前60个字符作为默认标题，不调用模型；手工标题不会被后续问题覆盖。

### 完整多轮记录

`GET /threads/{thread_id}/messages?limit=20&cursor=<next_cursor>`

一个item是一轮提问/回复，最多100轮/页，默认20。首次返回最新一页，页内按轮次从旧到新；next_cursor用于读取更早一页，前端将更早记录前插。新轮次在分页期间加入不会使旧页重复。不通过逐条POST重建会话。

```json
{"items":[{"id":7,"request_id":"b9322722-e12b-4ed1-a2da-180d91056a55","run_id":"run_demo","input":"解释轨迹评估指标的单位","input_available":true,"output":"合成回复：距离误差使用米。","status":"completed","created_at":1793779200,"updated_at":1793779210,"last_sequence":3}],"next_cursor":null}
```

字段：

| 字段 | 含义 |
| --- | --- |
| id | 状态库内稳定轮次编号，用于客户端去重；不作为其他接口授权 |
| request_id | 原提交UUID，恢复请求必须保持，不生成新的UUID重放 |
| run_id | 已绑定的实际运行ID；准入未确认时可能为null |
| input / input_available | 原始用户提问及是否有原文；旧记录没有原文时为null/false |
| output | 最终回复或已持久保存的流式片段；未获取/超出保存范围时可为null |
| status | admitting、started、queued、running、completed、failed、cancelled、interrupted、unavailable |
| created_at / updated_at | UTC Unix秒；旧绑定不知道创建时间时为null，不伪造日期 |
| last_sequence | 最后持久片段的SSE序号，初始为-1；用于按同一运行续接 |

`unavailable`表示旧绑定尚未恢复到可信运行状态，不等于执行失败。该接口可对当前页未终止/旧运行进行只读状态补取，并行最多4个；失败保留缺失状态，不调用模型生成回复、不重新提交计算。终态的已保存内容可在原生服务不可用时直接读取。当前服务未收到终态时，下一次读取或SSE会更新持久记录。

`admitting`且run_id为空表示原请求的准入结果未确认：保留原request_id和原提问，仅允许明确恢复原请求，不自动POST。已绑定运行只按原run_id读取/续接。历史接口不返回思考内容、system指令、工具原始凭据或内存召回快照。

### 修改会话标题

`PATCH /threads/{thread_id}`

```json
{"title":"新的会话名称","expected_revision":1}
```

响应：

```json
{"thread_id":"a9322722-e12b-4ed1-a2da-180d91056a55","title":"新的会话名称","title_revision":2}
```

title去首尾空白后必须非空，输入长度不超过100字符，不允许控制字符；expected_revision必须为非负整数，旧无标题记录传0。每次成功递增版本，409表示被其他标签页更新，应先刷新获取新版本；超时/断连不盲目重发，先读取当前标题核对。改名不触发模型、不创建任务。仅获准只读的预览隐藏改名入口，代理仍拒绝PATCH，不因接口新增而自动扩大权限。

### 状态码与兼容性

| 状态码 | 场景 |
| --- | --- |
| 200 | 读取或改名成功 |
| 401 | 平台认证缺失/无效（沿用现有配置） |
| 404 | 会话不存在或不属于当前用户 |
| 409 | 标题版本冲突；提交同UUID的不同问题仍由原准入接口拒绝 |
| 422 | 非法UUID、limit、cursor、空白/超长标题、负数或非整数revision、额外写入字段 |

新增next_cursor、title、title_revision为会话列表的扩展字段；旧消费者仍可使用items。新前端使用messages分页接口，旧runs列表与单run/结果接口保持。游标仅是分页定位，服务端每次仍检查归属，不依赖游标保密。

## 持久化、恢复与限制

数据库仍为平台HERMES_STATE_FILE指向的项目内SQLite，权限0600；新增conversation_turns与conversation_titles，启动时对旧绑定创建缺失占位，不删除或改写旧绑定/预算。问题与准入事务一起提交或回滚；同UUID重试不产生第二轮，准入失败保留原问题与失败状态。流式片段按序号去重，终态防止迟到状态倒退；运行中的无序号快照不覆盖已按序保存的片段。

原问题限32000字符（沿用提交接口），每轮回复保存上限2MiB，不截断后假称完整；超过上限不保存该最终文本，界面明确不可用。保留策略沿用当前状态库的备份和访问控制，本轮不提供删除/自动清理接口；历史正文将随状态库备份保存，不写开发日志或测试报告。SQLite升级为增量加表，回滚旧代码可忽略新表；回滚前仍须核实在途运行并保留一致性备份。

## 验收记录

后端首轮26通过/1失败，失败为既有空列表测试尚未包含新增next_cursor字段；更新契约断言后服务器专项27通过。前端首次完整检查130项单元、格式/lint/类型/构建通过，桌面/手机28项浏览器通过，含原文恢复、两级分页、改名版本与继续原会话。测试均使用合成数据，无外部模型调用。

最终源码服务器专项29项、本机29项通过；服务器完整回归1406 passed/1 skipped（258.90秒）。前端最终130项单元、格式/lint/类型/构建通过，桌面/手机28项浏览器通过（20.4秒），合成截图已实际复核。补验覆盖无序号running快照不能覆盖流片段、超2MiB终态不能把片段当完整回复，以及两级分页的用户隔离。首轮完整回归1404/1和原失败均保留。

部署状态以最终验收元数据为准。开发期间SSH/ping间歇超时，按用户要求间隔探测并在恢复窗口同步；本机使用已有scat-frontend环境继续验证，不新增依赖。受限沙箱中本机pytest一度停在启动阶段，正常本机执行后29项通过。自动审批最初拒绝未明确授权的生产状态备份/暂停学习服务/核心切换，原命令未执行；用户随后明确允许部署本次版本，已备份并切换20261028-s7b-3ad5eac48b08，七个核心容器健康、学习调度恢复。上线最终核验见[验收元数据](validation/20261104-conversation-history-acceptance.json)。前端归档同步不等于正式前端网关切换。

生产只读验收：threads/messages均200（当前抽中的会话为空）、非法cursor 422、无归属thread 404。七核心容器及18120–18126、18133、18140–18142健康；数据库完整且2条原有运行绑定保留，学习调度active。私有备份仅服务器0600；模型账本保持70/4520033，无模型重放。前端416文件构建包与依赖摘要同步核对通过；正式前端网关未切换。详细元数据和临时清理记录见上述验收文件。
