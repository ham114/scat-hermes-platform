# Vue 分析工作台

工作台包含自然语言对话、会话历史、数据浏览、模型任务和二维、三维结果视图。Vue、TypeScript 与 Vite 构建页面，ECharts 和 Cesium 渲染业务结果。

使用 Node.js 24 与 npm 11，在仓库根目录运行：

```bash
npm --prefix frontend ci
npm --prefix frontend run dev
```

开发服务器通过 `SCAT_DEV_PROXY_TARGET` 代理 `/api`，默认目标为 `http://127.0.0.1:8080`。作者服务目标通过 `SCAT_AUTHOR_PROXY_TARGET` 配置。

```bash
npm --prefix frontend run check
npm --prefix frontend run test:e2e
```

前端 API 类型由 [`src/api/openapi.json`](src/api/openapi.json) 生成，更新契约后可运行 `npm --prefix frontend run openapi:generate`。[项目首页](../README.md)
