# Honcho 默认配置草案（2026-10-27）

开发纪律：仅已有Anaconda管理Python，不创建venv/uv，不安装依赖或启动服务。本次只准备配置和Key填写位置；无临时文件、模型请求、历史导入、现有配置/镜像重建。服务器项目 `/home/h3c/project/python/scat-hermes-platform`。现有18120–18128、18081/18133、旧80/443/8080/8501/8091/27017和GBAR保留。拟定回环18140 Honcho /18141本地Embedding，尚未监听或部署；DeepSeek出口 `https://api.deepseek.com`。数据库/cache建议容器内部网络，不新增宿主公开端口。

用户要求：先列出默认配置，所需Key留空，用户稍后填写。因此以下全部为**自托管候选配置**，不代表Honcho已启用或通过端到端验收。

后续状态（2026-10-27）：用户填写Key后，已完成独立候选Honcho服务/SDK/固定Hermes插件的真实合成提取、跨会话召回、更正/删除及重启恢复测试；候选服务测试后停止，正式主平台记忆尚未接入/启用。以下仍是初始模板设计，执行记录与实际边界见 [Honcho测试记录](HERMES_HONCHO_TESTING.md)。真实DeepSeek Key已迁至服务器私有provider字段，公共JSON保持空apiKey。

## 默认选择

| 项 | 默认草案 |
|---|---|
| 主角色 | 仅scat-main；worker/author关闭，未匹配host默认关闭 |
| Honcho地址 | http://127.0.0.1:18140，拟定 |
| 用户/AI/workspace | scat-researcher / scat-main / scat-hermes-main |
| 观察 | unified；限制双向互观察，初期围绕用户记忆 |
| 身份 | pinUserPeer=false，无外部账号别名；只接受平台认证身份 |
| 会话 | per-session + 用户/AI前缀；适配层仍须显式绑定平台thread_id |
| 写入 | async、保存新增对话、不导入历史；需实现重启flush/写入持久性验证 |
| 召回 | hybrid，contextTokens=1500，基础上下文间隔2轮 |
| 记忆推理 | 间隔5轮、深度1、low；禁动态升档、长度升档及查询改写 |
| 其他控制 | 不保存bot对话、不记录记忆注入正文日志；Dream关闭 |
| 向量 | 已有本地BGE-M3，1024维，待新增兼容/v1/embeddings接口 |
| 推理/提取/摘要 | 独立DeepSeek Key + deepseek-flash；接口/工具/schema仍需验收 |
| 数据 | 独立PostgreSQL/pgvector，缓存初期关闭；与现有Mongo/RAG索引分开 |

注意：cadence代表触发间隔，不保证固定次数或费用。hybrid的自动记忆推理与显式honcho_reasoning均可能消耗模型请求；contextTokens是插件配置预算，不能冒称平台所有调用的精确总token硬上限。后续需独立记录Honcho内部模型调用，不能假定现有Hermes预算钩子已覆盖远端Honcho。异步写入本身不减少推理token。保留期限/自动删除策略尚未实施，不宣称已有自动清理。

## 文件与填写位置

- 公共Hermes模板：`config/honcho/honcho.json.example`。
- 公共Honcho服务模板：`config/honcho/server.env.example`，所有凭据为空，不在此填写实际Key。
- **请在服务器私有文件填写：** `/home/h3c/project/python/scat-hermes-platform/var/deploy/honcho/secrets/honcho.env`，权限0600，位于忽略的var目录。只需填写 `LLM_OPENAI_API_KEY=`；建议申请一个独立DeepSeek Key，控制台命名 `scat-honcho-memory`。该变量是Honcho OpenAI兼容传输命名，不要求OpenAI厂商Key。
- `HONCHO_API_KEY`、`AUTH_JWT_SECRET`、`DB_CONNECTION_URI`、`HONCHO_EMBEDDING_API_KEY`暂时留空，部署时生成服务鉴权/数据库/本地接口凭据；自托管不需要申请Honcho Cloud Key。本次不把实际填写值同步回本机或复制进镜像。

模板没有挂进任何运行home，主host中的enabled=true表示未来意向；当前平台的记忆关闭状态不变。固定插件按命名profile使用 `hermes_scat-main`，部署时还应显式设置/验证HERMES_HONCHO_HOST和实际HERMES_HOME，不能以文件名代替运行确认。服务认证开启后，须验证本地URL的显式token处理；不得依赖本地默认跳过认证或只配置Cloud Key。

`sessionStrategy=per-session`不是平台线程映射的替代品：适配前若缺native session ID仍可能回退目录策略。后续要验证同thread多轮保持同一记忆session、不同thread分开、相同用户跨会话可召回、其他用户不可访问。后台/作者不能获得主用户记忆写权限；记忆不改变任务批准与工具权限。

## 版本依据与待验收

Hermes固定提交5b147ab036d6d19131bff8d20701b5001eab844a目录指向Plastic Labs插件1.0.0、Honcho仓库提交e2d2aba182862deeb846e5cd4a30a41bce0ed82a。本次按其公开README和服务端模板准备，不执行插件自动安装流程。

后续：核对插件SDK与既有Conda锁兼容；建立本地Embedding接口和独立记忆库；接入线程身份、消息脱敏和独立预算；合成测试验证偏好召回、错误记忆纠正/删除、跨线程/角色权限和故障恢复；验证后再决定启用及重建镜像。现有0.6B是重排模型，不作为生成/摘要模型；本轮不常驻35B或下载额外模型。

参考：[固定插件配置](https://github.com/plastic-labs/honcho/blob/e2d2aba182862deeb846e5cd4a30a41bce0ed82a/hermes-plugin-honcho/README.md)、[固定服务环境模板](https://github.com/plastic-labs/honcho/blob/e2d2aba182862deeb846e5cd4a30a41bce0ed82a/.env.template)。
