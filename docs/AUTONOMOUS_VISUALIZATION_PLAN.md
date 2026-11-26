# 自主可视化实施计划

日期：2026 年 11 月 27 日。状态：P0 API 验证完成，P1 任务契约与持久化基础完成，完整绘图链路待开发。适用分支：`main` 与 `codex/jev-decision-integration`。

将现有固定图表选择流程扩展为模型自主编写 ECharts JavaScript、Docker 沙箱渲染、Qwen 独立视觉审核、自动修订和用户偏好记忆的完整链路。保留现有数据工具、来源核验与历史图表入口，让自主图表作为可版本化、可追溯的独立产物交付。

第一版复用现有单机 SQLite 任务基础设施，在同一个任务状态库增加独立绘图任务表和专用 worker，不引入新的消息中间件。新增 DS 凭据专用于代码生成，`qwen3.7-plus` 凭据专用于图片审核，两者均在受信服务中调用。

## 1 实施范围和完成标准

第一版应完成以下用户流程：用户提出绘图要求，主 Agent 读取真实数据并组织任务；DS 根据数据结构、用户要求和偏好编写完整代码；平台运行并截图；Qwen 审核图片，必要时返回意见供 DS 修改；通过审核后将图表直接显示在对话；用户自然语言反馈生成新版本；明确的长期偏好可跨会话使用、纠正和删除。

固定 ECharts、Node、Chromium、字体和辅助库版本。模型可自由编写数据变换、子图、坐标、图例、标注、配色和 ECharts custom series，不限于五类图表或 x/y/group 参数。第一版使用标准 JavaScript，不增加运行时任意安装依赖的能力。三维 Cesium 轨迹继续走现有功能，自主三维代码不纳入本版。

交付必须同时满足：数据来源和授权可验证、实际渲染成功、独立视觉审核通过、产物摘要匹配、读取身份匹配。截图审核不等于数值正确性的证明；数值和单位由来源、变换记录及程序检查共同支撑。

## 2 当前代码和约束

| 位置 | 已有能力或限制 | 实施处理 |
| --- | --- | --- |
| `frontend/src/lib/analysisChart.ts` | 五类图和固定字段选择 | 保留快捷入口，新增自主产物展示 |
| `frontend/src/components/BaseChart.vue` | 按需注册 ECharts 模块 | 自主沙箱使用独立完整模块清单，不能沿用五类注册限制 |
| `src/application/readonly_display.py` | 打开来源时确定性投影，不运行模型或脚本 | 保持只读，生成图表通过显式任务入口 |
| `src/hermes_runtime/results.py` | 回执有大小和有效期限制 | 创建绘图前物化独立授权数据快照 |
| `src/hermes_runtime/script_language.py` | 有限 JSON Python 语法解释器 | 保持用途，另建真正代码执行环境 |
| `src/application/task_store.py` | SQLite 队列和租约，claimed/unknown 全局阻塞领取 | 绘图使用独立表，避免改变旧任务恢复语义 |
| `src/application/skill_job_store.py` | 有 generation 隔离，但并发检查仍读取 model_tasks 全局状态 | 不把绘图伪装成 skill job/model task |
| `src/hermes_runtime/profiles.py` | profile provider 和地址限定 DeepSeek | 新增独立模型适配器，不让审核器依赖现有 profile |
| `src/hermes_runtime/honcho_memory.py` | 所属用户隔离、记忆开关、更正、删除和显式偏好识别 | 增加图表结构化偏好并接入现有生命周期 |
| `src/schema/analysis.py` | 有版本约束且定位于确定性结果 | 新增图表引用块，生成代码和图像放独立契约 |
| `infra/compose/compose.yaml` | 已有受信后台 worker，环境变量显式列举 | 新增绘图 worker 与沙箱启动服务，不向沙箱传递角色凭据 |

本地核验时两个分支均为 `7a943d5b158831b053915b7c41c6716d9c434925`，左右差异为 0/0。实施开始及同步前必须重新核验，不能把该状态当作永久事实。

## 3 组件分工和运行路径

