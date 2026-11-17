# SCAT Hermes Platform

**面向航空数据研究的 Agent 分析工作台，覆盖航班查询、轨迹诊断和模型实验。**

[项目介绍](#项目介绍) · [界面展示](#界面展示) · [功能概览](#功能概览) · [操作示例](#操作示例) · [系统架构](#系统架构) · [本地运行](#本地运行) · [开发入口](#开发入口)

## 项目介绍

SCAT Hermes Platform 面向需要检查飞行数据、解释轨迹变化和比较预测模型的研究人员。研究者可以先从自然语言对话或数据浏览页定位一个批次、航班或 UAV 架次，再检查样本覆盖、实际轨迹和误差，最后决定是否需要运行模型实验。

- **SCAT 航班数据**包含航班元数据、实飞轨迹和可用的 CFPL 计划航路。可从批次航班量和数据质量下钻到单航班，在地图上回放轨迹，或查询计划航路与实飞的匹配误差。
- **UAV 传感器数据**包括 GPS 位置、各方向速度、姿态和惯性等通道。可按架次和原始样本窗口查看时序、通道相关性与分阶段分布，并用同一份结果切换图表或导出数据。

工作台通过版本化技能把自然语言问题转换成有明确数据范围的查询。页面保留结果来源、输入窗口、字段单位和样本覆盖，方便核对图表和恢复历史展示。涉及预测、历史回测或模型对比时，先确认观测窗口、模型版本与运行参数，再由后台队列执行，结果可从原会话回读。误差评估使用可匹配的实飞真值。

## 界面展示

### UAV 多通道时序与分布

下面的 200 点窗口覆盖速度由低到高的变化过程。折线同时展示 GPS 东向、北向和天向速度，便于对照三个分量随时间的变化。

<img src="docs/assets/uav-gps-velocity-timeseries.jpg" width="740" alt="200 个 UAV 样本的 GPS 速度时序，显示东向、北向和天向三个分量" />

同一窗口可以按阶段查看速度分布。箱线图用中位数、四分位数和两端极值展示各组样本的差异。

<img src="docs/assets/uav-gps-phase-boxplot.jpg" width="740" alt="UAV 三个 GPS 速度分量的阶段箱线图，图表区提供 CSV、PNG 和全屏操作" />

<details>
<summary>查看四通道 Pearson 相关性热力图</summary>

选取同一窗口的 GPS 高度与三个速度分量，按同期配对样本计算相关系数。

<img src="docs/assets/uav-gps-pearson-heatmap.jpg" width="740" alt="GPS 高度、东向速度、北向速度和天向速度的四通道 Pearson 相关性热力图" />

</details>

### 二维回放与三维跟随

| 二维轨迹回放                                                                                   | 三维跟随视图                                                                                        |
| ---------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| <img src="docs/assets/trajectory-playback.jpg" width="390" alt="卫星底图上的航迹和回放控件" /> | <img src="docs/assets/trajectory-3d-follow.jpg" width="390" alt="三维跟随视角下的航迹和实时高度" /> |

## 功能概览

| 场景           | 工作台能力                                                                                                   |
| -------------- | ------------------------------------------------------------------------------------------------------------ |
| 批次与质量分析 | 在数据总览页查看航班量趋势、主要机型、热门航线、机场分布和质量摘要；按条件筛选质量合格与存在问题的航班。     |
| UAV 传感器分析 | 按架次和样本窗口查看多通道时序、同期相关性及分阶段分布，并核对有效样本数。                                      |
| 航路误差评估   | 查看 CFPL 计划航路与实飞轨迹的水平和垂直误差。按时域、主导阶段或机型查看汇总，定位误差最大的样本并导出 CSV。 |
| 轨迹分析       | 在二维街道底图和 Cesium 三维场景中查看轨迹，支持定位、回放、全屏和历史结果恢复。                             |
| 预测与实验     | 使用匀速或 Kalman 基线，以及已登记的 GBAR GPU 模型，执行未来预测、历史回测、同样本比较与重复实验。           |
| 结果交付       | 在同一工作台查看指标、表格、轨迹、分类与预测结果。对可绘制结果支持切换图表和字段、全屏、CSV/PNG 导出和展示配置恢复。 |
| 知识问答       | 检索航空术语、指标定义与业务规则，依据回读的原文证据生成带引用回答。                                         |
| 技能管理       | 按版本加载业务技能，支持草稿修订、隔离试跑、审核发布和版本回滚。                                             |

## 操作示例

**从误差汇总追到单个样本。** 在「预测评估」页选择 SCAT 批次，查看误差随时域变化的曲线、样本量和覆盖航班。切换主导阶段或机型比较汇总指标，再在「最差案例」查看具体航班、预测时域和误差。也可以在对话中读取既有评估结果：

> 查询批次 `scat20161015_20161021` 已物化的 CFPL 误差，按水平误差从大到小返回前两条，列出航班、时域和误差（米），并说明样本覆盖范围。

该查询复用 CFPL 计划航路与实飞轨迹的物化指标，结果可回查样本和数据来源。

**从查询到可视化。** 先按呼号或批次定位航班，然后输入「绘制这架航班的二维轨迹，展示数据来源」。工作台用查询回执生成地图图层，并提供三维视图和原始证据入口。

**从同一窗口看时序与分布。** 对已选 UAV 架次指定连续 200 个原始样本，读取 GPS 高度及东向、北向和天向速度。用时序折线看变化过程，在阶段分布区切换箱线图，在相关性区切换热力图。各图保留字段单位和当前样本范围。

**从方案到预测报告。** 对已选航班输入「用最后 200 个观测点，预测从末点开始未来 10、30、60 秒的轨迹」。查看数据窗口、模型版本和运行范围，确认后跟踪任务状态。完成后在结果页查看预测轨迹、覆盖范围及可用评估，按需导出表格。

**从历史结果继续分析。** 从会话历史打开已保存的结果，调整表格排序或图表布局。保存展示配置后，再次打开时可恢复相应视图。

## 系统架构

```mermaid
flowchart LR
    UI[Vue 工作台] --> API[FastAPI 平台]
    API --> Main[Main Agent]
    Main --> Skills[版本化技能]
    Main --> MCP[SCAT 与 UAV 查询工具]
    Main --> RAG[知识检索与原文核验]
    MCP --> Data[(MongoDB)]
    MCP --> Results[结果引用与标准图表]
    Main --> Plan[任务方案]
    Plan --> Confirm[用户确认]
    Confirm --> Queue[(持久任务队列)]
    Queue --> Worker[Worker Agent]
    Worker --> Compute[CPU 基线或 GBAR GPU]
    Compute --> Results
    Results --> UI
```

- **Agent 编排。** Main 读取技能并组织查询、解释和任务方案。Worker 依据批准快照执行操作。Author 负责技能草稿与试跑，发布由管理端确认。
- **可追踪的执行。** 确认时固定数据范围、模型、技能和预算。任务以稳定试验标识传递制品，后台使用租约管理执行权，页面可查进度和已保存结果。
- **统一的结果协议。** `AnalysisResult` 将指标、表格、时序和轨迹等业务结果交给相应渲染器。页面以受控引用读取结果，并保留来源、观测范围和评估口径。
- **研究口径。** 未来预测按固定观测窗口和目标时刻运行。历史回测使用可配对真值计算误差，并记录样本覆盖与排除原因。

**技术栈** · Python · FastAPI · Hermes Agent · MCP · MongoDB · SQLite · Vue 3 · TypeScript · ECharts · Cesium · Docker · NVIDIA GPU

## 本地运行

准备 Python 3.12–3.14 的项目 Conda 环境、Node.js 24、npm 11，以及符合数据契约的 MongoDB。依赖定义见 [pyproject.toml](pyproject.toml) 和 [前端锁文件](frontend/package-lock.json)。

1. 从 [`.env.example`](.env.example) 创建本机 `.env`，配置数据连接及需要的模型服务。配置项见[配置说明](docs/CONFIGURATION.md)。
2. 在已有项目 Python 环境中启动确定性数据 API。

   ```bash
   python -m uvicorn service.scat_only:app --app-dir src --host 127.0.0.1 --port 8080
   ```

3. 启动前端开发服务，通过 Vite 地址打开工作台。`/api` 默认代理到 `127.0.0.1:8080`。

   ```bash
   npm --prefix frontend ci
   npm --prefix frontend run dev
   ```

要运行自然语言对话、后台任务和技能管理，请接入固定版本的原生 Hermes 运行时，并将前端连接到完整平台 API。部署依赖和启动入口见 [Hermes 服务说明](services/hermes/README.md) 与[部署指南](docs/DEPLOYMENT.md)。

## 开发入口

| 目录                                           | 内容                                         |
| ---------------------------------------------- | -------------------------------------------- |
| [`src/service`](src/service)                   | FastAPI 路由、会话与任务接口、身份及结果归属 |
| [`src/hermes_runtime`](src/hermes_runtime)     | 原生 Agent 适配、技能加载、RAG 和结果回执    |
| [`src/workflow_runtime`](src/workflow_runtime) | 观测窗口、模型试验、评估、物化与报告         |
| [`skills`](skills)                             | 业务技能正文和参考资料                       |
| [`frontend`](frontend)                         | Vue 工作台、二维及三维可视化                 |
| [`tests`](tests)                               | API、权限、任务生命周期与业务契约测试        |

```bash
python -m pytest
npm --prefix frontend run check
npm --prefix frontend run test:e2e
```

接口契约见[会话 API](docs/api/hermes-conversations.openapi.json)与[记忆和 Agent 设置 API](docs/api/hermes-memory-settings.openapi.json)。技能版本和操作映射见[技能锁](services/hermes/skills.lock.json)与[工作流操作契约](services/hermes/workflow-operations.v1.json)。

## 来源与许可

项目持续开发，基于 [Agent Service Toolkit](https://github.com/JoshuaC215/agent-service-toolkit) 的业务实现扩展。源码许可与第三方来源见 [LICENSE](LICENSE) 和 [NOTICE](NOTICE.md)。运行所需的数据、模型权重和知识库由部署者提供。
