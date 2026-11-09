# Honcho 合成测试记录（2026-10-27）

开发纪律与地址（测试前）：服务器 `/home/h3c/project/python/scat-hermes-platform` 优先，Python仅已有Anaconda环境及从既有Conda精确制品重建的候选镜像，禁venv/uv/新建宿主环境；依赖如需补充仅已有Conda前缀，不改正式服务环境。临时文件仅两端项目 `.tmp/honcho-test` 和受控原子同步暂存，完成清理。候选回环18140 Honcho、18141本地Embedding，测试模型代理18142（按需），数据库仅独立内部网络；保留七核心CPU容器、正式18081/18133、旧GBAR及80/443/8080/8501/8091/27017。Key只留服务器项目私有文件，不打印、不入镜像、不导出；BGE-M3模型只读复用，不下载新权重。

用户已填写Key并授权测试。本次先做独立Key/接口预检，再补齐自托管候选依赖，使用纯合成偏好/事实测试写入、跨会话召回、修改/删除、身份/角色边界及重启恢复；不导入历史、不默认开启正式主Agent记忆，不切正式网关。

## 预登记

- 固定Honcho仓库提交e2d2aba182862deeb846e5cd4a30a41bce0ed82a，版本3.2.2，服务器Python>=3.13；Hermes固定5b147ab036d6d19131bff8d20701b5001eab844a。现有scat-agent3.12不能直接承担该Honcho版本，优先在基于既有scat-hermes3.14的独立Conda候选镜像验证依赖，不升级正式环境。
- Key预检只用两条合成请求，首次失败保留；最多4个HTTP尝试/30000保守单位，不自动重跑成功请求。自托管记忆测试预留总上限60次模型请求/500000保守单位，后续内部调用需通过独立限额/计数代理，不假设原Hermes预算已覆盖Honcho。
- Embedding本地BGE-M3（1024维）；优先CPU隔离测试，避免复制模型及争用现有GPU。数据库、token、日志/模型代理只用于候选状态。真实平台业务数据库、原题集与原记忆不参与。
- 成功须分别报告Key、Embedding、Honcho服务、插件/平台实际接入四层结果；不能以HTTP 200或手工写入记忆替代真实提取/召回。不通过项保留，未完成层不宣称上线。

## 凭据位置纠正

用户将填写值放在本机公共 `config/honcho/honcho.json.example` 的apiKey字段，而服务器私有LLM_OPENAI_API_KEY仍为空。检查时该字段进入过工具输出，属一次实际凭据暴露；不在文档记录其值。已清空公共示例并保留项目忽略目录中的私有导入文件，转移到服务器正确的私有provider字段后验证。Honcho访问token与DeepSeek provider Key必须分别处理；建议测试后轮换此次暴露的Key。后续仅输出是否存在/权限/HTTP状态，测试结束清理本机私有导入副本，不将实际Key复制进候选镜像或下载回本机。

## 结果

已完成独立候选Honcho服务、SDK与固定Hermes插件的合成测试。主平台HTTP入口尚未接入Honcho；这不是正式记忆上线或真实用户历史导入验收。完整原始记录在服务器 `var/validation/20261027-honcho`，公共结果见 `docs/validation/20261027-honcho-acceptance.json`。

### 环境与服务

固定Honcho3.2.2源码归档SHA256 `1cecbb91aa744dba501725bac7b65f0847d5d8752c5c85622d6f0bc35996e087`；从已有scat-hermes Conda镜像派生独立Python3.14候选镜像，105个wheel逐SHA锁定并离线安装，pip check通过，宿主0安装/0新环境。最终候选镜像 `sha256:f251369c1c8c0db47e91bd68800e890330684188d38fb9cf53c1cd9524e823a9`。SDK2.5.1、插件1.0.0来自固定兼容输入；未执行uv/venv安装器。

独立pgvector/PostgreSQL16镜像 `sha256:7b822b0aac60967beb1ea5e576b8602c94c300a157d187f385ae3e0da199b90a`，数据库不发布宿主端口，数据位于本项目var/deploy/honcho/database；API/deriver非root、只读根、删除全部cap。模型Key只由宿主已有scat-agent的回环代理持有，Honcho仅得到单独的代理token。鉴权签名、管理员token和workspace限定token分开，测试工作空间与拟正式scat-hermes-main隔离。