```text
用户要求或反馈
  -> 主 Agent 确定数据范围和目标图表
  -> 已授权数据快照 + 生效偏好快照
  -> visualization_jobs 持久任务
  -> 受信绘图 worker 调用独立 DS API 生成代码
  -> 沙箱启动服务创建一次性 Docker 容器
  -> 固定 ECharts 与 Chromium 执行、截图并返回渲染清单
  -> worker 执行程序检查并调用 Qwen 图片审核
  -> revise 时把意见交回 DS，并重新执行与审核
  -> pass 时原子提交发布记录
  -> 对话图表卡片及可选隔离交互页
```

主 Agent 负责解释用户意图和选择授权数据；DS 代码生成器自主决定表达方式；沙箱只执行代码；Qwen 审核器只读图像及核验材料；服务端发布器决定交付状态。代码生成器与审核器使用独立上下文，主模型无权自行填写审核通过记录。

建议新增模块 `src/visualization_runtime/`：`contracts.py`、`settings.py`、`providers.py`、`snapshots.py`、`store.py`、`worker.py`、`sandbox_client.py`、`review.py`、`publication.py`、`preferences.py`。公共领域代码不绑定 Hermes profile；工具桥与 HTTP 接口只负责调用领域入口。

## 4 环境变量与 API 预检

用户已确认两家均为官方直连，Base URL 由用户填写。首次实施已通过独立凭据验证，详见第 18 节；预检仅在显式传入 `--live` 时发起真实请求，密钥不进入输出。

在项目根目录 `.env` 使用以下变量；主对话继续使用原有 `DEEPSEEK_API_KEY` 等配置：

```dotenv
SCAT_VIS_ENABLED=false

# DS 绘图代码生成，使用新增的独立 key
SCAT_VIS_CODE_API_KEY=
SCAT_VIS_CODE_BASE_URL=
SCAT_VIS_CODE_MODEL=deepseek-flash

# Qwen 真实图片审核
SCAT_VIS_REVIEW_API_KEY=
SCAT_VIS_REVIEW_BASE_URL=
SCAT_VIS_REVIEW_MODEL=qwen3.7-plus

SCAT_VIS_API_TIMEOUT_SECONDS=120
SCAT_VIS_MAX_REVISIONS=2
SCAT_VIS_MAX_PROVIDER_REQUESTS=10
SCAT_VIS_MAX_TOTAL_TOKENS=60000
SCAT_VIS_RENDER_TIMEOUT_SECONDS=30
SCAT_VIS_WORKER_CONCURRENCY=2
SCAT_VIS_ARTIFACT_ROOT=var/visualizations
```

上述变量已由独立设置类与预检脚本读取，正式 Agent/worker 链路尚未接入。只填写 `.env` 不会启用功能。两套 API key 不回退到主 Agent 凭据；缺失时报告具体缺少的变量名。

`BASE_URL` 为兼容接口前缀，不填完整 `/chat/completions`。DeepSeek 官方示例使用 `https://api.deepseek.com`。Qwen 按控制台选择地域、工作空间和套餐的兼容地址；普通百炼和 Token Plan 不可盲目混用地址与 key。准确 URL 以用户实际填写为准，不由模型推断账号信息。

配置实现使用 `SecretStr` 保存 key；配置摘要仅记录模型名、端点标识和 key 是否存在。HTTP 客户端关闭环境代理继承和自动跨站重定向；URL 是运维配置，不接受用户工具参数覆盖。生产只允许 HTTPS 官方配置端点，禁止带 URL 内嵌凭据。

### 4.1 预检步骤

