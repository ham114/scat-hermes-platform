# 自主图表渲染器

固定 ECharts 6.1.0、Playwright 1.63.0、PNGJS 7.0.0，以及微软 Playwright 基础镜像摘要。`runtime.lock.json` 记录 Linux amd64 镜像和 seccomp 来源；`seccomp.json` 基于对应版本 Playwright，额外允许 Chromium 在自身用户命名空间使用 `chroot`，以兼容外层 `cap-drop ALL`，没有授予容器该 capability。原始与项目配置摘要分别记录，Apache 2.0 许可见 `PLAYWRIGHT_LICENSE`。字体和 Chromium 随固定基础镜像提供。

在本目录执行 `npm ci --ignore-scripts --no-audit --no-fund`，再以本目录为 Docker 构建上下文：

```bash
docker build -t scat-visualization-renderer:p3 .
docker image inspect scat-visualization-renderer:p3 --format '{{.Id}}'
```

将返回的 `sha256:...` 配置为 `SCAT_VIS_SANDBOX_IMAGE`。运行时只接受已存在的镜像 ID，不自动拉取可变标签。离线导入基础镜像时，校验原始配置与全部层摘要；Docker 29 格式转换可能改变导入 ID，此时还须逐项核对 RootFS diff IDs、架构、Env、Cmd、Entrypoint、User 和 WorkingDir，不能仅因导入成功放行。验证通过后可用 `--build-arg BASE_IMAGE=<已验证的本地基础镜像标签>` 构建。

受信宿主先准备至少 32 字符的随机服务密钥，保存于项目内 `var/secrets/visualization-sandbox.key`，权限 600。不要把 DS/Qwen 密钥交给绘图容器。使用现有 Conda 环境启动独立服务和 worker：

```bash
python scripts/run_visualization_sandbox.py --image sha256:YOUR_IMAGE_ID
python scripts/run_visualization_worker.py --once
```

启动 worker 前须配置两家 API、同一任务数据库与产物目录，显式启用 `SCAT_VIS_ENABLED=true`。服务默认仅监听 `127.0.0.1:18166`；受信服务持有 Docker 权限，worker 只通过携带独立 Bearer key 的内部接口发送任务 ID、代次和候选 ID。模型不能指定任意路径、镜像或 Docker 参数。正式 API 和主 Agent 工具入口在 P4 接入。

生成脚本仅通过 Blob ES module 在 Chromium 页面执行；Node 只执行固定渲染宿主。容器使用非 root、只读根目录、禁网、移除 capabilities、no-new-privileges、固定 seccomp 和资源限制，Chromium 自身沙箱保持开启。每个候选的宽屏和窄屏图片均检查尺寸、非空白内容和摘要。失败不会发布，也不会在主页面执行生成脚本。

`render_visualization_once.py` 是运维冒烟入口，读取 stdin 中的 `source`、`snapshot`、`viewports`，只在项目内生成测试产物。它没有模型凭据，也不会调用模型。短执行结果只输出到终端；需要恢复和审计的图表、数据快照与调用账本作为业务产物保存。
