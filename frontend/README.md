# Vue 门户与专业可视化前端

航空数据智能分析平台的 Vue 3 前端。根据 2026-09-12 的产品路线调整，Vue 的目标职责是系统总入口、分析任务/结果导航和专业二维/三维可视化；对话、数据查询、统计图表和模型调用由 Streamlit 主分析工作台承载。浏览器中不保存后端密钥。

M3-V1 的 SCAT 批次总览和 M3-V2 的航班检索已经完成，目前仍可作为 SCAT API 验证与回归页面使用。它们不会扩展为 UAV 或任意数据集通用页面；待 Streamlit 对等能力通过验收后，再从正式导航下线，源码和测试不会在迁移前直接删除。

## 环境

- Node.js 24 LTS（见 `.nvmrc`）
- npm 11
- 后端默认监听 `http://127.0.0.1:8080`

## 本地开发

在项目根目录双击 `start_frontend.bat`，可以同时启动 Vue 门户和 Streamlit
工作台，并自动打开 `http://localhost:5173`。脚本默认连接
`http://100.105.110.20:8080`，且不会在端口已监听时重复启动服务。
联调结束后双击根目录的 `stop_frontend.bat`，脚本只会终止命令行和项目路径均匹配的 Vue/Streamlit 进程树，不会仅凭端口结束其他程序。

如需临时覆盖默认地址，在 CMD 中运行：

```bat
set SCAT_BACKEND_URL=http://<BACKEND_HOST>:8080
set SCAT_FRONTEND_PYTHON=C:\path\to\scat-frontend\python.exe
start_frontend.bat
```

也可以分别手动启动：

```powershell
npm ci --cache .npm-cache
npm run dev
```

当前 `/` 已是系统门户，并通过整页跳转进入 Streamlit；SCAT 验证页仍保留在 `/overview` 和 `/flights`，工程诊断页为 `/foundation`。门户也接受 artifact ID 或完整结果链接，用于恢复 `/visualizations/<artifact_id>/3d`。Streamlit 公开地址由 `VITE_STREAMLIT_URL` 配置，未配置时默认使用本机 `http://localhost:8501`：

```powershell
$env:VITE_STREAMLIT_URL = "http://localhost:8501"
npm run dev
```

生产构建应把该变量设置为同域公开路径 `/assistant/`。变量只允许包含公开 URL，不得拼接 bearer token、数据库密码或其他密钥。

Streamlit 模型任务成功后会显示“打开三维轨迹视图”主按钮，直接跳回 Vue `/visualizations/<artifact_id>/3d?thread_id=<thread_id>`，不再要求复制地址。Vue 会通过受保护的 `/api/v1/analysis/artifacts/<artifact_id>` 自动加载持久化结果，并使用服务端记录的 `thread_id` 返回原对话，不信任 URL 中的展示参数。真实三维渲染仍由 M3-V3/M7 接入。

Vite 会把 `/api` 代理到 `http://127.0.0.1:8080`。后端不在默认地址时，只为当前终端设置服务端开发变量：

```powershell
$env:SCAT_DEV_PROXY_TARGET = "http://<BACKEND_HOST>:8080"
npm run dev
```

若后端启用了 `AUTH_SECRET`，本机根目录不入 Git 的 `.env` 中需配置同一个值；Vite 仅在开发代理服务端注入 Bearer header，不会打包进浏览器。不要把密钥写入 `frontend/.env`、Vue 源码或静态资源。

当前系统没有登录模块，开发和私有部署应显式使用单用户身份。本机 Streamlit 和远程 FastAPI 使用同一个非秘密稳定值：

```dotenv
# 本机 .env（供 Streamlit 使用）
ANALYSIS_SINGLE_USER_ID=local-researcher

# 服务器 .env（供 FastAPI 使用）
ANALYSIS_IDENTITY_MODE=single_user
ANALYSIS_SINGLE_USER_ID=local-researcher
```

单用户模式由后端直接确定 artifact owner，忽略浏览器提供的身份头；这不是登录认证，只适用于受控的单用户/私有网络部署。未来接入 OIDC 后，改为 `ANALYSIS_IDENTITY_MODE=trusted_proxy`，由认证代理覆盖 `X-Authenticated-User`。`disabled`（默认）会让 artifact REST 以 503 安全关闭。

修改 `ANALYSIS_SINGLE_USER_ID` 后，新任务归属于新身份；以前由随机 URL `user_id` 或其他身份创建的 artifact 不会自动迁移或越权显示。

本地可直接访问 Vue `http://localhost:5173`，再从门户打开 Streamlit `http://localhost:8501`。模型 action link 会把当前 `thread_id` 传给 Vue，结果入口的“返回原对话”会将其传回 Streamlit；其他页面的会话共享继续在 M3-I 完善。

Streamlit 当前已支持 `AnalysisResult 1.0～1.6` 的通用指标卡、表格、时序、二维轨迹、预测和 action link 渲染。1.2+ 时序只记录一个 UTC 开始时间，横轴统一使用秒时间步；SCAT 与 UAV 使用同一个渲染器，工具原始 JSON 在存在合法结构化结果时不再直接展开。协议见 `docs/ANALYSIS_RESULT.md`。

客户端连接、历史、生成、反馈以及工具无结果/失败状态已经使用通用安全提示，并保留可用的 request ID。双数据集对话的本机启动和人工验收语句见 `docs/STREAMLIT_ACCEPTANCE.md`。

## 接口契约

`src/api/openapi.json` 是从后端 FastAPI 应用实际导出的契约快照，`src/api/schema.d.ts` 是生成的 TypeScript 类型。后端接口变化后重新导出快照，再运行：

```powershell
npm run openapi:generate
```

## 质量检查

```powershell
npm run format:check
npm run lint
npm run test
npm run build
npm run test:e2e
```

也可用 `npm run check` 一次执行格式、静态检查、单元测试和生产构建。Playwright 首次运行前需要安装 Chromium：

```powershell
npx playwright install chromium
```

## 生产部署

```powershell
npm ci --cache .npm-cache
npm run build
```

将 `dist/` 内容部署到 Nginx 静态目录。`deploy/nginx-scat.conf` 已包含 Vue SPA 回退、Streamlit WebSocket、`/api/` 和 `/agui/` SSE 反向代理。生产构建和 Streamlit 分别使用：

```powershell
$env:VITE_STREAMLIT_URL = "/assistant/"
npm run build
```

```bash
export PORTAL_URL=/
export ANALYSIS_VISUALIZATION_BASE_URL=/
export ANALYSIS_IDENTITY_MODE=single_user
export ANALYSIS_SINGLE_USER_ID=local-researcher
python -m streamlit run src/streamlit_app.py \
  --server.address 127.0.0.1 \
  --server.port 8501 \
  --server.baseUrlPath assistant \
  --server.headless true
```

目标生产路径：

```text
/                         Vue 门户与专业可视化
/assistant/               Streamlit 主分析工作台
/api/                     FastAPI 确定性 API
/agui/                    FastAPI Agent 事件接口
```

启用配置前应按实际静态目录、端口和站点名调整。样例会清空客户端传入的 `X-Authenticated-User`；单用户 owner 来自服务器环境。FastAPI 和 Streamlit 应只监听 `127.0.0.1`。把样例放入 Nginx `http` 上下文包含的 `conf.d`/站点配置后，先运行 `sudo nginx -t`，再验证 Vue → Streamlit → Vue、WebSocket/SSE、artifact 读取、原对话返回和刷新恢复。未来接入 OIDC 时再把空身份头替换为网关提供的已验证 subject。