- [x] 新增 `scripts/check_visualization_apis.py`，默认检查配置，不调用模型；显式 `--live` 执行用户已要求的真实连通性验证，可在总功能开关为 false 时运行。
- [x] DS 发起一条限额请求，要求返回使用给定合成数据的最小 ECharts JavaScript；核对 HTTP、实际模型标识、有效正文及 finish_reason，不以列出模型接口成功代替生成成功。预检不执行生成代码，不将代码片段检查当作可运行性证明。
- [x] Qwen 发送本地生成且已知内容的三柱图片，要求识别最高柱类别、颜色和柱数，并返回 JSON。实际图片字节经 image_url data URL 传入，不把本地文件路径或提示词描述当作图片输入。
- [x] 测试图片不附带正确答案提示；本地校验关键答案，证明接口接收了图片。单次小测试只验证接入，不视为审图质量评估。
- [x] Qwen 使用 JSON object 输出并进行本地严格字段及答案检查；DS 按设计返回自由 JavaScript 正文。审核器原生 JSON schema 能力在 P3 再验证，当前不宣称已验证。
- [x] 记录脱敏状态、错误分类、用量和识别检查；报告保存 `output/visualization-api-preflight/`，不保存 Authorization 或原始 HTTP 调试转储。改进后的脚本另记录受限的返回请求 ID。
- [x] 预检通常每家一次请求，默认不自动重试。401/403、模型/端点不存在和限流分别报告；超时标记结果未知，不循环消耗额度。支持 `--role review` 单独重测。

部署时把密钥显式注入受信 `visualization-worker`。镜像中不 COPY `.env`，绘图容器不继承 worker 的环境变量；根目录 `.env` 不会自动进入现有 Compose 服务。生产 secret 文件或环境变量注入须与启动脚本统一，启动后查询脱敏配置状态确认实际加载。

## 5 数据快照与数值边界

快照创建入口解析当前用户可访问的 result/artifact 引用，再调用现有只读查询能力补齐所需数据。每次补读受授权范围、查询预算和总快照大小限制约束，不允许绘图代码直接查询数据库或调用平台工具。

保存 `snapshot_id`、owner、thread、源 run/receipt ID、源 payload SHA256、查询条件、字段名和单位、时区、缺测规则、采样步长、数据行数、完整性、截断说明与创建时间。分页结果只有确认全部页齐全后才可标记完整；无法补齐时允许按可见范围绘图，但标题或说明必须明确范围。

将完整数据文件以只读 JSON/Arrow 等形式提供给沙箱；给 DS 的上下文提供 schema、代表行、范围和统计摘要，避免将大数据完整塞入提示词。生成代码运行时访问完整授权快照。初始上限建议 100000 行、20 MiB，超限要求收窄范围或按明确规则抽样。

模型可编写变换和统计代码，产物必须声明聚合、过滤、抽样、插值或单位转换。关键业务指标引用现有确定性结果；新衍生统计标为代码计算，并对可验证的行数、分组计数、范围和单位做程序检查。禁止把拟合线、合成示例和缺测补值伪装为观测值。

快照和图表拥有独立保留期，避免回执一天后失效导致无法修改；普通源回执过期不自动撤回已授权物化快照，用户撤权或删除数据则必须撤销相关访问。旧快照过期后，不能声称可精确重放；重新查询应形成新快照和新版本。

## 6 Docker 执行环境

新增固定绘图镜像及依赖锁，包含 Node、ECharts、Chromium、中文字体及一个小型受信渲染宿主。沙箱加载完整受支持 ECharts 模块，包括 graphic/custom series 能力，固定库边界不等于限制图表种类。

每次渲染使用新的非 root 容器：只读根文件系统，关闭网络，去除 capabilities，启用 no-new-privileges，限制进程数、CPU 和内存。Chromium 仅加载容器内资源，不允许生成代码关闭浏览器安全边界。初始建议 1 CPU、1 GiB 内存、128 个进程、30 秒墙钟时间，按实际基准调整。

只读挂载代码、数据与库；只写每次渲染的输出目录和临时 tmpfs。禁止挂载仓库、宿主 `.env`、数据库凭据、Docker socket。容器停止、清理和输出采集由受信启动服务负责；不能把启动服务的任意 Docker 参数暴露为模型工具参数。

绘图 worker 调用受控沙箱启动服务，其接口只接收已落盘的渲染 ID、摘要和允许的视口参数。仅该服务持有受限容器启动权限，可先部署在同机宿主进程或独立管理服务。模型、API 和绘图容器均不能直接访问 Docker daemon。

代码入口建议 `export async function render({ echarts, container, data, metadata })`。沙箱等待宿主的确定性完成信号并截图，固定随机种子、时区、字体、locale 和动画设置。输出 PNG、可选 SVG、浏览器错误、执行清单和展示用衍生表；失败不产出发布记录。SVG 和 HTML 作为不可信产物处理，不内联到主应用。

