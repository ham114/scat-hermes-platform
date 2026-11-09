2026-11-08更新：正式五核心已接通既有GBAR CUDA服务，production_gpu=true；两次实际POST和持久制品回读通过。标准GPU镜像迁移仍未执行。当前状态见 docs/HERMES_GBAR_PRODUCTION_20261108.md；下文保留10月27日基线。

# GPU 服务迁移边界

2026-10-27 实测服务器 Docker 只有 runc，未配置 NVIDIA runtime/CDI。系统盘构建后约31GiB余量。当前交付的 Compose 只声明已经构建、启动和验证的七个 CPU 服务；没有可误启动的占位 GPU 镜像。

## 本轮实际接入

| 服务 | 当前方式 | 地址 | 固定身份与用途 |
|---|---|---|---|
| RAG | 既有 `scat-hermes-rag.service`，Anaconda `scat-rag-train` | `127.0.0.1:18133` | BGE-M3/RRF Top100 + Qwen3-Reranker-0.6B，v4语料149块，权重和语料保持原位 |
| GBAR | 既有 `scat-gbar-production-gbar-1`，BentoML HTTP服务 | Docker私网3000；本轮观测 `172.30.67.2:3000` | 固定镜像 `sha256:0bbae03be3d2c1340350d7eca6c9168b8735be57adfb61883fda8bad914d7bf4`；该私网IP属于观测结果，不能当长期服务发现契约 |

GBAR 的模型封装继续使用 BentoML。它与 Docker 分属模型服务和部署层，保留 `/ready`、`/metadata`、`/predict` 契约。旧容器使用历史 Python 镜像及手工 GPU 设备/库映射，本轮只读检查，不进入、不重建、不停止该镜像，也不把它视为新容器 Conda 纪律的验收结果。当前 CPU 容器的 GBAR **执行开关关闭**；连通并不授予真实模型任务执行权限。

## 后续进入独立 GPU Compose 的条件

1. 在维护窗口配置并验收 NVIDIA Container Toolkit/CDI；保留现有 GPU 任务，核对驱动、CUDA库、设备权限和最小 GPU 探针。安装运行时/修改 Docker daemon 与现有服务切换应单独记录，不能只依据宿主 `nvidia-smi` 宣布通过。
2. RAG 从既有 `scat-rag-train` 生成独立精确 Conda/pip 清单和带 SHA256 的 Linux GPU 制品；GBAR 先把经审核的 BentoML/torch/模型依赖纳入独立既有或获准的 Anaconda 环境锁。两者都不能使用宿主环境目录挂载、venv 或 uv 替代可复核镜像。
3. GPU 镜像以固定基础镜像+锁定制品离线构建，至少预估两份环境层和临时构建空间，保留10GiB构建/5GiB运行磁盘余量。模型目录只读外置，不把权重 COPY 进镜像，不为本轮强行复制7.9GiB GPU环境。
4. 候选 RAG 回环18134、GBAR回环18127；单独日志/缓存挂载与用户1000:1000，只读根、无Docker socket、受控GPU分配。RAG入口应允许镜像内 `/opt/conda/envs/scat-rag-train/bin/python`，保持现有配置/权重/语料校验和；GBAR保留BentoML接口及版本元数据。
5. 两者分别验收健康、固定版本元数据、GPU实际推理、超时/并发/重启；RAG复跑冻结准入题集，GBAR通过模型准入测试。再将 CPU 配置指向候选地址，并以单独发布操作切换；保留原地址回滚。

这一阶段的准确状态是：**CPU部署封装已实施；RAG沿用已上线Conda服务；GBAR沿用现有BentoML容器；新的GPU镜像和GPU迁移验收未完成。**
