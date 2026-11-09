import vue from '@vitejs/plugin-vue'
import { defineConfig, loadEnv, type Plugin } from 'vite'
import { createReadStream, cpSync, existsSync, statSync } from 'node:fs'
import { extname, resolve, sep } from 'node:path'
import { fileURLToPath, URL } from 'node:url'

/** Cesium 静态资源在 npm 包里的根目录（绝对路径，resolve 规范化去除尾斜杠）。 */
const CESIUM_SOURCE_ROOT = resolve(
  fileURLToPath(new URL('node_modules/cesium/Build/Cesium/', import.meta.url)),
)

const MIME_TYPES: Record<string, string> = {
  '.css': 'text/css',
  '.js': 'text/javascript',
  '.json': 'application/json',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.webp': 'image/webp',
  '.gif': 'image/gif',
  '.svg': 'image/svg+xml',
  '.wasm': 'application/wasm',
  '.ktx2': 'image/ktx2',
  '.bin': 'application/octet-stream',
}

/**
 * 把 Cesium 静态资源（Workers/ThirdParty/Assets/Widgets）提供给浏览器：
 * - build：closeBundle 时递归拷贝到 dist/cesium/（与源目录结构一致）；
 * - dev：configureServer 中间件把 /cesium/* 映射到 npm 包目录。
 * 浏览器侧通过 window.CESIUM_BASE_URL='/cesium' 引用。
 */
function copyCesiumAssets(): Plugin {
  return {
    name: 'scat-cesium-assets',
    configureServer(server) {
      server.middlewares.use('/cesium', (req, res, next) => {
        const raw = decodeURIComponent((req.url ?? '').split('?')[0] ?? '')
        // connect 不剥挂载前缀：/cesium/Assets/x.png -> Assets/x.png
        const relative = raw.replace(/^[/\\]+/, '').replace(/^cesium[/\\]/, '')
        const file = resolve(CESIUM_SOURCE_ROOT, relative)
        if (file !== CESIUM_SOURCE_ROOT && !file.startsWith(CESIUM_SOURCE_ROOT + sep)) {
          res.statusCode = 403
          res.end()
          return
        }
        if (!existsSync(file) || !statSync(file).isFile()) {
          next()
          return
        }
        res.statusCode = 200
        res.setHeader(
          'Content-Type',
          MIME_TYPES[extname(file).toLowerCase()] ?? 'application/octet-stream',
        )
        res.setHeader('Cache-Control', 'no-cache')
        createReadStream(file).pipe(res)
      })
    },
    closeBundle() {
      const targetRoot = fileURLToPath(new URL('dist/cesium/', import.meta.url))
      for (const name of ['Workers', 'ThirdParty', 'Assets', 'Widgets']) {
        const source = resolve(CESIUM_SOURCE_ROOT, name)
        if (!existsSync(source)) {
          throw new Error(`Cesium 静态资源缺失: ${source}（请先 npm install）`)
        }
        cpSync(source, resolve(targetRoot, name), { recursive: true })
      }
    },
  }
}

export default defineConfig(({ mode }) => {
  // Load the project-root .env as well as process variables. Secrets are
  // consumed only by the dev proxy and are never bundled into browser code.
  const rootEnv = loadEnv(mode, fileURLToPath(new URL('..', import.meta.url)), '')
  const authSecret = (process.env.AUTH_SECRET ?? rootEnv.AUTH_SECRET)?.trim()
  const proxyHeaders = {
    ...(authSecret ? { Authorization: `Bearer ${authSecret}` } : undefined),
  }
  return {
    cacheDir: process.env.SCAT_FRONTEND_CACHE_DIR ?? 'node_modules/.vite',
    plugins: [vue(), copyCesiumAssets()],
    resolve: {
      alias: {
        '@': fileURLToPath(new URL('./src', import.meta.url)),
      },
    },
    server: {
      proxy: {
        '/author-api': {
          target: process.env.SCAT_AUTHOR_PROXY_TARGET ?? 'http://127.0.0.1:18124',
          rewrite: (path: string) => path.replace(/^\/author-api/, ''),
          changeOrigin: true,
        },
        '/api': {
          target: process.env.SCAT_DEV_PROXY_TARGET ?? 'http://127.0.0.1:8080',
          changeOrigin: true,
          ...(Object.keys(proxyHeaders).length ? { headers: proxyHeaders } : undefined),
        },
      },
    },
  }
})