首轮审核至少包含宽屏和窄屏预览，例如 1200×720 与 390×640；同一代码可自主实现响应式布局。交互态不可能靠初始截图穷尽验证，需限制能力并覆盖主要缩放、图例切换和 tooltip 行为。

## 7 任务存储和恢复

复用 `HERMES_TASK_STATE_FILE` 指向的本机 SQLite 文件，新增独立表 `visualization_jobs`、`visualization_attempts`、`visualization_events` 和 `visualization_provider_calls`。不创建伪造的 model run，不通过原有模型确认 API 提交绘图。

第一版不全面重构旧 TaskStore/SkillJobStore；共享连接和事务工具可有限抽取，旧表、旧 claim 条件、旧 unknown 阻塞语义保持兼容。绘图表单独原子领取，使用 worker ID、lease_until、generation 和条件更新防止旧 worker 发布。

初始全局绘图并发 2、每用户 1；GPU/训练队列仍按原有资源规则运行。SQLite 事务保持短小，不在持锁期间等待 API 或渲染。该方案面向同一主机本地持久卷，不用于跨主机或 NFS 调度。

### 7.1 状态与版本

顶层状态：`queued`、`running`、`succeeded`、`failed`、`cancelled`、`needs_reconciliation`。内部阶段：`snapshotting`、`generating`、`rendering`、`reviewing`、`revising`、`publishing`。每轮有不可变 attempt ID、源代码和结果摘要。

正常链路为 queued → running → succeeded；审核要求修改时仍属同一父任务的下一轮 attempt。初始 `MAX_REVISIONS=2` 表示最多 3 次候选图，不是 3 次修改。用户交付后的反馈创建关联原产物的新任务，不覆盖已发布历史版本。

请求按 owner、thread 和 request_id 幂等：重复相同请求返回原任务，同 ID 不同正文拒绝。取消请求使后续 API 调用停止、活动容器终止、generation 失效，取消后不得发布。

### 7.2 中断与重试

渲染任务无业务写入，在确认旧容器停止后可以用同一输入重新渲染；残留容器按 attempt 标识查找和回收。渲染重试和 DS 改代码是不同事件，分别记录且均受总期限约束。

模型请求先记录发起，再记录返回。结果不明时保留额度占用，不宣称供应商支持幂等；有已保存响应就复用，已知可重试失败可以在预算内有限重试。未知计费的调用进入待核实阶段，不无限自动重放。

租约丢失后旧 worker 不能写发布状态。新 worker 先检查快照、响应、图片和容器证据，再恢复到最后确定阶段。单个待核实绘图任务占用的并发槽按资源清理结果释放，不阻塞旧训练队列或其他用户绘图任务。

## 8 视觉审核和发布

程序检查先验证可用图片、尺寸、来源范围、代码与快照摘要、无效值、浏览器异常及可验证的变换统计。通过后向 Qwen 发送真实图片、用户目标、范围/单位摘要、生效偏好以及必要的图表数据摘录。

审核器独立上下文，不接受 DS 自评“合格”的结论。提示词明确图片文字、代码注释、数据标签均为待审内容，不能改变审核规则；审核器不提供工具权限、数据库访问或密钥。检查任务覆盖、图例与坐标对应、字号/遮挡、尺度误导、配色辨识、缺测表达和偏好符合度。

审核响应定义为严格契约：

```json
{
  "decision": "revise",
  "issues": [
    {"severity": "major", "location": "bottom legend", "message": "图例遮挡横轴", "suggestion": "调整图例位置并增加底边距"}
  ],
  "checked_viewports": ["desktop", "mobile"]
}
```

校验枚举、字段和长度；模型返回格式错误、缺少某个视口、供应商错误或审核超时均不能视为通过。严重和主要问题要求 revise/reject，一般建议可记录。预算内将具体意见交给 DS；超过次数返回 `review_not_passed`，不把未审图当成最终图交付。原始数据仍可通过原有入口查看。