BGE-M3使用已有scat-rag-train在CPU上提供1024维、归一化且无静默截断的兼容Embedding接口，支持float/base64及独立鉴权；未加载新GPU模型或修改正式RAG向量。独立模型代理保留全部请求准入、失败、保守预留和供应商用量，沿用60请求/50万保守单位总账，未复位。

### 实际验证

- Key预检普通响应、强制工具调用2/2通过，374供应商token。
- 固定Hermes HonchoMemoryProvider执行实际initialize/sync_turn/工具调用/shutdown钩子，配置解析到hermes_scat-main，其他两角色未启用。没有手动预写用户偏好结论来替代提取。
- 第一合成会话写入“简体中文简短回复、距离使用海里、HONCHO-TEST-741项目代号”，后台模型真实生成英文结构化观察。第二独立session通过honcho_reasoning召回中文、简短和海里，两个会话键确实不同。
- 第二会话将距离偏好更正为公里，生成带时间的新观察，再次查询得到“公里（kilometers）”。旧观察作为历史保留；本测试证明最新偏好得到采用，不等于物理删除历史消息。
- 通过honcho_conclude新增独立删除标记，实际取得唯一结论ID、删除并复查不存在。删除结论不代表自动清除消息、日志或备份。
- 错误token和跨workspace token实测均401，同workspace200；CPU Embedding无凭据401。原SDK断言错误使用status_code字段，首份memory/result.json保留failed；SDK实际字段为status，另存auth-recheck.json只读补验2/2，不重跑成功模型步骤。最终合并原记忆证据和补验鉴权证据，没有回写首轮成绩。
- 实际重启候选数据库和API后，排序归一化的记忆内容不变，当前公里偏好保持；模型账本前后完全一致。另验证OpenAI SDK默认base64解码及两个pgvector字段均1024维，共4项生命周期检查通过。
- 18项针对性回归通过（新增6项公共模板凭据保护+既有12项容器运输/权限），11个本轮Python文件Ruff/格式通过。未修改正式业务实现，因此本轮未再次运行原1302项完整套件，不冒称新的全量测试成绩。

### 失败、用量与收尾

首次Postgres非root初始化使用自定义Unix socket，psql仍找默认socket而失败；补齐PGHOST并继续初始化，未删库。第二次候选镜像源文件受宿主umask077影响，root所有的pyproject.toml对UID1000不可读；改COPY --chown后修复。第三次上游Dream关闭仍无条件导入NumPy，但pyproject未声明；仅候选镜像补齐NumPy并固定制品锁，未升级宿主。以上启动失败、原始镜像与日志均保留。

记忆测试代理10次尝试，8成功、2次ConnectError（网络失败也计入准入）；另加2次Key预检，**累计12次请求尝试、10个成功响应，供应商输入25869/output1141/合计27010 token，保守预留155514**。SDK对Honcho的测试客户端关闭重试；Honcho服务内部出现的连接重试均经过同一代理计数，不把它们排除在预算之外。未测定长期费用节省比例，不能将contextTokens配置值当成全平台费用硬上限。

首次收尾扫描误将API_KEY_ENV中的环境变量名引用当成实际凭据，保留audit-first-credential-scan.json；按凭据字段类型修正后复核。另补镜像上下文保护：公共Honcho JSON或env示例只要填入真实凭据即在COPY之前拒绝，错误消息不回显值。

有限测试结束已停止候选API/deriver/数据库及代理、CPU Embedding，18140/18141/18142关闭，临时profile和.tmp/honcho-test清理。保留私有provider文件、候选数据库、固定镜像/源码/制品和原始证据。七正式CPU容器ID/镜像/健康、三Conda history、既有服务端口及正式语料/向量保持。主平台记忆仍关闭，没有在测试后继续后台付费提取。

下一步若正式接入：应在主平台适配层绑定可信用户/thread/session、限制角色与写入数据范围、把Honcho内部费用并入持续资源治理，验证写入恢复/留存删除规则后发布候选镜像；本轮SDK显式session测试不能替代该平台线程映射。Key曾进入工具输出，建议轮换后只修改服务器私有honcho.env中的LLM_OPENAI_API_KEY，不再填写公共JSON的apiKey字段。