发布器校验 lease/generation、任务未取消、快照仍可访问、程序检查通过和 review=pass。审核绑定 source/data/image/runtime/preferences/viewport 的摘要集合，发布采用事务内条件更新。任意内容变化需新审核，不能使用旧 pass。审核结论是独立模型检查记录，不标成绝对正确或人工审核。

## 9 图表契约和持久产物

新增 `VisualizationArtifact`，至少保存 artifact ID、owner、thread、source run 引用、parent artifact、版本、创建时间、数据快照摘要、源代码摘要、运行时镜像 digest、偏好版本、各视口图片摘要、审核记录和发布状态。绘图任务 ID 使用独立命名空间，不要求符合旧 Mongo 模型 run ID 格式。

数据、代码、图片和审核记录不可原地覆盖；版本之间通过父引用关联。建议布局为 `var/visualizations/<artifact_id>/<version>/`，路径由服务端生成。导出和读取按 owner 重新鉴权，不凭随机 ID 当授权；拒绝路径穿越、符号链接输出和超量产物。初始单次产物总上限 30 MiB。

新增独立版本化契约和 `visualization_ref` 分析块，更新 schema_version 允许值、版本校验、前端类型与 OpenAPI 生成物。该引用块不将模型生成数值混入确定性分析块。保持现有七种 block 和历史版本可读。

普通历史读取不调用模型、不重新渲染；按保存文件返回。导出 PNG 使用审核的图片；导出 SVG 使用同次候选产物和安全下载响应。CSV 标明是源快照还是衍生展示表，不以一个下载按钮混淆二者。

## 10 工具接口与前端反馈

建议新增 `src/application/visualization_tasks.py`、`src/service/visualization_api.py` 和 `src/hermes_runtime/visualization_bridge.py`。主 Agent 工具为 `create_visualization(data_refs, intent, viewport, request_id)`、`revise_visualization(artifact_id, feedback, request_id)`、`get_visualization_status(task_id)`；实际自由代码由独立 DS 生成并传入内部 `render(source_code, snapshot_ref, viewport)`。外部创建请求不接受 owner、密钥、任意数据路径或镜像参数。

API 使用现有认证及会话归属逻辑，建议路径：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| POST | `/api/v1/hermes/threads/{thread}/visualizations` | 创建自主绘图任务 |
| GET | `/api/v1/hermes/threads/{thread}/visualization-tasks/{task}` | 状态及受限错误 |
| POST | `/api/v1/hermes/threads/{thread}/visualization-tasks/{task}/cancel` | 取消 |
| GET | `/api/v1/hermes/threads/{thread}/visualizations/{artifact}` | 读取指定已发布版本 |
| POST | `/api/v1/hermes/threads/{thread}/visualizations/{artifact}/revisions` | 根据反馈创建修订 |
| GET | `/api/v1/hermes/threads/{thread}/visualizations/{artifact}/files/{file}` | 鉴权预览或下载 |

具体方法和路径在契约阶段冻结，文件名必须解析为产物清单中的逻辑文件 ID。状态读取复用已有轮询/取消模式；SSE 只作为可选通知，数据库状态为准。

前端新增 `VisualizationArtifactView.vue` 与反馈输入，接入对话结果渲染器及任务面板。显示中文阶段文案、审核后的图片和版本；操作为放大、导出、查看来源、继续修改。用户修改已发布图时保留旧版本可见，新版本通过审核后才切换。

第一版完整交付图片及修改闭环。交互扩展阶段增加独立无登录 cookie 的产物域与 sandbox iframe，使用 `allow-scripts`，不授予 same-origin、顶层导航或弹窗。受信壳通过绑定窗口与一次性 nonce 传递数据，严禁凭 `origin=null` 单独授权。CSP 禁止外部连接，资源固定，父子消息校验结构及大小。不得在 Vue 主页面执行模型代码。交互内容运行同一已审核代码，但动态交互态仍需单独测试。

## 11 用户偏好学习

新增结构化偏好存储，字段包括 owner、memory epoch、key、value、作用域、任务类型、依据原话、来源图表、明确程度、状态、更新时间和替代关系。支持配色、字体、字号、密度、图例位置、单位、图形习惯和特殊需求，不将业务结论当偏好。

“这张图换成蓝色”作用于本图；“以后都用蓝色系”作为明确全局偏好。模糊反馈先作为会话偏好或候选，不能从一次点击推断永久规则。没有要求记住时也可形成当前任务上下文；跨会话持久化遵循现有记忆开关。

优先级为本次指令、当前图表、特定任务偏好、用户全局偏好、默认。生成和审核均使用同一偏好快照；用户偏好不覆盖数据真实性、单位及授权要求。明确结构化偏好在本地立即可用，Honcho 可异步补充语义摘要，不能等待远程推导才生效。

对接现有 memory API 的查询、纠正、关闭与整体删除。关闭时停止召回和新增长期记录；整体删除时递增 epoch，清除结构化记录、待写 outbox 和缓存，旧任务不得把删除前偏好写回来。生成中的任务改用删除后的偏好快照重新检查。已发布产物如为可复现而保留可见样式，不再将历史反馈作为新任务记忆；敏感原话按删除语义清除。

## 12 预算与观测

初始配置：最多 2 次修订、10 次供应商请求、60000 输入加输出 token、单次 API 120 秒、每次渲染 30 秒；任务总期限建议 15 分钟。实际启动前校验输出上限和总预算不冲突，输出截断不得执行残缺代码或接受残缺审核。

DS 和 Qwen 的请求分别归账，图像 token 按供应商已上报用量记录；请求前保守预留，失败和未知调用不当作零成本。最多请求数包括网络重试；未上报完整用量时保留“未知”，不伪造精确费用。另设绘图每日共享额度，接现有父预算的并发原子预留，不因使用独立 key 绕过平台额度。

沿用 owner-scoped diagnostics，记录父对话、任务、attempt、生成/审核角色、耗时、错误分类、模型、已知用量、截图摘要和发布事件。默认不记录完整图片、数据、key 或敏感提示词；源代码作为受限产物保存。指标至少包含成功率、首轮通过率、修订次数、阶段耗时、成本已知程度、沙箱超时、待核实任务和清理失败。

## 13 分阶段实施清单

| 阶段 | 工作与主要文件 | 阶段完成条件 |
| --- | --- | --- |
| P0 API 验证 | 新增独立配置与 `check_visualization_apis.py`，读取用户填写值 | DS 代码和 Qwen 实图最小请求均成功，脱敏报告可读 |
| P1 契约与状态 | contracts/settings/store、任务独立表、快照与产物布局 | 幂等、归属、租约隔离、取消、迁移通过离线测试 |
| P2 渲染执行 | 固定 Dockerfile/锁、启动服务、渲染宿主与截图 | 标准图、多子图、custom series、中文与窄屏可渲染，容器可终止清理 |
| P3 自动审核 | DS 编码适配器、Qwen 图片审核、程序检查、修订和发布器 | 一条真实数据链路能自动修改后发布，错误及未审核图无法交付 |
| P4 对话交付 | API、Hermes 工具桥、图表卡片、自然语言修订和 OpenAPI | 原始用户请求到图表、新反馈到新版本均可用，历史读取不发模型请求 |
| P5 偏好记忆 | preferences 与 Honcho 生命周期集成、设置和删除 | 本图/全局范围正确，跨会话应用且删除后不复活 |
| P6 验收与双分支 | 故障演练、真实图表集、Compose、锁文件、双分支验证 | 两个分支均满足验收矩阵，可关闭功能并恢复旧入口 |
| P7 隔离交互扩展 | 独立产物域、iframe、通信与交互截图 | 动态图在隔离域运行，交互回归和访问控制通过后启用 |

P0 → P1/P2 → P3 → P4 → P5 → P6 是首版完整闭环。P7 在完整闭环之后单独交付；首版不以交互扩展未完作为已实现交互功能。实施使用现有 Conda 环境，不创建项目 venv。先完成小范围关键验证，再执行必要相关回归。

## 14 验收矩阵

| 类别 | 必须覆盖的场景 |
| --- | --- |
| 自主表达 | UAV 多通道分面时序、SCAT 轨迹加高度子图、异常阶段分布、自定义误差图；可超出五类模板 |
| 数据 | 完整/截断分页、缺测、空结果、单位混用、衍生统计、快照过期、撤权 |
| 审核 | 清晰图通过；遮挡/错误单位/范围误导被指出；坏 JSON、无图片、缺视口和超时均不通过 |
| 修订 | Qwen 指出问题后 DS 改图；最多三候选；用户反馈新建版本；旧审核不能批准新图 |
| 隔离 | 无 key/env/数据库/socket 访问；外联被阻断；死循环、超内存、超时可清理 |
| 队列 | 双 worker 竞争、重复 request_id、租约丢失、取消、旧 generation 提交、重启恢复 |
| 恢复 | 请求发出后断网、响应保存后崩溃、截图后崩溃、发布前崩溃、孤儿容器和重复发布 |
| 资源 | 绘图失败或待核实不堵塞训练；用户额度隔离；等待审图不占渲染容器 |
| 权限 | 跨用户、跨会话伪造引用、路径穿越、过期下载、已撤销快照和受限诊断 |
| 记忆 | 一次性修改不变全局、明确偏好跨会话、新指令覆盖旧偏好、关闭/删除/epoch 竞争 |
| 兼容 | 历史 AnalysisResult、旧任务确认与 unknown 恢复、来源只读打开、原 Cesium 轨迹 |

建议测试文件为 `tests/visualization_runtime/test_settings.py`、`test_store.py`、`test_snapshots.py`、`test_publication.py`、`test_recovery.py`、`test_preferences.py` 及服务/API 测试；前端增加图表产物组件和反馈端到端用例。测试围绕可观察行为与安全边界，不逐行复制实现。

真实验收集预先冻结 12 个场景，包含多子图、中文长标签、窄屏、缺测和错误尺度。首轮调试记录与最终候选分开保存，审核器不能读取验收答案。记录所有失败与用量，人工抽查交付图的数据与表达；真实 API 成功不替代故障和权限测试。

## 15 双分支实施与回滚

新文档显式加入 `.gitignore` 例外，以便纳入两个分支。准备阶段只编辑文档与配置模板，不移动分支引用，不提交密钥。现有未跟踪图片 `docs/assets/uav-sensor-analysis.png` 不属于本次改动。

实施时先记录两个分支 SHA 和工作区状态，从共同基线建立一个实施分支。按 P0、P1、P2、P3、P4、P5 和部署验收形成可审查提交，运行时默认开关关闭。业务代码只实现一份，不在两个分支分别开发两套。

若两个分支仍同源且可快进，将相同实现提交同步到两个分支；若已分叉，按顺序移植共同提交，解决接口差异并分别验证。同步前检查目标分支是否被其他工作区占用，不通过强制移动分支绕过用户工作。实际提交与分支同步属于后续实施步骤，本计划生成不宣称已经同步。

两分支分别运行后端相关测试、前端类型检查/测试/构建和部署配置验证；公共代码相同也要核验各自入口、技能锁、环境变量注入和 schema 生成物。保存分支 SHA、共同补丁摘要、API 预检和验收报告引用。

灰度顺序为本地合成数据、单用户真实数据、小范围用户、默认入口。生产前备份任务库与配置，采用可重复且有 schema_version 的增量迁移，不重建旧任务表。新增表与旧版本兼容，不在回滚时删除已保存数据。

回滚先关闭 `SCAT_VIS_ENABLED`，停止接收新任务，再取消或核实活动绘图任务并清理容器；回退服务和技能版本。保留可用的静态历史读取入口及产物，不撤销既有训练恢复规则。图库镜像回退与 API key 轮换独立执行。

## 16 实施启动顺序

1. 用户填写根目录 `.env` 中两组 key、Base URL 和实际 DS 模型 ID，保留功能开关关闭。
2. 实施者先新增配置解析及最小预检脚本，按用户要求测试两组新 key；仅输出脱敏结果。
3. 两家预检通过后冻结契约、镜像依赖和预算，开始 P1/P2；接口失败先修正配置，不以主 Agent key 顶替。
4. 完成 P3 到 P6，提交可审查结果与双分支验证记录，再启用完整闭环；P7 单独验收。

## 17 官方接口参考

以下文档用于接口兼容性核验，账号权限和地址以用户实际控制台为准。

- [DeepSeek 首次 API 调用](https://api-docs.deepseek.com/)：独立 key、兼容调用及当前模型名。
- [Qwen 3.7 Plus 模型能力](https://help.aliyun.com/zh/model-studio/qwen3-7-plus)：支持图片输入和文本输出，结构化输出须按地域实际能力验证。
- [百炼 OpenAI 兼容接口](https://help.aliyun.com/en/model-studio/compatibility-of-openai-with-dashscope)：Base URL、地域及工作空间域名。
- [ECharts 服务端渲染](https://echarts.apache.org/handbook/en/how-to/cross-platform/server/)：SVG/Canvas 与交互限制。
- [Node VM 文档](https://nodejs.org/api/vm.html)：VM 不构成不可信代码的安全边界。

## 18 第一阶段实施记录

2026 年 11 月 27 日完成 P0 与 P1 中的契约、队列存储基础。第一阶段停在通过关键回归的代码节点；未开启正式绘图，未部署容器，未启动常驻 worker，未修改原有训练或技能任务的恢复逻辑。

### 18.1 API 实测

使用已有 `scat-frontend` Conda Python 3.12 环境。Windows 受限环境的 asyncio 初始化在本机 socketpair 阻塞，阻塞进程终止后，经工具自动审批改在沙箱外执行同一限定请求；被终止的初始化没有发出模型请求。

首次实测报告 `output/visualization-api-preflight/20261126T220956213800Z.json`：DS `deepseek-flash` 成功返回绘图代码，已知输入 75、输出 159、合计 234 token；Qwen 返回 HTTP 401。用户修正本地配置后，仅重测 Qwen，未重复调用 DS。

Qwen 成功报告 `output/visualization-api-preflight/20261126T221200038993Z.json`：`qwen3.7-plus` 正确返回最高柱 B、蓝色、三根柱，已知输入 126、输出 22、合计 148 token。报告文件名为 UTC 时间，所对应客户端日期为 11 月 27 日。两次成功请求已知合计 382 token；401 响应未上报用量，不补记为零。

报告中的模型匹配检查均为 true。预检不保存模型生成代码或图片识别原始正文，只保存脱敏检查状态；供应商 HTTP 错误正文不进入日志。凭据始终留在被 Git 忽略的本地 `.env`。

### 18.2 已完成代码与验证

- 独立 `VisualizationSettings` 与 provider 配置：官方 HTTPS 前缀、无主 Agent key 回退、输出/请求/资源上限和项目相对产物路径校验。
- 限定模型适配器：不自动重试、不跟随重定向、不继承环境代理、整次调用超时和响应大小限制；截断响应保留已上报用量。
- 合成 PNG 实图预检、默认离线配置检查、独立角色重测及脱敏 JSON 报告。
- 版本化绘图请求、反馈父版本引用与审核结果契约；重大问题不能返回 pass。
- 同一任务库中的独立绘图表、增量初始化、请求幂等和摘要核验、所属用户及会话隔离、原子领取、全局及每用户并发、租约心跳与 generation 校验。
- 取消运行中任务使旧 worker 立即失去写权限，但保留资源槽为待核实，等待后续容器清理模块接入；当前没有伪装成功或直接成功发布的接口。
- 55 项相关测试通过，包含 46 项新增测试和 9 项原模型任务存储回归；新增目录及脚本通过 Ruff 检查与格式校验。

### 18.3 下一阶段边界

P1 尚需授权数据解析、快照物化、attempt/provider 账本、资源清理后核实与恢复；P2 尚需固定 Docker 镜像、受信启动服务、渲染宿主和截图。P3 之后才接入自动审图和发布门禁；前端、偏好记忆与隔离交互尚未实现。

后续先补齐快照和调用账本，再接 Docker 执行。两家最小接口已经验证，不应在每次续做时无理由重跑付费预检。只有配置变化或接口集成故障需要重新验证时，按具体角色最小重测。
